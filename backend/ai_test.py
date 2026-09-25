"""AI 智能链路的验收脚本。

和 smoke / security / inbox 并列的第四套。本脚本覆盖：

1. **模型配置**——增删改查、预设厂商、Key 加密与掩码、默认配置唯一性、权限（仅管理员）
2. **连通性测试**——通、以及「Key 错 / 地址错」是否报得清楚
3. **助手对话与工具循环**——流式 SSE 分片、progress 事件、只读工具多轮查询，
   `​```tool` / `​```action` 块必须从正文里藏掉并转成结构化数据
4. **发票识别增强**——本地识别全空时大模型兜底补字段，且**只补空缺不覆盖**
5. **单据分析 / 审批建议 / AI 记账**——结构化输出 + 非法值过滤
6. **用量统计**、**权限边界**（非管理员看不到 Key、不能改配置）
7. **全系统操作能力**——工具/动作按角色裁剪、参数按名字解析成 id、缺参数时先生成
   追问而不是死路、动作最终仍走既有业务接口（越权照旧被拒）、
   以及**前端执行器必须覆盖后端全部动作**（漏一个就是「点了没反应」）

为什么要配一个 Mock 大模型
------------------------
真实厂商 Key 不能进测试，而 AI 的坑几乎都在协议细节上（SSE 分片、JSON 模式、
多模态消息体、错误体结构）。`mock_llm.py` 提供一个按系统提示词返回固定内容的
OpenAI 兼容服务，于是这些细节全部可离线、可重复地覆盖住。

用法：
    # 先起 mock（另一个终端）
    python mock_llm.py 8899
    WB_BASE_URL=http://127.0.0.1:8801 WB_PASSWORD=xxx python ai_test.py
"""

import io
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = (
    (sys.argv[1] if len(sys.argv) > 1 else None)
    or os.environ.get("WB_BASE_URL")
    or "http://127.0.0.1:8801"
).rstrip("/")

ADMIN_USER = os.environ.get("WB_USER", "admin")
ADMIN_PASSWORD = os.environ.get("WB_PASSWORD", "Adm1n@2026")
MOCK_BASE = (os.environ.get("WB_MOCK_LLM") or "http://127.0.0.1:8899/v1").rstrip("/")

OK = 0
FAIL = 0
SKIP = 0
TOKEN = None
PWD = "AiTest@2026"
SUF = uuid.uuid4().hex[:6]
# 发票号码必须是纯数字。直接用 hex 后缀会掺进 a-f，号码被正则截断成半截，
# 于是「本地识别成功」的用例时灵时不灵——这里先转成纯数字再拼。
NUM4 = str(int(SUF, 16) % 10000).zfill(4)

print(f"目标: {BASE}")
print(f"Mock 大模型: {MOCK_BASE}")


# ------------------------------------------------------------------ 请求层


def req(method, path, body=None, params=None, token=None, raw=None, ctype=None,
        headers=None, timeout=60):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = None
    head = dict(headers or {})
    if raw is not None:
        data = raw
        head["Content-Type"] = ctype
    elif body is not None:
        data = json.dumps(body).encode()
        head["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, method=method)
    for k, v in head.items():
        if v is not None:
            r.add_header(k, v)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            payload = resp.read()
            try:
                return resp.status, json.loads(payload.decode() or "null"), payload
            except (json.JSONDecodeError, UnicodeDecodeError):
                return resp.status, None, payload
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload.decode() or "null"), payload
        except (json.JSONDecodeError, UnicodeDecodeError):
            return e.code, f"{len(payload)} bytes", payload
    except Exception as e:  # noqa: BLE001
        return -1, str(e), b""


