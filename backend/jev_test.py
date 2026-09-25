"""Jev 决策网关客户端测试（v2.9.1）

这套用例的由来：v2.7.9 上线时把 Jev 的 HTTP 协议整段实现错了——端点是
`/v1/ai/jev`（不存在，恒 404）、questions 写成数组、字段叫 question/options、
类型名用 Noul。因为调用失败时异常被吞掉、日志里什么都不留，管理员在界面上
只能看到一句「连通性测试失败」，排查无从下手。

所以这里用**本地假网关**把协议钉死：不联网、不花 token，专门盯住
  - 端点必须是 /v1/evaluate
  - questions 必须是对象、字段必须叫 instructions、choice 的 criteria 必须是对象、
    score 的 criteria 必须是数组、题型判别符必须是小写 boolean/choice/score
  - 响应解析：answers 按题 id 取，confidence 在 providerMetadata
  - 失败必须留痕（状态码 + 服务端 message + 可读建议），不能再静默
跑法：
    python jev_test.py
"""
import json
import os
import pathlib
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="jev_test_"))
os.environ["DATABASE_URL"] = "sqlite:///" + str(TMP / "t.db")
os.environ.pop("JEV_API_KEY", None)  # 确保走 DB 配置，避免 env 干扰
# 假网关跑在回环上：必须绕开 HTTP_PROXY，否则请求会被本机代理接管（拿到 502 而不是连通）
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"

from app import database as dbm  # noqa: E402
from app import jev_client as jc  # noqa: E402
from app import models as m  # noqa: E402

OK = 0
FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        FAIL += 1
        print(f"  \033[31m✗\033[0m {name}" + (f"  —— {extra}" if extra else ""))


def group(title):
    print(f"\n\033[1m{title}\033[0m")


# --------------------------------------------------------------- 本地假网关

