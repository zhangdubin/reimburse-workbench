"""AI 业务任务：把大模型接到具体动作上（发票补字段、分类、分析、审批建议、记账）。

分工
----
- 传输与配置在 `ai.py`；这里只有**提示词与结构化解析**，不碰 HTTP 也不碰数据库，
  方便单独测试与复用。
- 每个任务都强制模型输出 JSON，并用 `ai.extract_json` 容错解析（模型爱加解释文字）。
- 所有任务都写清楚「不知道就留空」，宁可少填也不许编造——报销系统里一个错金额
  比一个空金额麻烦得多。

关于「执行写操作」
------------------
AI 只负责**产出方案**（改哪张单、填什么值），真正的写库由前端在人工确认后调用
既有业务接口完成。这样做的理由：

1. 权限、校验、审计、状态机全都在既有接口里，不重复实现就不会有两套规则；
2. AI 拿不到绕过校验的后门，最坏情况也只是「建议得不对」，不会「改错数据」；
3. 每一步都有明确的用户确认点，符合财务场景的可追溯要求。
"""

from __future__ import annotations

import json
from typing import Any

from . import ai

# ---------------------------------------------------------------- 发票识别兜底

RECOGNIZE_SYSTEM = """你是中国增值税发票的信息提取专家。你会收到发票的文字内容或图片，\
需要提取出结构化字段。

严格遵循：
1. 只提取你能明确看到的信息，任何不确定的字段一律填 null，**绝对不要猜测或编造**。
2. 看不清、被遮挡、疑似手写涂改的字段，填 null。
3. 金额只填数字，不带货币符号和千分位，例如 1280.00。
4. 日期统一成 YYYY-MM-DD。
5. 税号是 18 位统一社会信用代码（也可能出现 15/17/20 位老税号），只填字母数字。
6. 务必区分「购买方」（本公司的客户方，即付款方）与「销售方」（开票方，收款方）。
   发票上通常购买方在左上、销售方在右上；不要写反。
7. 发票类型按票面字样判断，例如「增值税电子普通发票」「增值税专用发票」「数电票」
   「铁路电子客票」「航空运输电子客票行程单」。
8. 价税合计填**含税总额**，不要把不含税金额或税额填进去。

只输出一个 JSON 对象，不要任何解释文字：
{"invoice_no": null, "invoice_code": null, "invoice_type": null, "amount": null,
 "tax_amount": null, "tax_rate": null, "invoice_date": null, "seller_name": null,
 "seller_tax_no": null, "buyer_name": null, "buyer_tax_no": null, "confidence": 0.0,
 "note": ""}

字段说明：invoice_no 发票号码；invoice_code 发票代码（数电票没有，填 null）；
tax_rate 税率百分数（13 表示 13%）；confidence 是你对自己提取结果的把握（0-1）；
note 里可以写一行说明，比如「票面模糊，金额依 OCR 结果推测」。"""


def _recognize_prompt(text: str, filename: str, hint: str) -> str:
    parts = []
    if filename:
        parts.append(f"文件名：{filename}")
    if hint:
        parts.append(f"邮件主题/正文线索：{hint}")
    if text:
        parts.append("发票文字内容（可能来自 OCR，个别汉字可能有误，请结合常识判断）：\n" + text[:6000])
    else:
        parts.append("请直接阅读随附的发票图片。")
    return "\n\n".join(parts)