def check(label, cond, detail=""):
    """纯布尔断言（不涉及 HTTP），用于校验返回值内容。"""
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  \033[32mPASS\033[0m {label}" + (f" | {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m {label}" + (f" | {detail}" if detail else ""))
    return cond


def expect(label, method, path, want=None, body=None, *, token=None, params=None, raw=None,
           ctype=None, headers=None, probe=None, timeout=60):
    global OK, FAIL
    if isinstance(want, dict):
        want, body = body, want
    if want is None:
        want = 200
    st, data, rawbytes = req(method, path, body, params, token, raw, ctype, headers, timeout)
    accepted = want if isinstance(want, (tuple, set, list)) else (want,)
    ok = st in accepted
    extra = ""
    if ok and probe:
        try:
            extra = " | " + str(probe(data, rawbytes))
        except Exception as e:  # noqa: BLE001
            ok = False
            extra = f" | probe 失败: {e}"
    if ok:
        OK += 1
        print(f"  \033[32mPASS\033[0m {label}{extra}")
    else:
        FAIL += 1
        detail = str(data)[:220] if data is not None else f"{len(rawbytes)} bytes"
        print(f"  \033[31mFAIL\033[0m {label} -> HTTP {st}（期望 {accepted}）{detail}{extra}")
    return data


def deny(label, method, path, body=None, *, token=None, probe=None):
    return expect(label, method, path, (401, 403), token=token, body=body, probe=probe)


def skip(label, why):
    global SKIP
    SKIP += 1
    print(f"  \033[33mSKIP\033[0m {label} | {why}")


def login(username, password):
    st, data, _ = req("POST", "/api/auth/login", {"username": username, "password": password})
    if st != 200:
        print(f"\033[31m登录失败 {username}: HTTP {st} {data}\033[0m")
        sys.exit(2)
    return data["token"]


def multipart(parts):
    boundary = "----wbai" + uuid.uuid4().hex
    buf = io.BytesIO()
    for field, filename, content, mime in parts:
        buf.write(f"--{boundary}\r\n".encode())
        if filename is None:
            buf.write(f'Content-Disposition: form-data; name="{field}"\r\n\r\n'.encode())
            buf.write(content.encode("utf-8") if isinstance(content, str) else content)
        else:
            buf.write(
                f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode()
            )
            buf.write(f"Content-Type: {mime or 'application/octet-stream'}\r\n\r\n".encode())
            buf.write(content)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def upload(path, parts, token):
    raw, ctype = multipart(parts)
    st, data, _ = req("POST", path, raw=raw, ctype=ctype, token=token, timeout=180)
    return st, data


# ------------------------------------------------------------------ Mock 可用性

mock_ok = False
try:
    mreq = urllib.request.Request(MOCK_BASE + "/chat/completions", method="POST",
                                  data=json.dumps({"messages": []}).encode(),
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(mreq, timeout=5) as resp:
        mock_ok = resp.status == 200
except urllib.error.HTTPError:
    # mock 只实现 POST /chat/completions，能握上手就算通
    mock_ok = True
except Exception:  # noqa: BLE001
    mock_ok = False

if not mock_ok:
    print(f"\n\033[33m警告：Mock 大模型不可达（{MOCK_BASE}），AI 相关用例将跳过。\033[0m")
    print("       另开一个终端执行： python mock_llm.py 8899\n")


# ------------------------------------------------------------------ 前置

print("\n\033[1m== 前置：账号与主数据 ==\033[0m")
TOKEN = login(ADMIN_USER, ADMIN_PASSWORD)
expect("管理员登录", "GET", "/api/auth/me", 200, token=TOKEN,
       probe=lambda d, _: f"{d['user']['name']} / {d['user']['role']}")

st, dept, _ = req("POST", "/api/departments",
                  {"name": f"AI测试部{SUF}", "code": f"AIT{SUF[:4]}"}, token=TOKEN)
if st != 201:
    print(f"\033[31m建部门失败: {st} {dept}\033[0m")
    sys.exit(2)
DEPT_ID = dept["id"]

st, cat, _ = req("POST", "/api/categories", {"name": f"AI测试费{SUF}", "group_name": "测试"},
                 token=TOKEN)
CAT_ID = cat["id"] if st == 201 else None

st, emp, _ = req("POST", "/api/employees",
                 {"name": f"AI测试员{SUF}", "employee_no": f"AIT{SUF}", "department_id": DEPT_ID},
                 token=TOKEN)
if st != 201:
    print(f"\033[31m建员工失败: {st} {emp}\033[0m")
    sys.exit(2)
EMP_ID = emp["id"]


def make_user(username, name, role, *, employee_id=None, approval_level=0):
    st, data, _ = req("POST", "/api/users", {
        "username": username, "name": name, "role": role,
        "employee_id": employee_id, "approval_level": approval_level, "password": PWD,
    }, token=TOKEN)
    if st != 201:
        print(f"\033[31m建账号失败 {username}: {st} {data}\033[0m")
        sys.exit(2)
    return data


FIN_U = f"ait_fin_{SUF}"
APP_U = f"ait_appr_{SUF}"
USR_U = f"ait_usr_{SUF}"
make_user(FIN_U, "AI财务", "财务")
make_user(APP_U, "AI审批人", "审批人", employee_id=EMP_ID, approval_level=1)
make_user(USR_U, "AI申请人", "申请人", employee_id=EMP_ID)
FIN_TOKEN = login(FIN_U, PWD)
APP_TOKEN = login(APP_U, PWD)
USR_TOKEN = login(USR_U, PWD)
print("  四个角色账号就绪")


# ------------------------------------------------------------------ 一、模型配置

print("\n\033[1m== 一、模型配置与权限 ==\033[0m")
deny("未登录不能读配置", "GET", "/api/ai/providers")
deny("未登录不能建配置", "POST", "/api/ai/providers",
     {"name": "x", "base_url": "http://x", "model": "m"})
deny("财务不能读配置（Key 属敏感）", "GET", "/api/ai/providers", token=FIN_TOKEN)
deny("财务不能建配置", "POST", "/api/ai/providers",
     {"name": "x", "base_url": "http://x", "model": "m"}, token=FIN_TOKEN)
deny("审批人不能建配置", "POST", "/api/ai/providers",
     {"name": "x", "base_url": "http://x", "model": "m"}, token=APP_TOKEN)
deny("申请人不能读预设", "GET", "/api/ai/presets", token=USR_TOKEN)

expect("管理员可读预设厂商清单", "GET", "/api/ai/presets", 200, token=TOKEN,
       probe=lambda d, _: f"{len(d)} 家（含 {','.join(p['key'] for p in d[:4])}…）")

# 校验：地址/模型名必填，名称不能重复
expect("缺地址被拒", "POST", "/api/ai/providers",
       {"name": f"缺地址{SUF}", "base_url": "", "model": "m"}, 400, token=TOKEN)
expect("缺模型名被拒", "POST", "/api/ai/providers",
       {"name": f"缺模型{SUF}", "base_url": "http://x/v1", "model": ""}, 400, token=TOKEN)

prov = expect("新增模型配置（自定义）", "POST", "/api/ai/providers",
              {"name": f"AI测试Mock{SUF}", "preset": "custom", "base_url": MOCK_BASE,
               "api_key": "mock-key-abcdef123456", "model": "mock-model",
               "vision_model": "mock-vl", "is_default": True,
               "use_assistant": True, "use_recognize": True, "use_analyze": True},
              201, token=TOKEN)
PID = prov["id"] if prov else None
check("明文 Key 不出后端（只回掩码）", bool(prov) and prov.get("has_key") is True
      and "mock-key-abcdef123456" not in json.dumps(prov, ensure_ascii=False),
      f"mask={prov.get('key_mask') if prov else '-'}")
# 早先的掩码是「首字符 + 星号 + 末字符」，对 API Key 这类凭据泄露得偏多：
# 首字符往往带厂商前缀（sk-/m-），末字符又能缩小穷举范围。
# 现在整段遮掉、只留末 4 位认身份，且长度固定为 12，不把密钥真实长度带出去。
check("掩码只留末 4 位便于辨认", bool(prov) and str(prov.get("key_mask", "")).endswith("3456")
      and str(prov.get("key_mask", "")).startswith("*"),
      f"mask={prov.get('key_mask') if prov else '-'}")
check("掩码不暴露首字符、长度固定", bool(prov)
      and "m" not in str(prov.get("key_mask", ""))
      and len(str(prov.get("key_mask", ""))) == 12,
      f"mask={prov.get('key_mask') if prov else '-'}")
check("视觉模型已标记", bool(prov) and prov.get("can_vision") is True)

expect("同名配置被拒", "POST", "/api/ai/providers",
       {"name": f"AI测试Mock{SUF}", "base_url": MOCK_BASE, "model": "m"}, 400, token=TOKEN)

# 不传 api_key（None）时不应清空已有 Key
upd = expect("改配置但不传 Key", "PUT", f"/api/ai/providers/{PID}",
             {"name": f"AI测试Mock{SUF}", "preset": "custom", "base_url": MOCK_BASE,
              "model": "mock-model", "is_default": True, "enabled": True},
             200, token=TOKEN)
check("不传 Key 时保留原 Key", bool(upd) and upd.get("has_key") is True,
      f"has_key={upd.get('has_key') if upd else '-'}")

# 传空串才清空
upd2 = expect("传空串清空 Key", "PUT", f"/api/ai/providers/{PID}",
              {"name": f"AI测试Mock{SUF}", "preset": "custom", "base_url": MOCK_BASE,
               "model": "mock-model", "api_key": "", "is_default": True},
              200, token=TOKEN)
check("空串确实清空", bool(upd2) and upd2.get("has_key") is False)
# 再装回去，后面还要用
req("PUT", f"/api/ai/providers/{PID}",
    {"name": f"AI测试Mock{SUF}", "preset": "custom", "base_url": MOCK_BASE,
     "model": "mock-model", "vision_model": "mock-vl", "api_key": "mock-key-abcdef123456",
     "is_default": True}, token=TOKEN)

expect("所有登录用户可见 AI 状态", "GET", "/api/ai/status", 200, token=USR_TOKEN,
       probe=lambda d, _: f"configured={d['configured']} can_manage={d['can_manage']} "
                          f"ocr={d['ocr'].get('engine')}")
expect("普通用户不能改配置", "PUT", f"/api/ai/providers/{PID}",
       {"name": "x", "base_url": "http://x", "model": "m"}, (401, 403), token=USR_TOKEN)

# ------------------------------------------------------------------ 二、连通性

print("\n\033[1m== 二、连通性测试 ==\033[0m")
if mock_ok:
    got = expect("连通性测试通过", "POST", f"/api/ai/providers/{PID}/test", {}, 200, token=TOKEN,
                 probe=lambda d, _: f"ok={d.get('ok')} {d.get('latency_ms')}ms")
    check("测试结果落到配置上", bool(got) and got["provider"]["last_test_ok"] is True)
    expect("连通性测试仅管理员可用", "POST", f"/api/ai/providers/{PID}/test", {},
           (401, 403), token=FIN_TOKEN)

    bad = expect("Key 错时报错清清楚楚", "POST", f"/api/ai/providers/{PID}/test",
                 {"api_key": "bad-key"}, 200, token=TOKEN,
                 probe=lambda d, _: str(d.get("message"))[:80])
    check("错误信息可读（提到认证失败）", bool(bad) and "认证失败" in str(bad.get("message")),
          str(bad.get("message") if bad else "-")[:60])
    # 复原
    req("PUT", f"/api/ai/providers/{PID}",
        {"name": f"AI测试Mock{SUF}", "preset": "custom", "base_url": MOCK_BASE,
         "model": "mock-model", "vision_model": "mock-vl", "api_key": "mock-key-abcdef123456",
         "is_default": True}, token=TOKEN)

    expect("地址不可达时报网络错", "POST", f"/api/ai/providers/{PID}/test",
           {"base_url": "http://127.0.0.1:9/v1"}, 200, token=TOKEN,
           probe=lambda d, _: f"ok={d.get('ok')} {str(d.get('message'))[:60]}")
    req("PUT", f"/api/ai/providers/{PID}",
        {"name": f"AI测试Mock{SUF}", "preset": "custom", "base_url": MOCK_BASE,
         "model": "mock-model", "vision_model": "mock-vl", "api_key": "mock-key-abcdef123456",
         "is_default": True}, token=TOKEN)
else:
    skip("连通性测试", "Mock 未启动")

# ------------------------------------------------------------------ 三、助手对话

print("\n\033[1m== 三、助手对话 ==\033[0m")
expect("空提问被拒", "POST", "/api/ai/chat", {"messages": [], "stream": False}, 400, token=TOKEN)
expect("未登录不能用助手", "POST", "/api/ai/chat",
       {"messages": [{"role": "user", "content": "hi"}], "stream": False}, (401, 403))
CATALOG = expect("动作目录可见", "GET", "/api/ai/actions", 200, token=FIN_TOKEN,
                 probe=lambda d, _: f"{len(d.get('actions') or [])} 个动作 / "
                                    f"{len(d.get('tools') or [])} 个只读工具")
check("动作目录是新形状（actions + tools + tool_rounds）",
      isinstance(CATALOG, dict) and {"actions", "tools", "tool_rounds"} <= set(CATALOG or {}),
      str(sorted((CATALOG or {}).keys())))

if mock_ok:
    # 问「怎么用」类问题也要先去查库：服务端会把只读工具结果回灌给模型，
    # trace 里能看到到底查了什么。这是「不再张口说我不知道」的关键证据。
    ans = expect("非流式对话（含只读工具循环）", "POST", "/api/ai/chat",
                 {"messages": [{"role": "user", "content": "这个系统怎么登记发票？"}],
                  "stream": False}, 200, token=TOKEN,
                 probe=lambda d, _: f"{len(d.get('text') or '')} 字 / "
                                    f"trace={[t.get('tool') for t in (d.get('trace') or [])]}")
    check("回复里不含 ```tool / ```action 原始块",
          bool(ans) and "```tool" not in (ans.get("text") or "")
          and "```action" not in (ans.get("text") or ""))
    trace = (ans or {}).get("trace") or []
    check("确实调用了只读工具（不是直接编答案）", len(trace) >= 1,
          json.dumps(trace, ensure_ascii=False)[:160])
    check("工具调用全部成功", bool(trace) and all(t.get("ok") for t in trace),
          json.dumps(trace, ensure_ascii=False)[:160])
    check("这次问句没有触发写动作", (ans or {}).get("action") is None,
          json.dumps((ans or {}).get("action"), ensure_ascii=False)[:120])

    # 流式：必须能拿到多个 delta 分片，最后有 done 事件
    url = BASE + "/api/ai/chat"
    payload = json.dumps({"messages": [{"role": "user", "content": "你好"}],
                          "page": "invoices", "stream": True}).encode()
    r = urllib.request.Request(url, data=payload, method="POST",
                               headers={"Content-Type": "application/json",
                                        "Authorization": f"Bearer {TOKEN}"})
    deltas, done, sse_err, progress = [], None, None, []
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            for line in resp:
                line = line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    evt = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if evt.get("type") == "delta":
                    deltas.append(evt.get("text") or "")
                elif evt.get("type") == "progress":
                    progress.append(evt.get("text") or "")
                elif evt.get("type") == "done":
                    done = evt
                elif evt.get("type") == "error":
                    sse_err = evt.get("message")
    except Exception as e:  # noqa: BLE001
        sse_err = str(e)
    check("流式对话拿到多个增量分片", len(deltas) >= 2, f"{len(deltas)} 个分片")
    check("流式对话以 done 收尾", bool(done) and sse_err is None, f"err={sse_err}")
    check("流式正文里也没有代码块",
          "```action" not in "".join(deltas) and "```tool" not in "".join(deltas))
    # 工具循环期间正文还没出来，progress 是唯一的「它还在干活」信号，
    # 少了它用户会以为助手卡死。
    check("查库期间下发 progress 事件", len(progress) >= 1, " / ".join(progress)[:120])
    check("done 事件带回本轮工具轨迹",
          bool(done) and len(done.get("trace") or []) >= 1,
          json.dumps((done or {}).get("trace"), ensure_ascii=False)[:160])
else:
    skip("对话链路", "Mock 未启动")

# ------------------------------------------------------------------ 四、识别增强

print("\n\033[1m== 四、发票识别增强（AI 兜底）==\033[0m")

OFD_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice>
  <InvoiceNumber>24417000000{NUM4}001</InvoiceNumber>
  <IssueTime>2026-09-10</IssueTime>
  <SellerName>AI测试供应商有限公司</SellerName>
  <SellerIdNum>91440300MA5F1234XA</SellerIdNum>
  <BuyerName>AI测试采购方有限公司</BuyerName>
  <BuyerIdNum>91440300MA5G5678XB</BuyerIdNum>
  <TotalAmtWithTax>3180.00</TotalAmtWithTax>
  <TotalTax>365.84</TotalTax>
  <TaxRate>13</TaxRate>
</Invoice>"""


def make_ofd() -> bytes:
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("OFD.xml", "<OFD/>")
        z.writestr("Doc_0/OriginalInvoice.xml", OFD_XML)
    return buf.getvalue()


# 本地能读全的票：不该再花 AI 的钱
st, loc = upload("/api/inbox/recognize", [
    ("file", "dzfp.ofd", make_ofd(), "application/octet-stream"),
    ("use_ai", None, "true", None),
], TOKEN)
check("OFD 本地全字段命中", st == 200 and loc.get("invoice_no") == f"24417000000{NUM4}001",
      f"号码={loc.get('invoice_no') if loc else '-'}")
check("本地已覆盖时不调用大模型（省 token）",
      bool(loc) and (loc.get("ai") or {}).get("used") is False,
      str((loc or {}).get("ai"))[:100])
check("逐字段来源标出本地层",
      bool(loc) and (loc.get("field_sources") or {}).get("invoice_no") in ("ofd", "xml"),
      str((loc or {}).get("field_sources"))[:100])

if mock_ok:
    # 空白图：本地什么都读不出，AI 必须兜底
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (800, 500), "white").save(buf, format="JPEG")
    blank = buf.getvalue()

    st, got = upload("/api/inbox/recognize", [
        ("file", "照片.jpg", blank, "image/jpeg"),
        ("use_ai", None, "true", None),
    ], TOKEN)
    check("识别不出时 AI 兜底补字段", st == 200 and (got.get("ai") or {}).get("used") is True,
          f"st={st} ai={str((got or {}).get('ai'))[:90]}")
    applied = (got.get("ai") or {}).get("applied") or [] if got else []
    check("补全清单非空（不是「已调用但补 0 项」）", len(applied) >= 5,
          f"{len(applied)} 项：{','.join(applied[:6])}")
    check("补全的字段逐项标注来源=ai",
          bool(got) and (got.get("field_sources") or {}).get("invoice_no") == "ai",
          str((got or {}).get("field_sources", {}).get("invoice_no")))
    check("来源打上 +ai 标记", bool(got) and "+ai" in str(got.get("source")),
          f"source={got.get('source') if got else '-'}")

    st, off = upload("/api/inbox/recognize", [
        ("file", "照片.jpg", blank, "image/jpeg"),
        ("use_ai", None, "false", None),
    ], TOKEN)
    check("use_ai=false 时确实不调模型",
          st == 200 and not (off.get("ai") or {}).get("used"),
          str((off or {}).get("ai"))[:100])
    check("关掉 AI 后字段确实缺（对照组）", bool(off) and not off.get("invoice_no"),
          f"号码={off.get('invoice_no') if off else '-'}")

    # 权限：申请人不能识别
    raw, ctype = multipart([("file", "x.ofd", make_ofd(), "application/octet-stream")])
    st = req("POST", "/api/inbox/recognize", raw=raw, ctype=ctype, token=USR_TOKEN)[0]
    check("申请人无权用识别接口", st in (401, 403), f"HTTP {st}")
else:
    skip("AI 兜底识别", "Mock 未启动")

# ------------------------------------------------------------------ 五、业务智能

print("\n\033[1m== 五、单据分析 / 审批建议 / AI 记账 ==\033[0m")

st, rmb, _ = req("POST", "/api/reimbursements", {
    "title": f"AI测试报销单{SUF}", "applicant_id": EMP_ID, "department_id": DEPT_ID,
    "purpose": "验证 AI 分析与审批建议",
    "items": [{"category_id": CAT_ID, "occur_date": "2026-09-10", "amount": 3180,
               "tax_amount": 365.84, "description": "测试明细"}],
}, token=TOKEN)
if st not in (200, 201):
    print(f"\033[31m建报销单失败: {st} {rmb}\033[0m")
    sys.exit(2)
RID = rmb["id"]

# 造几张散票供 AI 记账
for i in range(2):
    st, inv, _ = req("POST", "/api/invoices", {
        "invoice_no": f"24417000000{NUM4}9{i:02d}", "invoice_type": "数电票",
        "amount": 500 + i * 120, "tax_amount": 0, "tax_rate": 0,
        "invoice_date": "2026-09-10", "seller_name": f"AI测试商户{i}",
        "buyer_name": "AI测试采购方有限公司", "category_id": CAT_ID,
    }, token=TOKEN)
    if st not in (200, 201):
        print(f"\033[33m  造散票失败: {st} {inv}\033[0m")

expect("未登录不能分析单据", "POST", f"/api/ai/analyze/{RID}", {}, (401, 403))
expect("不存在的单号报 404", "POST", "/api/ai/analyze/99999999", {}, 404, token=TOKEN)

if mock_ok:
    ana = expect("单据合规分析", "POST", f"/api/ai/analyze/{RID}", {}, 200, token=TOKEN,
                 probe=lambda d, _: f"级别={d.get('level')} 发现={len(d.get('findings') or [])} 项")
    check("分析结果带摘要与等级",
          bool(ana) and ana.get("summary") and ana.get("level") in ("正常", "关注", "风险"),
          str((ana or {}).get("level")))
    findings = (ana or {}).get("findings") or []
    check("发现项结构完整（标题/说明/级别）",
          bool(findings) and all({"title", "detail", "level"} <= set(f) for f in findings),
          f"{len(findings)} 项")
    check("分析结果给出整改建议", bool(ana) and len(ana.get("suggestions") or []) >= 1)

    adv = expect("审批建议", "POST", f"/api/ai/approval/{RID}", {}, 200, token=APP_TOKEN,
                 probe=lambda d, _: f"建议={d.get('recommend')} 风险={len(d.get('risks') or [])} 项")
    check("审批建议带免责声明", bool(adv) and "参考" in str(adv.get("disclaimer")))
    expect("申请人不能看审批建议", "POST", f"/api/ai/approval/{RID}", {},
           (401, 403), token=USR_TOKEN)

    inv_page = req("GET", "/api/invoices", {"unlinked": "true", "page_size": "50"}, token=TOKEN)[1] or {}
    ids = [v["id"] for v in (inv_page.get("items") or []) if isinstance(v, dict) and v.get("id")]
    if ids:
        bk = expect("AI 记账出草稿", "POST", "/api/ai/bookkeeping",
                    {"invoice_ids": ids[:3]}, 200, token=TOKEN,
                    probe=lambda d, _: f"{d.get('invoice_count')} 张票 -> "
                                       f"{len(d.get('drafts') or [])} 张单")
        drafts = (bk or {}).get("drafts") or []
        check("确实编出了草稿（不是空列表）", len(drafts) >= 1, f"{len(drafts)} 张")
        check("草稿带金额合计", all("total_amount" in d for d in drafts) and bool(drafts))
        all_rows = [r for d in drafts for r in (d.get("items") or [])]
        check("明细里的发票 id 都在本次输入范围内",
              bool(all_rows) and all(r["invoice_id"] in ids[:3] for r in all_rows),
              f"{len(all_rows)} 行 / 输入 {ids[:3]}")
        check("草稿回填了发票号码，便于人核对",
              all((v.get("invoice_no") for d in drafts for v in (d.get("invoices") or []))))
        check("草稿带免责声明", bool(bk) and "核对" in str(bk.get("disclaimer")))
        expect("申请人不能 AI 记账", "POST", "/api/ai/bookkeeping",
               {"invoice_ids": ids[:1]}, (401, 403), token=USR_TOKEN)
        expect("空发票列表被拒", "POST", "/api/ai/bookkeeping", {"invoice_ids": []},
               400, token=TOKEN)
    else:
        skip("AI 记账", "没有可用的散票")

    expect("用量统计可读", "GET", "/api/ai/usage", 200, token=TOKEN,
           probe=lambda d, _: f"共 {d.get('total_calls')} 次调用 / {d.get('total_tokens')} tokens")
    expect("用量统计仅管理员", "GET", "/api/ai/usage", (401, 403), token=FIN_TOKEN)
else:
    skip("单据智能分析", "Mock 未启动")

# ------------------------------------------------------------------ 六、全系统操作能力

print("\n\033[1m== 六、全系统操作能力 ==\033[0m")

# 助手要「替用户把事办完」，就得覆盖整个系统。这一节把这条承诺钉死：
# 工具与动作按角色裁剪、参数能按名字解析成 id、缺参数时先追问而不是摆烂，
# 以及**前端必须为每个动作都准备执行器**——漏一个就是「点了没反应」。


def ensure_named(res, name, extra=None):
    """按名字找一条主数据，没有才建。返回 (记录, 是否新建)。"""
    st, rows, _ = req("GET", f"/api/{res}", params={"q": name}, token=TOKEN)
    for row in rows or []:
        if isinstance(row, dict) and row.get("name") == name:
            return row, False
    st, got, _ = req("POST", f"/api/{res}", {"name": name, **(extra or {})}, token=TOKEN)
    return (got, True) if st == 201 else (None, False)


# 助手回复里说的是人话（名字），落库要的是 id。先把这几个名字准备好，
# 让「名字 -> id」这条解析路径真的被走到；已经存在就复用。
CUST, NEW_CUST = ensure_named("customers", "XTS")
CAT2, NEW_CAT = ensure_named("categories", "商务宴请")
EMP2, NEW_EMP = ensure_named("employees", "张斌",
                             {"employee_no": f"ZB{SUF}", "department_id": DEPT_ID})
check("用于验证「名字→id」解析的数据就绪", bool(CUST and CAT2 and EMP2),
      f"客户={CUST and CUST['id']} 费用类型={CAT2 and CAT2['id']} 员工={EMP2 and EMP2['id']}")

if mock_ok:
    # ---- 6.1 工具与动作都按角色裁剪 ----
    # 上面 CATALOG 是用财务账号取的（顺带验了「非管理员也能拿目录」），
    # 这里必须另取管理员那份：拿财务的当 ADMIN_ACTIONS 会漏掉管理员独有的动作，
    # 「管理员专属动作不给申请人」会一路假绿。
    ADMIN_CAT = expect("管理员动作目录", "GET", "/api/ai/actions", 200, token=TOKEN,
                       probe=lambda d, _: f"{len(d.get('actions') or [])} 个动作 / "
                                          f"{len(d.get('tools') or [])} 个工具")
    ADMIN_ACTIONS = {a["type"] for a in (ADMIN_CAT or {}).get("actions") or []}
    FIN_ACTIONS = {a["type"] for a in (CATALOG or {}).get("actions") or []}
    USR_CAT = expect("申请人也能拿动作目录", "GET", "/api/ai/actions", 200, token=USR_TOKEN,
                     probe=lambda d, _: f"{len(d.get('actions') or [])} 个动作 / "
                                        f"{len(d.get('tools') or [])} 个工具")
    USR_ACTIONS = {a["type"] for a in (USR_CAT or {}).get("actions") or []}
    check("管理员的动作集严格大于申请人", ADMIN_ACTIONS > USR_ACTIONS,
          f"管理员 {len(ADMIN_ACTIONS)} / 申请人 {len(USR_ACTIONS)}")
    check("财务的动作集介于管理员与申请人之间",
          USR_ACTIONS < FIN_ACTIONS < ADMIN_ACTIONS,
          f"申请人 {len(USR_ACTIONS)} / 财务 {len(FIN_ACTIONS)} / 管理员 {len(ADMIN_ACTIONS)}")
    # 申请人只该看到「自己能干的那几件事」。这些动作要么是管理员的，
    # 要么是财务台的，一个都不能漏给他。
    for not_for_user in ("create_user", "reset_password", "create_customer",
                         "create_mail_account", "set_setting", "delete_master"):
        check(f"「{not_for_user}」不给申请人",
              not_for_user in ADMIN_ACTIONS and not_for_user not in USR_ACTIONS, "")
    # delete_master 是「按实体再判一次」的动作：客户/项目/部门/员工只有管理员能动，
    # 费用类型财务也能动，所以它对财务可见、对申请人不可见——可见 ≠ 可执行，
    # 真正拦住越权的是 prepare_write 里的逐实体角色判定。
    check("delete_master 对财务可见（费用类型财务能删）",
          "delete_master" in FIN_ACTIONS, "")
    for only_admin in ("create_user", "reset_password", "create_customer",
                       "create_mail_account", "set_setting"):
        check(f"管理员专属动作「{only_admin}」也不给财务",
              only_admin not in FIN_ACTIONS, "")

    # ---- 6.2 显式指定申请人：姓名要能解析成员工 id ----
    ask = {"messages": [{"role": "user", "content": "帮我建一张报销单：XTS 招待晚餐"}],
           "page": "reimbursements", "stream": False}
    got = expect("起草新建报销单（写明申请人=张斌）", "POST", "/api/ai/chat", 200,
                 body=ask, token=USR_TOKEN,
                 probe=lambda d, _: f"action={(d.get('action') or {}).get('type')}")
    act = (got or {}).get("action") or {}
    check("给出可执行动作，而不是让用户自己去点页面",
          act.get("type") == "create_reimbursement",
          json.dumps(act, ensure_ascii=False)[:160])
    params = act.get("params") or {}
    check("申请人姓名被解析成员工 id",
          params.get("applicant_id") == EMP2["id"],
          f"applicant_id={params.get('applicant_id')} 期望 {EMP2['id']}")
    check("客户名被解析成客户 id", params.get("customer_id") == CUST["id"],
          f"customer_id={params.get('customer_id')}")
    items = params.get("items") or []
    check("明细行的费用类型名被解析成 id",
          bool(items) and items[0].get("category_id") == CAT2["id"],
          json.dumps(items, ensure_ascii=False)[:160])
    check("动作带后端给的标签，前端不用维护第二份",
          bool(act.get("label")), str(act.get("label")))
    check("动作带前置校验提示（warnings）", len(act.get("warnings") or []) >= 1,
          " / ".join(act.get("warnings") or [])[:160])

    # ---- 6.3 不指定申请人：默认取登录账号绑定的员工 ----
    default_ask = {"messages": [{"role": "user",
                                 "content": "帮我建一张报销单：XTS 招待晚餐，不提申请人"}],
                   "page": "reimbursements", "stream": False}
    g2 = expect("起草时不指定申请人", "POST", "/api/ai/chat", 200, body=default_ask,
                token=USR_TOKEN,
                probe=lambda d, _: f"action={(d.get('action') or {}).get('type')}")
    a2 = (g2 or {}).get("action") or {}
    p2 = a2.get("params") or {}
    check("申请人默认取登录账号绑定的员工",
          p2.get("applicant_id") == EMP_ID,
          f"applicant_id={p2.get('applicant_id')} 期望 {EMP_ID}")
    check("补出来的默认值给了明确提示",
          any("员工" in w for w in (a2.get("warnings") or [])),
          " / ".join(a2.get("warnings") or [])[:160])

    # ---- 6.4 参数不够：追问，而不是把必失败的卡片丢给用户 ----
    thin = {"messages": [{"role": "user", "content": "帮我建一张报销单，缺明细"}],
            "stream": False}
    q = expect("明细缺失时助手会反问", "POST", "/api/ai/chat", 200, body=thin,
               token=USR_TOKEN, probe=lambda d, _: f"action={bool(d.get('action'))}")
    check("缺参数时不给动作", (q or {}).get("action") is None,
          json.dumps((q or {}).get("action"), ensure_ascii=False)[:140])
    check("反问里点到了缺的那一项", "明细" in ((q or {}).get("text") or ""),
          str((q or {}).get("text"))[:140])

    # ---- 6.5 动作最终仍走既有业务接口，所以越权照旧被拒 ----
    # 这比断言提示词有意义得多：AI 拿不到绕过权限的路径，是因为它压根没有那条路径。
    deny("申请人调主数据写接口仍被拒", "POST", "/api/customers", {"name": "越权测试"},
         token=USR_TOKEN)
    deny("申请人建账号仍被拒", "POST", "/api/users",
         {"username": f"x{SUF}", "name": "越权", "role": "申请人", "password": PWD},
         token=USR_TOKEN)

    # ---- 6.6 前端执行器必须覆盖后端全部动作 ----
    js = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "js" / "ai.js"
    check("能定位到前端助手脚本", js.exists(), str(js))
    if js.exists():
        src = js.read_text(encoding="utf-8")
        block = src[src.index("const EXECUTORS = {"):src.index("/* 动作标签优先用")]
        impl = set(re.findall(r"^\s{4}(?:async\s+)?([a-z_]+)\s*[:(]", block, re.M))
        missing = sorted(ADMIN_ACTIONS - impl)
        check("前端为每个动作都实现了执行器（漏一个就是点了没反应）", not missing,
              f"缺失：{missing}" if missing else f"已覆盖 {len(impl)} 个动作")
else:
    skip("全系统操作能力", "Mock 未启动")

# ------------------------------------------------------------------ 七、收尾

print("\n\033[1m== 七、清理 ==\033[0m")
expect("删除模型配置", "DELETE", f"/api/ai/providers/{PID}", 200, token=TOKEN)
expect("删除后不再可见", "GET", f"/api/ai/providers/{PID}", (404, 405), token=TOKEN)
left = req("GET", "/api/ai/providers", token=TOKEN)[1] or []
check("删除后 AI 状态回到未配置", not any(p["id"] == PID for p in left),
      f"剩余 {len(left)} 条")
st, stt, _ = req("GET", "/api/ai/status", token=TOKEN)
check("无可用模型时状态如实反馈", st == 200 and stt.get("configured") is False,
      f"configured={stt.get('configured') if stt else '-'}")

print(f"\n\033[1m结果：{OK} 通过 / {FAIL} 失败" + (f" / {SKIP} 跳过" if SKIP else "") + "\033[0m")
sys.exit(1 if FAIL else 0)