class Gateway(BaseHTTPRequestHandler):
    """假 Vercel AI Gateway：记录请求，按 scenario 返回预设响应。"""

    scenario = {"status": 200, "body": {"answers": {"q": {"choice": "餐饮"}}}}
    captured: list = []
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {"__unparsable__": raw[:200]}
        Gateway.captured.append({
            "path": self.path,
            "auth": self.headers.get("Authorization") or "",
            "ctype": self.headers.get("Content-Type") or "",
            "body": body,
        })
        sc = Gateway.scenario
        payload = sc["body"]
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        data = text.encode("utf-8")
        self.send_response(sc["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # 静音
        pass


def start_gateway():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def set_scenario(status, body):
    Gateway.scenario = {"status": status, "body": body}
    Gateway.captured.clear()


def set_cfg(db, key="vck_test_key_123456", base_url="", timeout="8"):
    """写配置并让 5s 缓存立即失效。"""
    for k, v in (("jev_key", key), ("jev_base_url", base_url), ("jev_timeout_sec", timeout)):
        row = db.get(m.Setting, k)
        if row is None:
            db.add(m.Setting(key=k, value=v, remark=""))
        else:
            row.value = v
    db.commit()
    jc.invalidate_jev_config_cache()


# ------------------------------------------------------------------- 用例

def test_evaluate_url():
    group("端点规范化")
    cases = [
        ("https://ai-gateway.vercel.sh/v1", "https://ai-gateway.vercel.sh/v1/evaluate"),
        ("https://ai-gateway.vercel.sh", "https://ai-gateway.vercel.sh/v1/evaluate"),
        # 历史错误默认值必须被纠正（这是当初 404 的根因）
        ("https://ai-gateway.vercel.sh/v1/ai/jev", "https://ai-gateway.vercel.sh/v1/evaluate"),
        ("https://ai-gateway.vercel.sh/v1/evaluate", "https://ai-gateway.vercel.sh/v1/evaluate"),
        ("https://ai-gateway.vercel.sh/", "https://ai-gateway.vercel.sh/v1/evaluate"),
        ("http://127.0.0.1:9999", "http://127.0.0.1:9999/v1/evaluate"),
        ("http://127.0.0.1:9999/v1", "http://127.0.0.1:9999/v1/evaluate"),
    ]
    for src, want in cases:
        got = jc.evaluate_url(src)
        check(f"{src} → {want}", got == want, got)
    check("空值回落官方默认", jc.evaluate_url("") == "https://ai-gateway.vercel.sh/v1/evaluate")


def test_choice(db, port):
    group("choice：端点 / 请求体 / 解析")
    set_scenario(200, {"answers": {"q": {"choice": "餐饮"}},
                       "providerMetadata": {"typesafe": {"confidence": {"q": 0.91}}}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    ans, conf = jc.jev_choice("属于哪类？", ["餐饮", "交通"], context="发票内容：晚餐招待客户", db=db)

    check("答案取到", ans == "餐饮", repr(ans))
    check("confidence 取自 providerMetadata", abs(conf - 0.91) < 1e-6, conf)

    req = Gateway.captured[-1]
    check("端点路径是 /v1/evaluate", req["path"] == "/v1/evaluate", req["path"])
    check("带 Bearer 鉴权", req["auth"].startswith("Bearer "), req["auth"])
    b = req["body"]
    check("model 是 typesafe-ai/jev", b.get("model") == "typesafe-ai/jev", b.get("model"))
    check("上下文放在 state", b.get("state") == "发票内容：晚餐招待客户", b.get("state"))
    check("questions 是对象而非数组", isinstance(b.get("questions"), dict), type(b.get("questions")).__name__)
    q = b["questions"]["q"]
    check("每题不再用 question 字段（改为 instructions）", "question" not in q)
    check("题型判别符是小写 choice", q.get("type") == "choice", q.get("type"))
    check("题目文本在 instructions", q.get("instructions") == "属于哪类？", q.get("instructions"))
    check("choice 的 criteria 是对象", isinstance(q.get("criteria"), dict), type(q.get("criteria")).__name__)
    check("criteria 覆盖全部选项", set(q["criteria"]) == {"餐饮", "交通"}, q.get("criteria"))
    check("不再出现 options 字段（写成数组会被网关拒）", "options" not in q)

    # 描述式选项（选项 key 与判据分离）
    set_scenario(200, {"answers": {"q": {"choice": "meal"}}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    ans2, _ = jc.jev_choice("归类", {"meal": "餐饮与招待", "traffic": "交通出行"}, db=db)
    check("dict 形式选项返回 key", ans2 == "meal", repr(ans2))
    check("描述进 criteria", Gateway.captured[-1]["body"]["questions"]["q"]["criteria"]["meal"] == "餐饮与招待")

    # 模型回描述文本时的兜底
    set_scenario(200, {"answers": {"q": {"choice": "交通出行"}}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    ans3, _ = jc.jev_choice("归类", {"meal": "餐饮与招待", "traffic": "交通出行"}, db=db)
    check("回描述文本也能映射回 key", ans3 == "traffic", repr(ans3))


def test_boolean_and_score(db, port):
    group("boolean / score：题型判别符与 criteria 形态")
    set_scenario(200, {"answers": {"q": {"probability": 0.87}}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    p = jc.jev_noul("是否重复？", context="两笔明细", db=db)
    check("boolean 概率解析", abs(p - 0.87) < 1e-6, p)
    q = Gateway.captured[-1]["body"]["questions"]["q"]
    check("题型判别符是小写 boolean（不是 Noul）", q.get("type") == "boolean", q.get("type"))
    check("boolean 不需要 criteria", "criteria" not in q)

    set_scenario(200, {"answers": {"q": {"score": 4}}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    s = jc.jev_score("多可疑？", criteria=["正常", "轻微", "可疑", "高度可疑"], db=db)
    check("score 解析", abs(s - 4.0) < 1e-6, s)
    q = Gateway.captured[-1]["body"]["questions"]["q"]
    check("题型判别符是小写 score", q.get("type") == "score", q.get("type"))
    check("score 的 criteria 是数组（与 choice 相反）", isinstance(q.get("criteria"), list), type(q.get("criteria")).__name__)

    # 不传 criteria 时按 1~N 生成
    set_scenario(200, {"answers": {"q": {"score": 3}}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    jc.jev_score("打分", scale_min=1, scale_max=5, db=db)
    q = Gateway.captured[-1]["body"]["questions"]["q"]
    check("按 scale 生成 5 档判据", q["criteria"] == ["1", "2", "3", "4", "5"], q.get("criteria"))

    # 过界概率被夹到 0~1
    set_scenario(200, {"answers": {"q": {"probability": 1.7}}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    check("概率过界被夹到 1.0", jc.jev_noul("越界", db=db) == 1.0)


def test_response_robustness(db, port):
    group("响应解析的宽容度")
    set_scenario(200, {"answers": {"q": {"value": "办公用品"}}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    ans, _ = jc.jev_choice("归类", ["办公用品", "差旅费"], db=db)
    check("字段名换成 value 也能取到", ans == "办公用品", repr(ans))

    set_scenario(200, {"answers": {"q": {"choice": "交通"}}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    _, conf = jc.jev_choice("归类", ["交通"], db=db)
    check("缺 confidence 时回落 0.0", conf == 0.0, conf)

    set_scenario(200, {"answers": {}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    ans, _ = jc.jev_choice("归类", ["交通"], db=db)
    check("题 id 找不到时返回 None 而不是瞎猜", ans is None, repr(ans))

    set_scenario(200, "这不是 JSON")
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    ans, _ = jc.jev_choice("归类", ["交通"], db=db)
    check("非 JSON 响应被识别为失败", ans is None)
    check("失败原因带上原文片段", "合法 JSON" in jc.last_error().get("message", ""), jc.last_error())

    set_scenario(200, {"foo": 1})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    ans, _ = jc.jev_choice("归类", ["交通"], db=db)
    check("缺 answers 字段被识别为失败", ans is None)
    check("错误信息点明缺 answers", "answers" in jc.last_error().get("message", ""), jc.last_error())


def test_error_surfacing(db, port):
    group("失败必须留痕（这是当初最痛的地方）")
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")

    set_scenario(401, {"error": {"message": "Invalid API key provided", "type": "invalid_api_key"}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    ans, _ = jc.jev_choice("归类", ["交通"], db=db)
    err = jc.last_error()
    check("调用返回失败", ans is None)
    check("记下 HTTP 状态码", err.get("status") == 401, err)
    check("带回服务端原文", "Invalid API key" in err.get("message", ""), err)
    check("给出可读建议", "重新生成" in err.get("message", ""), err)

    set_scenario(403, {"error": {"message": "AI Gateway requires a valid credit card on file to service requests."}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    jc.jev_choice("归类", ["交通"], db=db)
    msg = jc.last_error().get("message", "")
    check("403 提示绑卡（真实踩到的坑）", "信用卡" in msg, msg)

    set_scenario(404, {"error": {"message": "The requested resource was not found: /v1/evaluate"}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    jc.jev_choice("归类", ["交通"], db=db)
    msg = jc.last_error().get("message", "")
    check("404 提示端点路径", "/v1/evaluate" in msg, msg)

    set_scenario(400, {"error": {"message": "questions.q.criteria: Invalid input: expected record"}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    jc.jev_choice("归类", ["交通"], db=db)
    msg = jc.last_error().get("message", "")
    check("400 提示请求体协议", "Jev 协议" in msg, msg)

    set_scenario(500, {"error": {"message": "internal error"}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    jc.jev_choice("归类", ["交通"], db=db)
    check("5xx 提示稍后重试", "稍后重试" in jc.last_error().get("message", ""), jc.last_error())

    # 网络不可达
    set_cfg(db, base_url="http://127.0.0.1:1")  # 必然拒连
    jc.jev_choice("归类", ["交通"], db=db)
    check("连不上时提示网络不可达", "网络不可达" in jc.last_error().get("message", ""), jc.last_error())

    set_scenario(400, {"error": {"message": "按协议描述，应给模型判据"}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    ans, _ = jc.jev_choice("归类", [], db=db)
    check("选项为空时直接拦截，不发请求", ans is None and "至少一个选项" in jc.last_error().get("message", ""))


def test_unconfigured(db, port):
    group("未配置 key 时优雅降级")
    set_cfg(db, key="", base_url=f"http://127.0.0.1:{port}")
    check("jev_available=False", not jc.jev_available(db))
    ans, conf = jc.jev_choice("归类", ["交通"], db=db)
    check("choice 返回 None", ans is None and conf == 0.0)
    check("score 返回 -1", jc.jev_score("打分", db=db) == -1.0)
    check("boolean 返回 -1", jc.jev_noul("是否", db=db) == -1.0)
    check("给出配置指引", "jev_key" in jc.last_error().get("message", ""), jc.last_error())

    r = jc.jev_probe(db)
    check("probe 报未配置", r.get("ok") is False and "jev_key" in r.get("error", ""), r)

    set_cfg(db, key="vck_test_key_123456", base_url=f"http://127.0.0.1:{port}")
    check("填上 key 后 available=True", jc.jev_available(db))


def test_probe(db, port):
    group("连通性测试（probe）")
    set_scenario(200, {"answers": {"ping": {"probability": 0.99}},
                       "providerMetadata": {"typesafe": {"confidence": {"ping": 0.95}}}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}", timeout="12")
    r = jc.jev_probe(db)
    check("ok=True", r.get("ok") is True, r)
    check("带上规范化端点", r.get("url") == f"http://127.0.0.1:{port}/v1/evaluate", r.get("url"))
    check("带上模型名", r.get("model") == "typesafe-ai/jev", r.get("model"))
    check("带上耗时", isinstance(r.get("latency_ms"), int), r.get("latency_ms"))
    check("带上答案", r.get("answer") == 0.99, r.get("answer"))
    check("带上 confidence", abs(float(r.get("confidence") or 0) - 0.95) < 1e-6, r)
    check("发的是 boolean（最省）", Gateway.captured[-1]["body"]["questions"]["ping"]["type"] == "boolean")

    set_scenario(403, {"error": {"message": "requires a valid credit card on file"}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}", timeout="12")
    r = jc.jev_probe(db)
    check("失败时 ok=False 且不抛异常", r.get("ok") is False, r)
    check("失败时带状态码", r.get("status") == 403, r.get("status"))
    check("失败时带错误原文", "credit card" in r.get("error", ""), r.get("error"))
    check("失败时带 hint", "信用卡" in (r.get("hint") or ""), r.get("hint"))

    set_scenario(200, {"answers": {"ping": {"probability": 0.5}}})
    set_cfg(db, base_url=f"http://127.0.0.1:{port}", timeout="12")
    r = jc.jev_probe(db)
    check("成功后再测仍 OK（错误状态已清零）", r.get("ok") is True and not jc.last_error(), r)


def test_config_cache(db, port):
    group("配置缓存与 db 缺失兜底")
    set_cfg(db, base_url=f"http://127.0.0.1:{port}")
    # 回归防线：调用链上漏传 db 时，也必须能读到 admin 在界面里配的值。
    # 这个坑真实发生过——早期 _cfg(db=None) 直接忽略库配置，于是「漏传 db」= 静默
    # 退化成「未配置 key」，界面上只看到一句「测试失败」，查了半天。
    jc.invalidate_jev_config_cache()
    check("不传 db 也能读到库里的配置", jc.jev_available())
    jc.invalidate_jev_config_cache()
    check("不传 db 也能读到基址", jc._cfg(None)[1] == f"http://127.0.0.1:{port}", jc._cfg(None)[1])

    jc.jev_available(db)
    # 直接改库但不让缓存失效：5 秒内仍读旧值（避免每次调用都查库）
    db.get(m.Setting, "jev_key").value = ""
    db.commit()
    check("缓存期内仍读到旧值", jc.jev_available(db))
    jc.invalidate_jev_config_cache()
    check("失效后立刻读到新值", not jc.jev_available(db))
    set_cfg(db, key="vck_test_key_123456", base_url=f"http://127.0.0.1:{port}")


def main():
    m.Base.metadata.create_all(bind=dbm.engine)
    db = dbm.SessionLocal()
    srv, port = start_gateway()
    print(f"\033[2m假网关：http://127.0.0.1:{port}/v1/evaluate\033[0m")
    try:
        test_evaluate_url()
        test_choice(db, port)
        test_boolean_and_score(db, port)
        test_response_robustness(db, port)
        test_error_surfacing(db, port)
        test_unconfigured(db, port)
        test_probe(db, port)
        test_config_cache(db, port)
    finally:
        db.close()
        srv.shutdown()
    print(f"\n结果：\033[32m{OK} 通过\033[0m / \033[31m{FAIL} 失败\033[0m")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