def fill_invoice(
    cfg: ai.AiConfig,
    *,
    text: str = "",
    images: list[tuple[bytes, str]] | None = None,
    filename: str = "",
    hint: str = "",
) -> dict:
    """让模型从文本/图片里补出发票字段。返回规范化后的字段字典。"""
    prompt = _recognize_prompt(text, filename, hint)
    if images:
        messages = [
            {"role": "system", "content": RECOGNIZE_SYSTEM},
            {"role": "user", "content": [{"type": "text", "text": prompt}]
             + [ai.image_part(data, mime) for data, mime in images]},
        ]
        model = cfg.vision_model or cfg.model
    else:
        messages = [
            {"role": "system", "content": RECOGNIZE_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        model = cfg.model

    raw, usage = ai.chat(cfg, messages, model=model, json_mode=True, max_tokens=1200, temperature=0)
    got = ai.extract_json(raw) or {}
    return {"fields": _normalize_invoice(got), "usage": usage, "raw": raw, "model": model}


_FIELD_TYPES: dict[str, type] = {
    "invoice_no": str, "invoice_code": str, "invoice_type": str,
    "amount": float, "tax_amount": float, "tax_rate": float,
    "invoice_date": str, "seller_name": str, "seller_tax_no": str,
    "buyer_name": str, "buyer_tax_no": str,
}


def _normalize_invoice(got: dict) -> dict:
    """清洗模型输出：类型对齐、空值剔除、明显不合理的值丢掉。"""
    out: dict[str, Any] = {}
    for key, kind in _FIELD_TYPES.items():
        value = got.get(key)
        if value in (None, "", "null", "None", "未知", "不详"):
            continue
        if kind is float:
            try:
                num = float(str(value).replace(",", "").replace("￥", "").replace("¥", "").strip())
            except (TypeError, ValueError):
                continue
            if key == "tax_rate" and not (0 < num <= 100):
                continue
            if key in ("amount", "tax_amount") and not (0 <= num < 100_000_000):
                continue
            out[key] = round(num, 2)
        else:
            text = str(value).strip()
            if key == "invoice_date":
                import re

                m = re.search(r"(\d{4})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})", text)
                if not m:
                    continue
                out[key] = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            elif key in ("invoice_no", "invoice_code"):
                digits = "".join(ch for ch in text if ch.isalnum())
                if not (6 <= len(digits) <= 25):
                    continue
                out[key] = digits
            elif key.endswith("_tax_no"):
                clean = "".join(ch for ch in text.upper() if ch.isalnum())
                if len(clean) < 15:
                    continue
                out[key] = clean
            else:
                out[key] = text[:120]
    if "confidence" in got:
        try:
            out["_confidence"] = max(0.0, min(1.0, float(got["confidence"])))
        except (TypeError, ValueError):
            pass
    if got.get("note"):
        out["_note"] = str(got["note"])[:200]
    return out


# ---------------------------------------------------------------- 费用分类


CLASSIFY_SYSTEM = """你是企业费用报销的费用类型归类助手。你会拿到若干条费用明细和一份\
可选的费用类型清单，需要为每条明细挑选最合适的费用类型。

规则：
1. **只能从给定清单里选**，不要臆造新的类型名。
2. 依据是摘要文字、金额、发生日期；确实判断不了就把 category 填 null。
3. 每条给出 0-1 的置信度，不确定就低分。
4. 同一批明细里可以重复使用同一个类型。

只输出 JSON：{"items": [{"index": 0, "category": "差旅费", "confidence": 0.9, "reason": "简短理由"}]}"""


def suggest_categories(cfg: ai.AiConfig, rows: list[dict], categories: list[str]) -> dict:
    if not rows:
        return {"items": [], "usage": {}}
    payload = {
        "可选费用类型": categories,
        "待归类明细": [
            {
                "index": i,
                "摘要": r.get("description") or "",
                "金额": r.get("amount"),
                "发生日期": r.get("occur_date") or "",
            }
            for i, r in enumerate(rows)
        ],
    }
    raw, usage = ai.chat(
        cfg,
        [
            {"role": "system", "content": CLASSIFY_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        json_mode=True, max_tokens=1500, temperature=0,
    )
    got = ai.extract_json(raw) or {}
    items = got.get("items")
    if not isinstance(items, list):
        items = []
    # 只保留清单里真实存在的类型，防止模型自由发挥
    valid = {c.strip() for c in categories}
    clean = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("category") or "").strip()
        if name not in valid:
            continue
        try:
            idx = int(it.get("index"))
        except (TypeError, ValueError):
            continue
        try:
            conf = float(it.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        clean.append({
            "index": idx, "category": name,
            "confidence": round(max(0.0, min(1.0, conf)), 2),
            "reason": str(it.get("reason") or "")[:80],
        })
    return {"items": clean, "usage": usage, "raw": raw}


# ---------------------------------------------------------------- 单据分析

ANALYZE_SYSTEM = """你是企业费用报销的合规与风控分析助手。你会拿到一张报销单的完整信息\
（含明细、发票、审批历史、以及系统已经算出的异常提示），需要输出一份简明分析。

请围绕以下几点：
1. **摘要**：一句话说清这张单子是什么事、多少钱、什么状态。
2. **关注点**：金额异常、发票缺失或重复、费用类型与事由不匹配、时间跨度异常、
   超出预算、审批路径异常等。每条要指出依据，不要泛泛而谈。
3. **建议**：给申请人或财务的下一步具体动作。
4. 如果确实没有问题，就明确说「未发现明显异常」，不要为了凑数编问题。

只输出 JSON：
{"summary": "...", "level": "正常|关注|风险", "findings": [{"title": "...", "detail": "...", "level": "正常|关注|风险"}], "suggestions": ["..."]}"""


def analyze_reimbursement(cfg: ai.AiConfig, payload: dict) -> dict:
    raw, usage = ai.chat(
        cfg,
        [
            {"role": "system", "content": ANALYZE_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)[:12000]},
        ],
        json_mode=True, max_tokens=1600, temperature=0.2,
    )
    got = ai.extract_json(raw) or {}
    return {
        "summary": str(got.get("summary") or "").strip(),
        "level": _level(got.get("level")),
        "findings": _findings(got.get("findings")),
        "suggestions": [str(s)[:200] for s in (got.get("suggestions") or []) if str(s).strip()][:6],
        "usage": usage, "raw": raw,
    }


def _level(value: Any) -> str:
    text = str(value or "").strip()
    return text if text in ("正常", "关注", "风险") else "正常"


def _findings(value: Any) -> list[dict]:
    out = []
    for it in value or []:
        if isinstance(it, str):
            out.append({"title": it[:80], "detail": "", "level": "关注"})
            continue
        if not isinstance(it, dict):
            continue
        out.append({
            "title": str(it.get("title") or "")[:80],
            "detail": str(it.get("detail") or "")[:300],
            "level": _level(it.get("level")),
        })
    return out[:8]


# ---------------------------------------------------------------- 审批建议

APPROVAL_SYSTEM = """你是协助审批人把关报销单的助手。你会拿到一张待审批报销单的信息，\
需要给出**参考意见**（最终决定权在审批人）。

要求：
1. 明确给出倾向：通过 / 需核实 / 建议驳回，并说明理由。
2. 逐条列出你依据的事实（金额、发票张数与金额是否吻合、是否重复报销、
   与事由是否匹配、是否有超期或大额提示）。
3. 如果信息不足以判断，明确列出「还需要确认什么」，不要硬给结论。
4. 语气克制、就事论事，不要使用「严重违规」这类定性措辞。

只输出 JSON：
{"recommend": "通过|需核实|建议驳回", "confidence": 0.0,
 "reasons": ["..."], "risks": ["..."], "questions": ["..."]}"""


def approval_advice(cfg: ai.AiConfig, payload: dict) -> dict:
    raw, usage = ai.chat(
        cfg,
        [
            {"role": "system", "content": APPROVAL_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)[:12000]},
        ],
        json_mode=True, max_tokens=1200, temperature=0.2,
    )
    got = ai.extract_json(raw) or {}
    recommend = str(got.get("recommend") or "").strip()
    if recommend not in ("通过", "需核实", "建议驳回"):
        recommend = "需核实"
    try:
        conf = float(got.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    return {
        "recommend": recommend,
        "confidence": round(max(0.0, min(1.0, conf)), 2),
        "reasons": [str(x)[:200] for x in (got.get("reasons") or []) if str(x).strip()][:6],
        "risks": [str(x)[:200] for x in (got.get("risks") or []) if str(x).strip()][:6],
        "questions": [str(x)[:200] for x in (got.get("questions") or []) if str(x).strip()][:5],
        "usage": usage, "raw": raw,
    }


# ---------------------------------------------------------------- AI 记账

BOOKKEEPING_SYSTEM = """你是报销单编制助手。你会拿到一批**尚未关联报销单的发票**，\
以及费用类型、部门、客户、项目的可选清单。请把它们组织成一张或几张报销单草稿。

要求：
1. 能合并的就合并成一张（同一批次、同一客户/项目的费用适合放一起）；
   差异明显（不同客户、时间跨度大）就拆成多张，最多 3 张。
2. 每张单要给出标题（简洁、体现事由）、事由说明、以及明细行。
   每条明细对应一张发票，金额取发票的价税合计，税额取发票税额，日期取开票日期。
3. 费用类型必须从给定清单里选；判断不了就留空让财务补。
4. customer_id / project_id / department_id 只能从给定清单里选，
   没有把握就填 null，**不要猜**。
5. 标题不要出现发票号码，用业务语言描述，例如「9 月深圳客户拜访差旅费」。

只输出 JSON：
{"drafts": [{"title": "...", "purpose": "...", "department_id": null,
  "customer_id": null, "project_id": null,
  "items": [{"invoice_id": 0, "category_id": null, "occur_date": "2026-09-01",
             "amount": 0, "tax_amount": 0, "description": "..."}],
  "note": "编制说明"}],
 "skipped": [{"invoice_id": 0, "reason": "为什么没用上"}]}"""


def bookkeeping_plan(cfg: ai.AiConfig, payload: dict) -> dict:
    raw, usage = ai.chat(
        cfg,
        [
            {"role": "system", "content": BOOKKEEPING_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)[:16000]},
        ],
        json_mode=True, max_tokens=2600, temperature=0.2,
    )
    got = ai.extract_json(raw) or {}
    return {
        "drafts": _drafts(got.get("drafts"), payload),
        "skipped": _skipped(got.get("skipped")),
        "usage": usage, "raw": raw,
    }


def _drafts(value: Any, payload: dict) -> list[dict]:
    """清洗草稿方案，并校验发票 ID 确实在本次输入里（防模型张冠李戴）。"""
    allowed_invoices = {int(i["id"]) for i in payload.get("invoices", []) if i.get("id")}
    allowed_categories = {int(c["id"]) for c in payload.get("categories", []) if c.get("id")}
    allowed_depts = {int(d["id"]) for d in payload.get("departments", []) if d.get("id")}
    allowed_customers = {int(c["id"]) for c in payload.get("customers", []) if c.get("id")}
    allowed_projects = {int(c["id"]) for c in payload.get("projects", []) if c.get("id")}

    def _int_or_none(v, allowed=None):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        if allowed is not None and n not in allowed:
            return None
        return n

    out: list[dict] = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        rows = []
        for row in item.get("items") or []:
            if not isinstance(row, dict):
                continue
            inv_id = _int_or_none(row.get("invoice_id"), allowed_invoices)
            if inv_id is None:
                continue
            try:
                amount = round(float(row.get("amount") or 0), 2)
            except (TypeError, ValueError):
                amount = 0.0
            try:
                tax = round(float(row.get("tax_amount") or 0), 2)
            except (TypeError, ValueError):
                tax = 0.0
            rows.append({
                "invoice_id": inv_id,
                "category_id": _int_or_none(row.get("category_id"), allowed_categories),
                "occur_date": str(row.get("occur_date") or "")[:10],
                "amount": amount,
                "tax_amount": tax,
                "description": str(row.get("description") or "")[:120],
            })
        if not rows:
            continue
        out.append({
            "title": str(item.get("title") or "").strip()[:80] or "待补充标题",
            "purpose": str(item.get("purpose") or "").strip()[:200],
            "department_id": _int_or_none(item.get("department_id"), allowed_depts),
            "customer_id": _int_or_none(item.get("customer_id"), allowed_customers),
            "project_id": _int_or_none(item.get("project_id"), allowed_projects),
            "items": rows,
            "note": str(item.get("note") or "")[:200],
        })
    return out[:3]


def _skipped(value: Any) -> list[dict]:
    out = []
    for it in value or []:
        if isinstance(it, dict):
            out.append({
                "invoice_id": it.get("invoice_id"),
                "reason": str(it.get("reason") or "")[:120],
            })
    return out[:20]


# ---------------------------------------------------------------- 助手动作目录
#
# 动作目录与工具目录都定义在 `ai_tools.py`（那里同时有参数预校验）。
# 这里只保留解析与提示词，避免「动作清单」散在两个文件里各改一半。
from .ai_tools import ASSISTANT_ACTION_TYPES, WRITE_ACTIONS  # noqa: E402


def _action_catalog() -> str:
    lines = []
    for act in WRITE_ACTIONS.values():
        lines.append(f"- {act['type']}（{act['label']}）：参数 {json.dumps(act['params'], ensure_ascii=False)}")
    return "\n".join(lines)


ASSISTANT_SYSTEM = """你是「销售费用报销管理工作台」内置的智能助手。你面对的是一套完整的费用\
报销系统，目标是**替用户把事办完**，而不是给他指路：能查的自己查，能做的直接给出可执行方案。

## 说话方式
- 用中文，简洁、直接、口语化，别写小作文，别用「首先其次最后」这种套话。
- 涉及金额带千分位和「元」，日期用 2026-09-18 这种格式。
- 结论要落到具体单号 / 发票号 / 金额上。**没查到的就说没查到**，不要编。

## 你有一个工具箱（关键，务必读完）
下面这份「本次会话上下文」只是开场摘要，是不全的。**在说「系统里找不到 / 我没有这类数据」
之前，先用工具去查**——绝大多数时候数据就在库里，只是摘要里没列。

### 1) 只读查询工具：你自己调用，服务端执行后把结果回给你
需要数据时，在回答里输出一个 ```tool 代码块（一次一个）：

```tool
{"tool": "list_invoices", "params": {"unlinked_only": true}}
```

系统会把查询结果追加给你，你可以接着再查（最多连续 4 次），最后给出最终回答。
命令式请求（「帮我查一下…」）也应该先查再答，而不是让用户自己去页面上看。

### 2) 写操作：给出可执行方案，用户点确认后由系统执行
用户要求新建 / 修改 / 提交 / 审批 / 关联 / 分类 / 查验 / 删除数据时，**不要只讲步骤**，
直接输出一个 ```action 代码块：

```action
{"type": "动作类型", "params": {...}, "summary": "一句话说明将会发生什么"}
```

- 凡是指人 / 客户 / 项目 / 部门 / 费用类型的参数，**可以直接写用户说的名字**
  （"张斌"、"xts"、"商务宴请"），系统负责解析成 id。所以**不要**以「我找不到这个客户的 id」
  为由拒绝——先去 list_master / search 里查一下，查不到再说。
- 系统会对参数做前置校验（必填项、单据状态是否可编辑、你有没有权限）。
  校验不过时系统会把原因告诉你，这时**用一句话向用户问清缺的那一项**，
  不要原样重复输出同一个动作。
- 一次最多输出一个 action 块。需要多步就先做最关键的一步。
- 新建报销单时：申请人默认取当前登录账号绑定的员工；如果是替别人建单，
  必须把 applicant 写上，否则系统会拦下来。
- 删除、驳回、付款、重置口令这类不可逆操作，先用一句话提醒影响范围。

## 可用工具与动作
%s

## 本次会话上下文
%s
"""


def assistant_system(context: str, catalog: str = "") -> str:
    return ASSISTANT_SYSTEM % (catalog or _action_catalog(), context)


_ACTION_BLOCK = "```action"
_TOOL_BLOCK = "```tool"


def parse_action(text: str) -> dict | None:
    """从助手回复里抠出 action 代码块。没有就返回 None。"""
    if not text or _ACTION_BLOCK not in text:
        return None
    start = text.find(_ACTION_BLOCK)
    body = text[start + len(_ACTION_BLOCK) :]
    end = body.find("```")
    if end >= 0:
        body = body[:end]
    got = ai.extract_json(body)
    if not got or not got.get("type"):
        return None
    if got["type"] not in ASSISTANT_ACTION_TYPES:
        return None
    return {
        "type": got["type"],
        "params": got.get("params") or {},
        "summary": str(got.get("summary") or "")[:300],
    }


def parse_tool(text: str) -> dict | None:
    """从助手回复里抠出 tool 代码块（只读查询请求）。"""
    if not text or _TOOL_BLOCK not in text:
        return None
    start = text.find(_TOOL_BLOCK)
    body = text[start + len(_TOOL_BLOCK) :]
    end = body.find("```")
    if end >= 0:
        body = body[:end]
    got = ai.extract_json(body)
    if not got:
        return None
    name = str(got.get("tool") or got.get("name") or "").strip()
    if not name:
        return None
    params = got.get("params") if isinstance(got.get("params"), dict) else {}
    return {"tool": name, "params": params}


def strip_blocks(text: str) -> str:
    """把 ```action / ```tool 代码块从正文里删掉——它们是给程序看的，不是给人看的。"""
    if not text:
        return text
    out = []
    rest = text
    while True:
        spots = [i for i in (rest.find(_ACTION_BLOCK), rest.find(_TOOL_BLOCK)) if i >= 0]
        if not spots:
            break
        start = min(spots)
        out.append(rest[:start])
        body = rest[start:]
        body = body[body.find("```") + 3 :]
        end = body.find("```")
        rest = "" if end < 0 else body[end + 3 :]
    out.append(rest)
    return "".join(out).strip()


# 兼容老名字：早先只藏 action 块，现在还要藏 tool 块
def strip_action(text: str) -> str:
    return strip_blocks(text)

