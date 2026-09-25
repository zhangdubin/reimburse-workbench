"""本地 Mock 大模型：一个最小的 OpenAI 兼容服务，用于端到端验证 AI 链路。

为什么需要它：真实厂商 Key 不能在测试里用，而 AI 这块的坑几乎都在「协议细节」上
（流式 SSE 分片、JSON 模式、多模态消息体、错误体结构）。用一个能按需返回固定内容的
假服务，就能把这些细节全部覆盖住，且完全离线、可重复。

用法：
    python mock_llm.py 8899                  # 起服务（只监听 127.0.0.1）
    # 然后在系统里把 Base URL 配成 http://127.0.0.1:8899/v1

    python mock_llm.py 8899 0.0.0.0           # 应用跑在容器里时用这个
    # 容器里的 127.0.0.1 是它自己，连不到宿主的 mock：
    #   mock 绑到宿主内网地址，Base URL 配成 http://<宿主IP>:8899/v1，
    #   并用 WB_MOCK_LLM=http://<宿主IP>:8899/v1 跑 ai_test.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 按「系统提示词里出现的特征词」返回对应场景的假数据，
# 这样一次 mock 就能同时服务识别、分类、分析、审批、记账多个任务。
# 助手那条放最前面：它的系统提示词里会带上完整的工具/动作目录，
# 目录文本难免蹭到别的场景的相似词，先匹配就不会被抢走。
RESPONSES: list[tuple[re.Pattern, object]] = [
    # 助手对话用动态构造：要能演「先查数据 → 再给结论/动作」的多轮循环，
    # 以及「参数不够 → 被服务端打回 → 反过来追问用户」这条纠错路径。
    (re.compile("你是「销售费用报销管理工作台」内置的智能助手"), "assistant"),
    (
        re.compile("发票的信息提取专家"),
        {
            "invoice_no": "24417000000099990001",
            "invoice_code": None,
            "invoice_type": "数电票",
            "amount": 2680.00,
            "tax_amount": 308.32,
            "tax_rate": 13,
            "invoice_date": "2026-09-10",
            "seller_name": "深圳市云图科技有限公司",
            "seller_tax_no": "91440300MA5F1234XA",
            "buyer_name": "深圳市智联云创科技有限公司",
            "buyer_tax_no": "91440300MA5G5678XB",
            "confidence": 0.93,
            "note": "由 Mock 模型返回，用于链路验证",
        },
    ),
    (
        re.compile("费用类型归类助手"),
        {
            "items": [
                {"index": 0, "category": "差旅费", "confidence": 0.92, "reason": "含交通与住宿"},
                {"index": 1, "category": "业务招待费", "confidence": 0.71, "reason": "餐饮消费"},
                {"index": 2, "category": "不存在的类型", "confidence": 0.9, "reason": "应被过滤掉"},
            ]
        },
    ),
    (
        re.compile("合规与风控分析助手"),
        {
            "summary": "9 月深圳客户拜访差旅费，金额 2,680.00 元，目前处于待审批状态。",
            "level": "关注",
            "findings": [
                {"title": "发票金额与单据金额一致", "detail": "两张发票合计与单据金额吻合。", "level": "正常"},
                {"title": "存在一张未查验发票", "detail": "发票 24417000000099990001 仍是未查验状态。", "level": "关注"},
            ],
            "suggestions": ["先完成发票查验再提交审批", "确认出差审批单是否已归档"],
        },
    ),
    (
        re.compile("协助审批人把关"),
        {
            "recommend": "需核实",
            "confidence": 0.68,
            "reasons": ["金额未超过大额预警线", "发票张数与明细行一致"],
            "risks": ["有一张发票未查验"],
            "questions": ["本次出差是否有事前审批单？"],
        },
    ),
    # 记账场景用动态构造：必须回显请求里真实存在的发票 id，
    # 否则服务端会因为「id 不在本次输入里」把草稿整条丢掉，
    # 测试就变成了「断言一个空列表」——看着绿，其实什么都没验。
    (re.compile("报销单编制助手"), "bookkeeping"),
]

def _bookkeeping_reply(messages: list[dict]) -> str:
    """按请求里真实的发票清单编一张草稿，回显真实 id 与金额。"""
    payload = {}
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                payload = {}
            if payload:
                break
    invoices = payload.get("invoices") or []
    cats = payload.get("categories") or []
    cat_id = cats[0]["id"] if cats else None
    rows = [
        {
            "invoice_id": v.get("id"),
            "category_id": cat_id,
            "occur_date": v.get("invoice_date") or "2026-09-10",
            "amount": v.get("amount") or 0,
            "tax_amount": v.get("tax_amount") or 0,
            "description": f"{v.get('seller_name') or '供应商'} 的票据",
        }
        for v in invoices
    ]
    return json.dumps(
        {
            "drafts": [
                {
                    "title": f"{rows[0]['description'] if rows else '票据'}等 {len(rows)} 张",
                    "purpose": "由 Mock 模型按票据销售方与金额归集",
                    "department_id": None,
                    "customer_id": None,
                    "project_id": None,
                    "items": rows,
                    "note": "Mock 返回，用于链路验证",
                }
            ] if rows else [],
            "skipped": [],
        },
        ensure_ascii=False,
    )


DEFAULT_TEXT = (
    "这是 Mock 模型的回复。\n\n"
    "系统里「发票管理」页可以登记发票并上传识别；"
    "「报销单管理」页支持新建、提交与审批。\n\n"
    "```action\n"
    '{"type": "create_reimbursement", "params": {"title": "测试单"}, '
    '"summary": "将新建一张名为「测试单」的报销单草稿"}\n'
    "```\n"
)


# ------------------------------------------------------------------ 助手场景

# 新建那段刻意用**名字**而不是 id：这样能顺带验到服务端的
# 「名称 → id 解析」与「申请人缺省取登录账号绑定的员工」两条逻辑。
WRITE_PARAMS = {
    "title": "XTS 招待晚餐",
    "purpose": "招待 XTS 客户 5 人晚餐",
    "applicant": "张斌",
    "customer": "XTS",
    "occur_start": "2026-09-19",
    "occur_end": "2026-09-19",
    "items": [
        {
            "category": "商务宴请",
            "occur_date": "2026-09-19",
            "amount": 1765,
            "tax_amount": 0,
            "description": "晚餐招待 XTS 等人",
        }
    ],
    "invoice_ids": [],
}

# 「参数不够」那条路径用的：只有标题，没有明细
THIN_PARAMS = {"title": "招待晚餐", "items": []}


def _last_user(messages: list[dict]) -> str:
    """取最后一条真正的用户发言（跳过服务端回灌的工具结果/校验提示）。"""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if "【工具 " in content or "【这个操作没通过前置校验】" in content:
                continue
            return content
        if isinstance(content, list):  # 多模态
            return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _loop_state(messages: list[dict]) -> str:
    """看服务端刚回灌了什么：'tool'（工具结果）| 'reject'（校验没过）| ''（首轮）。"""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        if "【工具 " in content:
            return "tool"
        if "【这个操作没通过前置校验】" in content:
            return "reject"
        return ""
    return ""


def _assistant_reply(messages: list[dict]) -> str:
    """助手场景的假回复：演一遍工具循环与校验回灌。

    真实模型自己决定「先查再答」，这里用关键词固定成两条路径——测试要覆盖的是
    链路，不是考察模型智商；固定下来断言才能写死。
    """
    last = _last_user(messages)
    state = _loop_state(messages)

    # ① 前置校验没过：像人一样把缺的那一项问清楚，而不是原样重发动作。
    #    这条路径正是「助手不够智能」的原始症状，必须能被测到。
    if state == "reject":
        return (
            "这张单还差**费用明细**，没有它建出来是废单。\n\n"
            "把费用类型、金额和发生日期告诉我，比如「商务宴请 1765 元，9 月 19 日」。"
        )

    # ② 首轮：先查库，别急着说「我没有这类数据」。
    if not state:
        if "建一张" in last or "新建报销" in last or "起草" in last:
            body = {"tool": "list_master", "params": {"entity": "categories", "limit": 50}}
        elif "未关联" in last or "散票" in last:
            body = {"tool": "list_invoices", "params": {"unlinked_only": True}}
        elif "待审批" in last:
            body = {"tool": "list_reimbursements", "params": {"status": "待审批"}}
        else:
            body = {"tool": "get_stats", "params": {"kind": "overview"}}
        return (
            "我先去系统里查一下。\n\n"
            "```tool\n" + json.dumps(body, ensure_ascii=False) + "\n```\n"
        )

    # ③ 拿到数据了：给结论；写请求则附上可执行方案。
    if "建一张" in last or "新建报销" in last or "起草" in last:
        if "缺明细" in last:
            # 故意漏掉 items：留给服务端打回，验「追问而不是摆烂」
            params = THIN_PARAMS
            summary = "新建一张只有标题、没有明细的单子"
        elif "不带申请人" in last or "不提申请人" in last:
            # 故意不写 applicant：验「申请人默认取登录账号绑定的员工」
            params = {k: v for k, v in WRITE_PARAMS.items() if k != "applicant"}
            summary = "新建一张 1,765.00 元的招待餐费草稿单（不指定申请人）"
        else:
            params = WRITE_PARAMS
            summary = "新建一张 1,765.00 元的招待餐费草稿单，客户 XTS"
        return (
            "参数齐了，下面是方案，你确认后才会落库。\n\n"
            "```action\n"
            + json.dumps(
                {"type": "create_reimbursement", "params": params, "summary": summary},
                ensure_ascii=False,
            )
            + "\n```\n"
        )
    if "未关联" in last or "散票" in last:
        return "查完了：未关联的散票就是查询结果里那几张，补个费用类型就能挂到报销单上。"
    if "待审批" in last:
        return "这些就是当前待审批的单子，按金额和提交时间排一下就能定优先级。"
    return "这是当前看板的概览口径，本月金额、环比与预算执行都在查询结果里。"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音，别刷屏
        pass

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _reply_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        body = self._read_body()
        auth = self.headers.get("Authorization") or self.headers.get("api-key") or ""
        if "bad-key" in auth:
            return self._reply_json(
                {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}}, 401
            )
        messages = body.get("messages") or []
        system = ""
        for msg in messages:
            if msg.get("role") == "system":
                content = msg.get("content")
                system = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                break

        text = None
        for pattern, payload in RESPONSES:
            if not pattern.search(system):
                continue
            # payload 为 None 表示这个场景要按请求内容动态构造
            if payload == "assistant":
                text = _assistant_reply(messages)
            elif payload == "bookkeeping":
                text = _bookkeeping_reply(messages)
            else:
                text = json.dumps(payload, ensure_ascii=False)
            break
        if text is None:
            text = DEFAULT_TEXT

        if body.get("stream"):
            return self._reply_stream(text)
        return self._reply_json({
            "id": "mock-1",
            "object": "chat.completion",
            "model": body.get("model") or "mock-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180},
        })

    def _reply_stream(self, text: str) -> None:
        """按字符切片下发，模拟真实厂商的分片行为（也包括跨片的 ```action 标记）。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(data: str) -> None:
            payload = ("data: " + data + "\n\n").encode("utf-8")
            self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")

        step = 7
        for i in range(0, len(text), step):
            piece = text[i : i + step]
            chunk(json.dumps(
                {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]},
                ensure_ascii=False,
            ))
            time.sleep(0.002)
        chunk(json.dumps(
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180}},
            ensure_ascii=False,
        ))
        chunk("[DONE]")
        self.wfile.write(b"0\r\n\r\n")


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    # 第二个参数是绑定地址。默认只听回环（本机直跑最安全）。
    # 要在**容器里**跑的应用也能连上 mock 时（app 在容器、mock 在宿主），
    # 得绑到宿主内网地址，并把 WB_MOCK_LLM 指成 http://<宿主IP>:<port>/v1 ——
    # 容器里的 127.0.0.1 是它自己，连不到宿主。
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    server = ThreadingHTTPServer((host, port), Handler)
    shown = "127.0.0.1" if host in ("127.0.0.1", "localhost") else host
    print(f"Mock 大模型已启动：http://{shown}:{port}/v1（监听 {host}:{port}）")
    server.serve_forever()


if __name__ == "__main__":
    main()
