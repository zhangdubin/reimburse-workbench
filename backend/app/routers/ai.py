"""AI 接口：模型配置、助手对话、智能分析与 AI 记账。

权限分层
--------
- 模型配置（增删改、连通性测试、用量统计）：仅管理员。API Key 属敏感凭据。
- 助手对话：所有登录用户，但上下文、查询工具、动作目录都按各自角色裁一遍。
- 单据分析 / 审批建议：需要对本单有可见权限；审批建议额外限审批人与管理员。
- AI 记账：财务与管理员（与手工入账的权限保持一致）。

关于「AI 执行写操作」
--------------------
本模块**不提供任何直接写业务数据的接口**。助手只输出结构化的动作方案
（`type` + `params` + `summary`），前端把它渲染成确认卡片，用户点确认后由前端调用
既有的业务接口落库。好处：权限校验、状态机、审计日志都只有一套实现，
AI 拿不到绕过校验的路径。

v2.5 起助手多了一层**只读工具循环**：模型可以先调 `list_invoices` 之类的查询工具，
服务端执行后把结果回灌给它，它再接着问或者给出结论（最多 `AI_TOOL_ROUNDS` 轮）。
写动作在生成卡片之前先过一遍 `ai_tools.prepare_write` 做参数规范化与前置校验，
校验不过就把原因回灌给模型，让它向用户追问——而不是先给一张点下去必然失败的卡片。
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, selectinload

from .. import ai, ai_service, ai_tasks, ai_tools, crypt, models as m, ocr
from .. import security as sec
from ..database import SessionLocal, get_db

router = APIRouter(prefix="/api/ai", tags=["AI 智能"])

_admin = sec.require_roles(m.ROLE_ADMIN)
_staff = sec.require_roles(m.ROLE_ADMIN, m.ROLE_FINANCE)

# 一次提问最多允许模型连续查几轮数据。4 轮足够「先找 id → 再查明细 → 再汇总」，
# 又不至于让一次提问打出十几次调用。
MAX_TOOL_ROUNDS = int(os.getenv("AI_TOOL_ROUNDS", "4") or 4)


# ---------------------------------------------------------------- 模型配置


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    preset: str = "custom"
    base_url: str = ""
    # None = 不修改已有 Key；"" = 清空；其它 = 更新
    api_key: str | None = None
    model: str = ""
    vision_model: str | None = None
    auth_style: str = "bearer"
    extra_headers: str | None = None
    extra_query: str | None = None
    temperature: float = Field(0.3, ge=0, le=2)
    max_tokens: int = Field(2048, ge=16, le=32000)
    timeout_sec: int = Field(60, ge=5, le=600)
    use_assistant: bool = True
    use_recognize: bool = True
    use_analyze: bool = True
    enabled: bool = True
    is_default: bool = False
    remark: str | None = None


def provider_out(row: m.AiProvider) -> dict:
    key = crypt.decrypt(row.api_key_enc) if row.api_key_enc else ""
    return {
        "id": row.id,
        "name": row.name,
        "preset": row.preset,
        "base_url": row.base_url,
        "model": row.model,
        "vision_model": row.vision_model,
        "auth_style": row.auth_style,
        "extra_headers": row.extra_headers,
        "extra_query": row.extra_query,
        "temperature": float(row.temperature or 0),
        "max_tokens": row.max_tokens,
        "timeout_sec": row.timeout_sec,
        "use_assistant": row.use_assistant,
        "use_recognize": row.use_recognize,
        "use_analyze": row.use_analyze,
        "enabled": row.enabled,
        "is_default": row.is_default,
        "remark": row.remark,
        # 明文 Key 永不出后端，只回显掩码与是否已配置
        "has_key": bool(key),
        "key_mask": crypt.mask(key) if key else "",
        "can_vision": bool(row.vision_model),
        "last_test_at": row.last_test_at.isoformat(sep=" ", timespec="seconds") if row.last_test_at else None,
        "last_test_ok": row.last_test_ok,
        "last_test_detail": row.last_test_detail,
        "updated_at": row.updated_at.isoformat(sep=" ", timespec="seconds") if row.updated_at else None,
    }


@router.get("/status", response_model=dict)
def ai_status(db: Session = Depends(get_db), user: m.AppUser = Depends(sec.current_user)):
    """所有登录用户都能看：AI 到底能不能用、识别引擎是哪一档。"""
    row = ai_service.provider_for(db, "assistant")
    providers = db.query(m.AiProvider).filter(m.AiProvider.enabled.is_(True)).count()
    return {
        "configured": row is not None,
        "provider": row.name if row else None,
        "model": row.model if row else None,
        "vision": bool(row.vision_model) if row else False,
        "providers": providers,
        "ocr": ocr.availability(),
        "can_manage": user.role == m.ROLE_ADMIN,
    }


@router.get("/presets", response_model=list[dict], dependencies=[Depends(_admin)])
def list_presets():
    """预设厂商清单，用于界面上「一键填充」。"""
    return ai.PROVIDER_PRESETS


@router.get("/providers", response_model=list[dict], dependencies=[Depends(_admin)])
def list_providers(db: Session = Depends(get_db)):
    rows = db.query(m.AiProvider).order_by(m.AiProvider.id.asc()).all()
    return [provider_out(r) for r in rows]


def _apply_payload(row: m.AiProvider, payload: ProviderIn) -> None:
    data = payload.model_dump(exclude={"api_key"})
    for key, value in data.items():
        setattr(row, key, value)
    if payload.api_key is not None:
        raw = payload.api_key.strip()
        row.api_key_enc = crypt.encrypt(raw) if raw else ""


@router.post("/providers", response_model=dict, status_code=201, dependencies=[Depends(_admin)])
def create_provider(
    payload: ProviderIn,
    request: Request,
    admin: m.AppUser = Depends(_admin),
    db: Session = Depends(get_db),
):
    if db.query(m.AiProvider).filter(m.AiProvider.name == payload.name.strip()).first():
        raise HTTPException(400, "同名配置已存在")
    if not payload.base_url.strip():
        raise HTTPException(400, "请填写接口地址（Base URL）")
    if not payload.model.strip():
        raise HTTPException(400, "请填写模型名")
    row = m.AiProvider(name=payload.name.strip(), created_by=admin.username)
    _apply_payload(row, payload)
    # 第一条自动设为默认，省得用户还要手动点一下
    if not db.query(m.AiProvider).count():
        row.is_default = True
    db.add(row)
    db.commit()
    db.refresh(row)
    _audit(db, admin, request, "新增 AI 模型配置", row.id, f"{row.name} / {row.model}")
    return provider_out(row)


@router.put("/providers/{oid}", response_model=dict, dependencies=[Depends(_admin)])
def update_provider(
    oid: int,
    payload: ProviderIn,
    request: Request,
    admin: m.AppUser = Depends(_admin),
    db: Session = Depends(get_db),
):
    row = db.get(m.AiProvider, oid)
    if not row:
        raise HTTPException(404, "配置不存在")
    dup = db.query(m.AiProvider).filter(m.AiProvider.name == payload.name.strip(), m.AiProvider.id != oid).first()
    if dup:
        raise HTTPException(400, "同名配置已存在")
    _apply_payload(row, payload)
    db.commit()
    _ensure_single_default(db, oid)
    db.refresh(row)
    _audit(db, admin, request, "修改 AI 模型配置", row.id, f"{row.name} / {row.model}")
    return provider_out(row)


@router.delete("/providers/{oid}", dependencies=[Depends(_admin)])
def delete_provider(
    oid: int,
    request: Request,
    admin: m.AppUser = Depends(_admin),
    db: Session = Depends(get_db),
):
    row = db.get(m.AiProvider, oid)
    if not row:
        raise HTTPException(404, "配置不存在")
    name = row.name
    db.delete(row)
    db.commit()
    _ensure_single_default(db)
    _audit(db, admin, request, "删除 AI 模型配置", oid, name)
    return {"ok": True, "deleted": oid}


def _ensure_single_default(db: Session, prefer: int | None = None) -> None:
    """保证「默认」最多一个；没有默认时挑第一个启用的顶上。"""
    rows = db.query(m.AiProvider).order_by(m.AiProvider.id.asc()).all()
    if not rows:
        return
    defaults = [r for r in rows if r.is_default]
    if len(defaults) > 1:
        keep = None
        if prefer:
            keep = next((r for r in defaults if r.id == prefer), None)
        keep = keep or defaults[0]
        for r in defaults:
            r.is_default = r.id == keep.id
    elif not defaults:
        target = next((r for r in rows if r.enabled), rows[0])
        target.is_default = True
    db.commit()


@router.post("/providers/{oid}/test", response_model=dict, dependencies=[Depends(_admin)])
def test_provider(
    oid: int,
    request: Request,
    payload: dict | None = None,
    admin: m.AppUser = Depends(_admin),
    db: Session = Depends(get_db),
):
    """连通性测试。可以带上临时参数，用于「还没保存就先试一下」。"""
    row = db.get(m.AiProvider, oid)
    if not row:
        raise HTTPException(404, "配置不存在")
    body = payload or {}
    if body.get("base_url"):
        row.base_url = str(body["base_url"]).strip()
    if body.get("model"):
        row.model = str(body["model"]).strip()
    if body.get("api_key"):
        row.api_key_enc = crypt.encrypt(str(body["api_key"]).strip())
    if body.get("auth_style"):
        row.auth_style = str(body["auth_style"])
    cfg = ai.provider_to_config(row)
    try:
        result = ai.test_connection(cfg)
        row.last_test_ok = True
        row.last_test_detail = result["message"]
    except ai.AiError as exc:
        result = {"ok": False, "message": str(exc), "latency_ms": 0}
        row.last_test_ok = False
        row.last_test_detail = str(exc)[:500]
    row.last_test_at = datetime.now()
    db.commit()
    ai.record_usage(
        db, user=admin, kind="连通性测试", cfg=cfg, ok=result["ok"],
        usage=result.get("usage"), error="" if result["ok"] else result["message"],
    )
    _audit(db, admin, request, "测试 AI 模型连通性", oid, result["message"][:200])
    return {**result, "provider": provider_out(row)}


@router.get("/usage", response_model=dict, dependencies=[Depends(_admin)])
def ai_usage(days: int = Query(30, ge=1, le=365), db: Session = Depends(get_db)):
    return ai_service.usage_summary(db, days)


# ---------------------------------------------------------------- 助手对话


class ChatIn(BaseModel):
    messages: list[dict] = Field(default_factory=list)
    page: str = ""
    focus: dict | None = None
    provider_id: int | None = None
    stream: bool = True


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _sanitize(messages: list[dict]) -> list[dict]:
    """只保留角色与文本内容，并限制轮数——前端传来的东西不能直接信。"""
    out: list[dict] = []
    for msg in messages[-16:]:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        out.append({"role": role, "content": content[:6000]})
    return out


_READ_LABELS = {t["name"]: t["label"] for t in ai_tools.READ_TOOLS.values()}

# 工具返回给模型时的截断长度。一次查询回几十条 json 会吃掉大量 token，
# 真需要更多就让模型缩小条件再查一次。
TOOL_RESULT_CHARS = int(os.getenv("AI_TOOL_RESULT_CHARS", "9000") or 9000)


def _chat_loop(db: Session, user: m.AppUser, cfg: ai.AiConfig, system: str, history: list[dict]):
    """助手的一轮问答。生成器：先 yield 若干进度事件，最后 yield 一条 `final`。

    循环里做三件事：
    1. 模型输出 ```tool → 服务端执行只读查询 → 结果回灌 → 继续问；
    2. 模型输出 ```action → 走 `prepare_write` 规范化 + 前置校验。通过就收工，
       由前端渲染确认卡片；不通过就把原因回灌，让模型向用户追问；
    3. 两者都没有 → 这就是最终回答。
    """
    ctx = ai_tools.Ctx(db=db, user=user)
    messages = [{"role": "system", "content": system}, *history]
    trace: list[dict] = []
    text = ""
    detail = history[-1]["content"][:160] if history else ""

    for round_no in range(MAX_TOOL_ROUNDS + 1):
        text, usage = ai.chat(cfg, messages, max_tokens=cfg.max_tokens, temperature=0.35)
        ai.record_usage(db, user=user, kind=m.AI_CHAT, cfg=cfg, ok=True, usage=usage, detail=detail)

        tool = ai_tasks.parse_tool(text)
        action = ai_tasks.parse_action(text)

        # ---- 1) 只读查询 ----
        if tool and not action and round_no < MAX_TOOL_ROUNDS:
            label = _READ_LABELS.get(tool["tool"], tool["tool"])
            yield {"type": "progress", "text": f"正在查询：{label}"}
            try:
                result = ai_tools.run_read(ctx, tool["tool"], tool["params"])
                payload = json.dumps(result, ensure_ascii=False, default=str)
                ok = True
            except ai_tools.ToolError as exc:
                payload = json.dumps({"error": str(exc)}, ensure_ascii=False)
                ok = False
            trace.append({
                "tool": tool["tool"], "label": label, "params": tool["params"],
                "ok": ok, "chars": len(payload),
            })
            messages.append({"role": "assistant", "content": text})
            messages.append({
                "role": "user",
                "content": (
                    f"【工具 {tool['tool']} 的返回】\n{payload[:TOOL_RESULT_CHARS]}\n\n"
                    "以上是查询结果（不是用户说的话）。信息够就给出最终回答，"
                    "不够可以再调用一次工具；如果结果里是 error，就换一种查法或如实说明。"
                ),
            })
            continue

        # ---- 2) 写动作：先规范化 + 前置校验 ----
        if action:
            try:
                clean, warns, meta = ai_tools.prepare_write(ctx, action["type"], action["params"])
            except ai_tools.ToolError as exc:
                if round_no < MAX_TOOL_ROUNDS:
                    messages.append({"role": "assistant", "content": text})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"【这个操作没通过前置校验】{exc}\n"
                            "请用一两句话向用户问清楚缺的那一项，然后重新给出动作；"
                            "不要原样重复上一个动作。若这件事系统确实做不到，就直说原因。"
                        ),
                    })
                    yield {"type": "progress", "text": "正在补全校验参数…"}
                    continue
                text = ai_tasks.strip_blocks(text) + f"\n\n（这个操作没能准备好：{exc}）"
                yield {"type": "final", "text": text, "action": None, "trace": trace}
                return
            yield {
                "type": "final",
                "text": ai_tasks.strip_blocks(text),
                "action": {**action, "params": clean, "warnings": warns,
                           "label": meta["label"], "danger": meta["danger"]},
                "trace": trace,
            }
            return

        # ---- 3) 最终回答 ----
        break

    if not text:
        text = "（模型没有返回内容，请重试或检查模型配置）"
    yield {"type": "final", "text": ai_tasks.strip_blocks(text), "action": None, "trace": trace}


@router.post("/chat")
def chat(
    payload: ChatIn,
    request: Request,
    user: m.AppUser = Depends(sec.current_user),
    db: Session = Depends(get_db),
):
    """助手对话。默认流式返回 SSE，便于边生成边显示。

    事件类型：`progress`（正在查询什么）、`delta`（正文增量）、`done`（收尾，带动作）、
    `error`。```tool / ```action 代码块永远不会出现在 delta 里——它们是给程序看的。
    """
    history = _sanitize(payload.messages)
    if not history:
        raise HTTPException(400, "请先输入问题")
    try:
        cfg = ai_service.ensure_provider(db, "assistant", payload.provider_id)
    except ai.AiError as exc:
        raise HTTPException(400, str(exc)) from None

    system = ai_service.system_prompt(db, user, payload.page, payload.focus)

    if not payload.stream:
        final: dict = {"text": "", "action": None, "trace": []}
        try:
            for ev in _chat_loop(db, user, cfg, system, history):
                if ev["type"] == "final":
                    final = ev
        except ai.AiError as exc:
            ai.record_usage(db, user=user, kind=m.AI_CHAT, cfg=cfg, ok=False,
                            error=str(exc), detail=history[-1]["content"][:200])
            raise HTTPException(400, str(exc)) from None
        return {
            "text": final["text"],
            "action": final["action"],
            "trace": final.get("trace") or [],
            "model": cfg.model,
            "provider": cfg.provider_name,
        }

    def generate():
        try:
            for ev in _chat_loop(db, user, cfg, system, history):
                if ev["type"] == "progress":
                    yield _sse({"type": "progress", "text": ev["text"]})
                    continue
                # 正文切片下发，前端能一段段显出来，视觉上仍是「边生成边显示」
                text = ev["text"] or ""
                for block in _chunks(text):
                    yield _sse({"type": "delta", "text": block})
                yield _sse({
                    "type": "done", "text": text, "action": ev["action"],
                    "trace": ev.get("trace") or [],
                    "model": cfg.model, "provider": cfg.provider_name,
                })
                return
        except ai.AiError as exc:
            ai.record_usage(db, user=user, kind=m.AI_CHAT, cfg=cfg, ok=False,
                            error=str(exc), detail=history[-1]["content"][:200])
            yield _sse({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            ai.record_usage(db, user=user, kind=m.AI_CHAT, cfg=cfg, ok=False,
                            error=str(exc)[:300], detail=history[-1]["content"][:200])
            yield _sse({"type": "error", "message": f"助手内部错误：{exc}"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _split_long(piece: str, size: int):
    """把一段超长文本按句末标点切开，切不动就硬切。"""
    out, buf = [], ""
    for ch in piece:
        buf += ch
        if len(buf) >= size and ch in "。！？；，、,.!?;:：\n ":
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    # 极端情况（整段没有一个标点）按长度硬切，保证仍然是多片而不是一整块
    final = []
    for p in out:
        while len(p) > size * 2:
            final.append(p[: size * 2])
            p = p[size * 2:]
        if p:
            final.append(p)
    return final


def _chunks(text: str, size: int = 90):
    """把最终正文切成若干片下发，让前端「一段段显出来」。

    模型（尤其是本地小模型）经常一次性吐完整段：如果整段当一片发出去，
    用户看到的就是「转半天圈 → 啪一下全出来」，流式就白做了。
    所以这里按换行优先、句读次之切成多片；只要正文超过 MIN_SPLIT 个字符，
    就一定至少发两片——「一次性刷出全文」在验收里是要被抓住的。
    """
    MIN_SPLIT = 30
    pieces: list[str] = []
    buf = ""
    for line in text.split("\n"):
        if buf and len(buf) + len(line) + 1 > size:
            pieces.append(buf + "\n")
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        pieces.append(buf)

    out: list[str] = []
    for p in pieces:
        out.extend(_split_long(p, size) if len(p) > size else [p])

    # 兜底：正文不算短却只切出一片，就在「离中点最近的标点」处断开。
    # 宁可切得难看一点，也不要一次性刷出全文——那样流式就失去意义了。
    if len(out) == 1 and len(text) >= MIN_SPLIT:
        mid = len(text) // 2
        cuts = [i for i, ch in enumerate(text)
                if ch in "。！？；\n，、,.!?;:： " and 0 < i < len(text) - 1]
        cut = min(cuts, key=lambda i: abs(i - mid)) if cuts else mid - 1
        out = [text[: cut + 1], text[cut + 1 :]]

    for p in out:
        if p:
            yield p


# ---------------------------------------------------------------- 单据分析


def _load_reimbursement(db: Session, oid: int, user: m.AppUser) -> m.Reimbursement:
    obj = (
        db.query(m.Reimbursement)
        .options(
            selectinload(m.Reimbursement.items).selectinload(m.ReimbursementItem.invoices),
            selectinload(m.Reimbursement.items).selectinload(m.ReimbursementItem.category),
            selectinload(m.Reimbursement.applicant),
            selectinload(m.Reimbursement.department),
            selectinload(m.Reimbursement.customer),
            selectinload(m.Reimbursement.project),
            selectinload(m.Reimbursement.logs),
        )
        .filter(m.Reimbursement.id == oid)
        .first()
    )
    if not obj:
        raise HTTPException(404, "报销单不存在")
    sec.ensure_can_view(user, obj)
    return obj


@router.post("/analyze/{oid}", response_model=dict)
def analyze(
    oid: int,
    request: Request,
    user: m.AppUser = Depends(sec.current_user),
    db: Session = Depends(get_db),
):
    """对一张报销单做合规与风险分析。结果只读，不改变任何数据。"""
    obj = _load_reimbursement(db, oid, user)
    try:
        cfg = ai_service.ensure_provider(db, "analyze")
    except ai.AiError as exc:
        raise HTTPException(400, str(exc)) from None
    payload = ai_service.reimbursement_payload(obj)
    payload["系统已算出的异常提示"] = _alerts_for(db, obj)
    try:
        got = ai_tasks.analyze_reimbursement(cfg, payload)
    except ai.AiError as exc:
        ai.record_usage(db, user=user, kind=m.AI_ANALYZE, cfg=cfg, ok=False, error=str(exc), detail=obj.code)
        raise HTTPException(400, str(exc)) from None
    ai.record_usage(db, user=user, kind=m.AI_ANALYZE, cfg=cfg, ok=True, usage=got["usage"], detail=obj.code)
    return {
        "code": obj.code,
        "summary": got["summary"],
        "level": got["level"],
        "findings": got["findings"],
        "suggestions": got["suggestions"],
        "model": cfg.model,
    }


@router.post("/approval/{oid}", response_model=dict)
def approval_advice(
    oid: int,
    request: Request,
    user: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN, m.ROLE_APPROVER)),
    db: Session = Depends(get_db),
):
    """给审批人一份参考意见。**不代替审批**，也不会自动审批。"""
    obj = _load_reimbursement(db, oid, user)
    try:
        cfg = ai_service.ensure_provider(db, "analyze")
    except ai.AiError as exc:
        raise HTTPException(400, str(exc)) from None
    payload = ai_service.reimbursement_payload(obj)
    payload["系统已算出的异常提示"] = _alerts_for(db, obj)
    payload["审批人视角"] = {
        "审批人": user.name,
        "角色": user.role,
        "当前待批级别": obj.approved_level + 1,
        "需要级别": obj.required_level,
    }
    try:
        got = ai_tasks.approval_advice(cfg, payload)
    except ai.AiError as exc:
        ai.record_usage(db, user=user, kind=m.AI_APPROVAL, cfg=cfg, ok=False, error=str(exc), detail=obj.code)
        raise HTTPException(400, str(exc)) from None
    ai.record_usage(db, user=user, kind=m.AI_APPROVAL, cfg=cfg, ok=True, usage=got["usage"], detail=obj.code)
    return {
        "code": obj.code,
        "recommend": got["recommend"],
        "confidence": got["confidence"],
        "reasons": got["reasons"],
        "risks": got["risks"],
        "questions": got["questions"],
        "model": cfg.model,
        "disclaimer": "以上为模型给出的参考意见，最终决定权在审批人。",
    }


def _alerts_for(db: Session, obj: m.Reimbursement) -> list[str]:
    """把系统已经算出来的异常点一并交给模型，避免它重复劳动或漏看。"""
    out: list[str] = []
    from .. import approvals as ap

    items = list(obj.items or [])
    invoices = [v for it in items for v in it.invoices]
    if items and not invoices:
        out.append("该单没有任何关联发票")
    if invoices:
        inv_sum = round(sum(float(v.amount or 0) for v in invoices), 2)
        if abs(inv_sum - float(obj.total_amount or 0)) > 0.01:
            out.append(f"发票金额合计 {inv_sum:,.2f} 与单据金额 {float(obj.total_amount or 0):,.2f} 不一致")
        dup = [v for v in invoices if v.check_result and "重复" in str(v.check_result)]
        if dup:
            out.append(f"有 {len(dup)} 张发票被标记为疑似重复")
        bad = [v for v in invoices if v.check_status != m.CHECK_OK]
        if bad:
            out.append(f"有 {len(bad)} 张发票未查验或查验异常")
    threshold = ap.get_float(db, "large_amount_threshold", 20000.0)
    if threshold and float(obj.total_amount or 0) > threshold:
        out.append(f"金额超过大额预警线 {threshold:,.2f}")
    if obj.submit_at:
        days = (datetime.now() - obj.submit_at).days
        overdue = ap.get_int(db, "approval_overdue_days", 7)
        if obj.status == m.ST_PENDING and days > overdue:
            out.append(f"已提交 {days} 天仍未审批完（超期线 {overdue} 天）")
    return out


# ---------------------------------------------------------------- AI 记账


class BookkeepingIn(BaseModel):
    invoice_ids: list[int] = Field(default_factory=list)
    note: str = ""


@router.post("/bookkeeping", response_model=dict)
def bookkeeping(
    payload: BookkeepingIn,
    request: Request,
    user: m.AppUser = Depends(_staff),
    db: Session = Depends(get_db),
):
    """AI 记账：把选中的散票组织成报销单草稿方案。

    只产出方案，不落库。前端确认后调用常规的创建报销单 / 关联发票接口执行。
    """
    ids = [int(i) for i in (payload.invoice_ids or []) if str(i).strip().lstrip("-").isdigit()]
    if not ids:
        raise HTTPException(400, "请先勾选要记账的发票")
    if len(ids) > 50:
        raise HTTPException(400, "单次最多处理 50 张发票")
    rows = (
        db.query(m.Invoice)
        .options(selectinload(m.Invoice.category))
        .filter(m.Invoice.id.in_(ids), *sec.invoice_scope_conds(user))
        .all()
    )
    if not rows:
        raise HTTPException(404, "没有找到可用的发票")
    unlinked = [v for v in rows if v.reimbursement_id is None]
    if not unlinked:
        raise HTTPException(400, "所选发票都已关联报销单，无需记账")
    try:
        cfg = ai_service.ensure_provider(db, "analyze")
    except ai.AiError as exc:
        raise HTTPException(400, str(exc)) from None
    data = ai_service.bookkeeping_payload(db, unlinked)
    if payload.note:
        data["用户补充说明"] = payload.note[:500]
    try:
        got = ai_tasks.bookkeeping_plan(cfg, data)
    except ai.AiError as exc:
        ai.record_usage(db, user=user, kind=m.AI_BOOKKEEPING, cfg=cfg, ok=False,
                        error=str(exc), detail=f"{len(unlinked)} 张票")
        raise HTTPException(400, str(exc)) from None
    ai.record_usage(db, user=user, kind=m.AI_BOOKKEEPING, cfg=cfg, ok=True, usage=got["usage"],
                    detail=f"{len(unlinked)} 张票 -> {len(got['drafts'])} 张单")
    invoice_map = {v.id: v for v in unlinked}
    for draft in got["drafts"]:
        draft["total_amount"] = round(sum(r["amount"] for r in draft["items"]), 2)
        draft["invoices"] = [
            {
                "id": r["invoice_id"],
                "invoice_no": invoice_map[r["invoice_id"]].invoice_no,
                "seller_name": invoice_map[r["invoice_id"]].seller_name,
            }
            for r in draft["items"]
            if r["invoice_id"] in invoice_map
        ]
    return {
        "drafts": got["drafts"],
        "skipped": got["skipped"],
        "invoice_count": len(unlinked),
        "model": cfg.model,
        "disclaimer": "以下为模型给出的编制方案，保存前请核对金额与费用类型。",
    }


# ---------------------------------------------------------------- 其它


@router.get("/actions", response_model=dict)
def list_actions(user: m.AppUser = Depends(sec.current_user)):
    """助手可起草的动作目录 + 只读工具目录，按当前角色裁剪。

    前端拿它渲染确认卡片的标题（label）与危险标记（danger），避免「动作清单」在
    前后端各维护一份、改一边忘一边。
    """
    return {
        "actions": [
            {
                "type": a["type"],
                "label": a["label"],
                "group": a["group"],
                "danger": a["danger"],
                "roles": list(a["roles"]),
                "params": a["params"],
                "note": a["note"],
                # 前端执行时走的既有接口，写在这里是为了让「谁来执行」一目了然
                "executor": "frontend",
            }
            for a in ai_tools.actions_for(user)
        ],
        "tools": [
            {"name": t["name"], "label": t["label"], "desc": t["desc"]}
            for t in ai_tools.tools_for(user)
        ],
        "tool_rounds": MAX_TOOL_ROUNDS,
    }


def _audit(db: Session, user: m.AppUser, request: Request, action: str, entity_id, detail: str = "") -> None:
    db.add(
        m.AuditLog(
            user_id=user.id, username=user.username, role=user.role,
            action=action, entity="ai_provider",
            entity_id=str(entity_id) if entity_id is not None else None,
            method=request.method, path=request.url.path, status_code=200,
            ip=sec._client_ip(request), detail=(detail or "")[:500],
        )
    )
    db.commit()
