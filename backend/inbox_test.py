"""发票收件箱 / 自动识别 / 批量删除 的验收脚本。

与 smoke_test（功能冒烟）、security_test（权限）并列的第三套：
本脚本验证「邮箱收票 + 自动识别 + 批量删除 + 一键清库」这条新增链路。

设计成**自己造数据**，在干净库上跑。文件全是**合成样本**（号码与公司名均为虚构），
不依赖真实发票，也不会真的去连邮箱——连不通的用例反而要断言「失败得干净」。

用法：
    WB_INGEST_TOKEN=xxx WB_BASE_URL=http://127.0.0.1:8792 WB_PASSWORD=xxx python inbox_test.py
"""

import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

BASE = (
    (sys.argv[1] if len(sys.argv) > 1 else None)
    or os.environ.get("WB_BASE_URL")
    or "http://127.0.0.1:8792"
).rstrip("/")

ADMIN_USER = os.environ.get("WB_USER", "admin")
ADMIN_PASSWORD = os.environ.get("WB_PASSWORD", "Adm1n@2026")
INGEST_TOKEN = os.environ.get("WB_INGEST_TOKEN", "")

OK = 0
FAIL = 0
# 被跳过的断言。跳过的原因只有两类：环境没配（如没给 WB_INGEST_TOKEN），
# 或前置步骤没成功（如入账返回里没有 invoice_id）。两者都必须**显式计数**——
# 早先这类分支只是默默少打几行，结果「72 通过 / 0 失败」看起来是绿的，
# 实际上外部投递、影像随票删除这几条根本没跑。
SKIPPED: list[str] = []
TOKEN = None


print(f"目标: {BASE}")


# ------------------------------------------------------------------ 请求层


def req(method, path, body=None, params=None, token=None, raw=None, ctype=None, headers=None):
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
        with urllib.request.urlopen(r, timeout=40) as resp:
            body_bytes = resp.read()
            try:
                return resp.status, json.loads(body_bytes.decode() or "null"), body_bytes
            except (json.JSONDecodeError, UnicodeDecodeError):
                return resp.status, None, body_bytes
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload.decode() or "null"), payload
        except (json.JSONDecodeError, UnicodeDecodeError):
            return e.code, f"{len(payload)} bytes", payload
    except Exception as e:  # noqa: BLE001
        return -1, str(e), b""


def expect(label, method, path, want=None, body=None, *, token=None, params=None, raw=None,
           ctype=None, headers=None, probe=None):
    global OK, FAIL
    if isinstance(want, dict):
        want, body = body, want
    if want is None:
        want = 200
    st, data, rawbytes = req(method, path, body, params, token, raw, ctype, headers)
    accepted = want if isinstance(want, (tuple, set, list)) else (want,)
    ok = st in accepted
    extra = ""
    if ok and probe:
        try:
            # str() 兜底：probe 偶尔会返回 None（比如只由 _must 组成时），
            # 直接拼字符串会炸出一个与真实原因无关的 TypeError
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
        # FAIL 时必须把 probe 的断言原因带出来，否则只看到 HTTP 200 无从排查
        print(f"  \033[31mFAIL\033[0m {label} -> HTTP {st}（期望 {accepted}）{detail}{extra}")
    return data


def must_deny(label, method, path, body=None, *, token=None, raw=None, ctype=None, headers=None):
    return expect(label, method, path, (401, 403), token=token, body=body, raw=raw,
                  ctype=ctype, headers=headers)


def skip(label: str, why: str) -> None:
    """显式记录一条没跑的断言。汇总里会点名，避免「绿的但没测到」。"""
    SKIPPED.append(f"{label}（{why}）")
    print(f"  \033[33mSKIP\033[0m {label}（{why}）")


def need(label: str, value, why: str):
    """前置步骤的返回值必须存在，拿不到就直接记 FAIL。

    这些前置（入账返回 invoice_id、上传影像返回 id…）在健康的系统里**必然**成立，
    所以缺了就是缺陷，不能当成「跳过」。早先它们只让后续断言静默消失，
    结果是一次「0 失败」的假绿。只有**环境相关**的依赖（如没配投递令牌）
    才走 skip()。
    """
    global FAIL
    if value:
        return value
    FAIL += 1
    print(f"  \033[31mFAIL\033[0m {label} -> 前置缺失：{why}")
    return None


def login(username, password):
    st, data, _ = req("POST", "/api/auth/login", {"username": username, "password": password})
    if st != 200:
        print(f"\033[31m登录失败 {username}: HTTP {st} {data}\033[0m")
        sys.exit(2)
    return data["token"]


def multipart(parts):
    """构造 multipart 请求体。

    parts: [("file", filename, bytes, mime) | ("subject", None, "文本", None), ...]
    urllib 没有现成的表单构造器，手工拼最省事也最可控。
    """
    boundary = "----wbinbox" + uuid.uuid4().hex
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


# ------------------------------------------------------------------ 合成样本

PWD = "Test@2026x"
SUF = uuid.uuid4().hex[:6]
# 发票号码必须是纯数字：用十六进制后缀会掺进字母，正则只能匹配到前半截数字
NUM6 = str(int(SUF, 16) % 1000000).zfill(6)


def inv_no(seq: int) -> str:
    """合成 20 位数电票号码（11 位前缀 + 6 位随机 + 3 位序号）。"""
    return f"24417000000{NUM6}{seq:03d}"


OFD_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice>
  <InvoiceNumber>{inv_no(1)}</InvoiceNumber>
  <IssueTime>2026-09-10</IssueTime>
  <SellerName>合成样本供应商甲有限公司</SellerName>
  <SellerIdNum>91440300MA5F1234XA</SellerIdNum>
  <BuyerName>合成样本采购方乙有限公司</BuyerName>
  <BuyerIdNum>91440300MA5G5678XB</BuyerIdNum>
  <TotalAmtWithTax>2450.00</TotalAmtWithTax>
  <TotalTax>281.86</TotalTax>
  <TaxRate>13</TaxRate>
</Invoice>"""


def make_ofd(invoice_no: str | None = None) -> bytes:
    xml = OFD_XML if not invoice_no else OFD_XML.replace(inv_no(1), invoice_no)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("OFD.xml", "<OFD/>")
        z.writestr("Doc_0/OriginalInvoice.xml", xml)
    return buf.getvalue()


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
EXE = b"MZ\x90\x00" + b"\x00" * 64


# ------------------------------------------------------------------ 前置数据

print("\n\033[1m== 前置：管理员登录并造账号 ==\033[0m")
TOKEN = login(ADMIN_USER, ADMIN_PASSWORD)
expect("管理员登录", "GET", "/api/auth/me", 200, token=TOKEN,
       probe=lambda d, _: f"{d['user']['name']} / {d['user']['role']}")
expect("管理员被要求改初始密码", "GET", "/api/auth/me", 200, token=TOKEN,
       probe=lambda d, _: f"must_change_password={d['user']['must_change_password']}")


def make_user(username, name, role, *, employee_id=None, approval_level=0):
    st, data, _ = req("POST", "/api/users", {
        "username": username, "name": name, "role": role,
        "employee_id": employee_id, "approval_level": approval_level, "password": PWD,
    }, token=TOKEN)
    if st != 201:
        print(f"\033[31m创建账号失败 {username}: HTTP {st} {data}\033[0m")
        sys.exit(2)
    t = login(username, PWD)
    req("POST", "/api/auth/password", {"old_password": PWD, "new_password": PWD + "Z9"}, token=t)
    return login(username, PWD + "Z9"), data["id"]


dept = expect("建部门", "POST", "/api/departments",
              {"name": f"收票测试部-{SUF}", "code": f"MB{SUF}"}, 201, token=TOKEN)["id"]
emp = expect("建员工", "POST", "/api/employees",
             {"name": f"收票测试员{SUF}", "employee_no": f"ME{SUF}", "department_id": dept},
             201, token=TOKEN)["id"]
cat = expect("建费用类型", "POST", "/api/categories",
             {"name": f"收票测试费用-{SUF}", "code": f"MC{SUF}"}, 201, token=TOKEN)["id"]

APP_TOKEN, APP_ID = make_user(f"iapp_{SUF}", f"收票申请人{SUF}", "申请人", employee_id=emp)
FIN_TOKEN, FIN_ID = make_user(f"ifin_{SUF}", f"收票财务{SUF}", "财务", employee_id=emp)
print("  账号就绪：申请人 / 财务")

# ------------------------------------------------------------------ 识别接口

print("\n\033[1m== 发票自动识别 ==\033[0m")
raw, ctype = multipart([("file", "dzfp.ofd", make_ofd(), "application/ofd"),
                        ("subject", None, "【发票】合成样本供应商甲有限公司", None)])
def _must(cond, msg):
    """probe 里断言失败要抛异常——expect() 会把 probe 异常算作 FAIL。"""
    if not cond:
        raise AssertionError(msg)


res = expect("识别 OFD 数电票", "POST", "/api/inbox/recognize", 200, token=FIN_TOKEN,
             raw=raw, ctype=ctype,
             probe=lambda d, _: f"号码={d.get('invoice_no')} 金额={d.get('amount')} 来源={d.get('source')}")
expect("识别出完整 20 位号码", "POST", "/api/inbox/recognize", 200, token=FIN_TOKEN,
       raw=raw, ctype=ctype,
       probe=lambda d, _: (
           _must(d.get("invoice_no") == inv_no(1), f"号码不符：{d.get('invoice_no')}"),
           str(d.get("invoice_no")),
       )[1])
expect("识别出金额与销售方", "POST", "/api/inbox/recognize", 200, token=FIN_TOKEN,
       raw=raw, ctype=ctype,
       probe=lambda d, _: (
           _must(d.get("amount") == 2450.0, f"金额不符：{d.get('amount')}"),
           _must(d.get("seller_name") == "合成样本供应商甲有限公司", f"销售方不符：{d.get('seller_name')}"),
           f"{d.get('amount')} / {d.get('seller_name')} / 置信度 {d.get('confidence')}",
       )[2])

raw_pdf, ctype_pdf = multipart([
    ("file", "fapiao.pdf", b"%PDF-1.4\n1 0 obj\n<< /Length 60 /Filter /FlateDecode >>\nstream\n"
     b"x\x9c\x53\x48\x2d\x2e\xe1\x02\x00\x0c\x8b\x02\x5e\nendstream\nendobj\n%%EOF",
     "application/pdf"),
])
expect("识别 PDF（拿不到内容也不报错）", "POST", "/api/inbox/recognize", 200, token=FIN_TOKEN,
       raw=raw_pdf, ctype=ctype_pdf, probe=lambda d, _: f"来源={d.get('source')} 置信度={d.get('confidence')}")

raw_exe, ctype_exe = multipart([("file", "evil.exe", EXE, "application/octet-stream")])
# /recognize 只做解析、不落盘，所以对后缀宽松：拿不出字段也返回 200 并给出空结果。
expect("识别接口对不支持的后缀不报错（不落盘）", "POST", "/api/inbox/recognize", 200, token=FIN_TOKEN,
       raw=raw_exe, ctype=ctype_exe,
       probe=lambda d, _: f"已识别={d.get('recognized')} 置信度={d.get('confidence')}")
# 真正危险的是入库路径：这里必须硬拒。
expect("导入非白名单后缀被拒", "POST", "/api/inbox/import", 400, token=FIN_TOKEN,
       raw=raw_exe, ctype=ctype_exe)
raw_noext, ctype_noext = multipart([("file", "noext", OFD_XML.encode(), "application/octet-stream")])
expect("无后缀文件被拒（导入）", "POST", "/api/inbox/import", 400, token=FIN_TOKEN,
       raw=raw_noext, ctype=ctype_noext)
must_deny("未登录不能识别", "POST", "/api/inbox/recognize", raw=raw, ctype=ctype)
must_deny("申请人不能识别（财务动作）", "POST", "/api/inbox/recognize", raw=raw, ctype=ctype, token=APP_TOKEN)

# ------------------------------------------------------------------ 手工导入入账

print("\n\033[1m== 手工导入入账 ==\033[0m")
IMG_NO = inv_no(2)
raw2, ctype2 = multipart([
    ("file", "shoudong.ofd", make_ofd(IMG_NO), "application/ofd"),
    ("subject", None, "手工上传", None),
])
imp = expect("导入 OFD 入账", "POST", "/api/inbox/import", 200, token=FIN_TOKEN, raw=raw2, ctype=ctype2,
             probe=lambda d, _: f"发票号={d.get('invoice_no')} 已识别={'是' if d.get('recognized') else '否'}")
new_inv_id = need("入账返回 invoice_id", (imp or {}).get("invoice_id"), "入账 200 却没给出单据 id")

if new_inv_id:
    expect("导入的发票带来源与识别痕迹", "GET", f"/api/invoices/{new_inv_id}", 200, token=FIN_TOKEN,
           probe=lambda d, _: (
               _must(d.get("source") == "批量导入", f"来源错误：{d.get('source')}"),
               _must(d.get("recognize_from") == "ofd", f"识别来源错误：{d.get('recognize_from')}"),
               _must((d.get("recognize_score") or 0) > 0, "置信度缺失"),
               _must(d.get("invoice_no") == IMG_NO, f"号码错误：{d.get('invoice_no')}"),
               f"来源={d.get('source')} / 识别={d.get('recognize_from')} / 置信度={d.get('recognize_score')}",
           )[4])
    expect("导入的发票带上了影像", "GET", f"/api/invoices/{new_inv_id}/attachments", 200, token=FIN_TOKEN,
           probe=lambda d, _: f"{len(d)} 个影像")
    # 识别引擎拿得到税号、入账环节却曾把它丢掉，导致台账里购销双方税号永远空白——
    # 这是「识别信息不全」最隐蔽的一处，必须钉死
    expect("导入的发票带上购销双方税号", "GET", f"/api/invoices/{new_inv_id}", 200, token=FIN_TOKEN,
           probe=lambda d, _: (
               _must(d.get("seller_tax_no") == "91440300MA5F1234XA", f"销售方税号丢失：{d.get('seller_tax_no')!r}"),
               _must(d.get("buyer_tax_no") == "91440300MA5G5678XB", f"购买方税号丢失：{d.get('buyer_tax_no')!r}"),
               f"{d.get('seller_tax_no')} / {d.get('buyer_tax_no')}",
           )[2])

# 人工修正：表单一律压过识别结果；传空值表示「人工确认这里就是空的」，
# 不能被识别结果又填回去
FIX_NO = inv_no(3)
raw5, ctype5 = multipart([
    ("file", "shougong-fix.ofd", make_ofd(FIX_NO), "application/ofd"),
    ("seller_name", None, "人工改正后的供应商有限公司", None),
    ("amount", None, "999.99", None),
    ("buyer_tax_no", None, "", None),
])
fixed = expect("导入时人工修正的字段生效", "POST", "/api/inbox/import", 200, token=FIN_TOKEN,
               raw=raw5, ctype=ctype5,
               probe=lambda d, _: f"金额={d.get('amount')} 号码={d.get('invoice_no')}")
fix_id = need("导入返回 invoice_id", (fixed or {}).get("invoice_id"), "入账 200 却没给出单据 id")
if fix_id:
    expect("覆盖值与空值都按人工意图落库", "GET", f"/api/invoices/{fix_id}", 200, token=FIN_TOKEN,
           probe=lambda d, _: (
               _must(d.get("seller_name") == "人工改正后的供应商有限公司", f"销售方未覆盖：{d.get('seller_name')!r}"),
               _must(float(d.get("amount") or 0) == 999.99, f"金额未覆盖：{d.get('amount')!r}"),
               _must(not d.get("buyer_tax_no"), f"空值被识别结果填回去了：{d.get('buyer_tax_no')!r}"),
               _must(d.get("buyer_name") == "合成样本采购方乙有限公司", "未改动的字段不该被清空"),
               f"销售方={d.get('seller_name')} / 金额={d.get('amount')} / 购买方税号={d.get('buyer_tax_no')!r}",
           )[4])

# 重复导入：应识别为重复但不阻断（由「重复报销检测」页签统一呈现）
raw3, ctype3 = multipart([("file", "again.ofd", make_ofd(IMG_NO), "application/ofd")])
expect("同一张发票重复导入被标记", "POST", "/api/inbox/import", 200, token=FIN_TOKEN, raw=raw3, ctype=ctype3,
       probe=lambda d, _: f"duplicate={d.get('duplicate')} 提示={d.get('note', '')[:40]}")

# 完全识别不出来的图片：必须占位入账，不能凭空消失
raw4, ctype4 = multipart([("file", "photo.jpg", JPEG, "image/jpeg")])
ph = expect("识别不了的图片仍占位入账", "POST", "/api/inbox/import", 200, token=FIN_TOKEN,
            raw=raw4, ctype=ctype4,
            probe=lambda d, _: f"号码={d.get('invoice_no')} 提示={d.get('note', '')[:30]}")
expect("占位号形如「待识别-」", "POST", "/api/inbox/import", 200, token=FIN_TOKEN, raw=raw4, ctype=ctype4,
       probe=lambda d, _: "占位" if str(d.get("invoice_no", "")).startswith("待识别-") else f"意外号码 {d.get('invoice_no')}")
must_deny("申请人不能手工导入", "POST", "/api/inbox/import", raw=raw2, ctype=ctype2, token=APP_TOKEN)

# ------------------------------------------------------------------ 邮箱账号

print("\n\033[1m== 邮箱账号管理 ==\033[0m")
must_deny("申请人看不到邮箱配置", "GET", "/api/mail-accounts", token=APP_TOKEN)
must_deny("财务不能新建邮箱账号（仅管理员）", "POST", "/api/mail-accounts",
          {"name": "x", "host": "h", "username": "u"}, token=FIN_TOKEN)

acc = expect("管理员新建邮箱账号", "POST", "/api/mail-accounts",
             {"name": f"收票箱-{SUF}", "host": "imap.invalid.local", "port": 993,
              "use_ssl": True, "username": "finance@example.com",
              "password": "MailAuth@2026", "folder": "INBOX",
              "subject_keywords": "发票,电子发票", "since_days": 7, "max_per_sync": 5},
             201, token=TOKEN,
             probe=lambda d, _: f"id={d.get('id')} 名称={d.get('name')}")
acc_id = need("建邮箱账号返回 id", (acc or {}).get("id"), "创建邮箱账号失败，后续连接/收票断言无从执行")

lst = expect("邮箱列表可读（财务）", "GET", "/api/mail-accounts", 200, token=FIN_TOKEN)
expect("列表不返回明文密码", "GET", "/api/mail-accounts", 200, token=FIN_TOKEN,
       probe=lambda d, _: "已脱敏" if d and "MailAuth@2026" not in json.dumps(d, ensure_ascii=False)
       else "明文泄露！")
expect("账号标记了已设置密码", "GET", "/api/mail-accounts", 200, token=FIN_TOKEN,
       probe=lambda d, _: f"has_password={d[0].get('has_password') if d else None}")

if acc_id:
    expect("测试连接：不可达主机给出明确失败", "POST", f"/api/mail-accounts/{acc_id}/test", 200, token=TOKEN,
           probe=lambda d, _: f"ok={d.get('ok')} 信息={str(d.get('message'))[:60]}")
    expect("立即收票：不可达时不崩溃，返回 400", "POST", f"/api/mail-accounts/{acc_id}/sync", 400, token=FIN_TOKEN,
           probe=lambda d, _: f"提示={str(d)[:80]}")
    expect("失败状态已落库，便于排查", "GET", "/api/mail-accounts", 200, token=FIN_TOKEN,
           probe=lambda d, _: f"状态={d[0].get('last_sync_status')} 明细={str(d[0].get('last_sync_detail'))[:50]}")

    expect("停用邮箱账号", "PUT", f"/api/mail-accounts/{acc_id}",
           {"active": False}, 200, token=TOKEN, probe=lambda d, _: f"active={d.get('active')}")
    expect("停用后收票被拒", "POST", f"/api/mail-accounts/{acc_id}/sync", 400, token=FIN_TOKEN)
    expect("重新启用", "PUT", f"/api/mail-accounts/{acc_id}", {"active": True}, 200, token=TOKEN)

    # 密码留空表示不修改，避免前端把脱敏串提交回来覆盖真实密码
    expect("留空密码不覆盖原密码", "PUT", f"/api/mail-accounts/{acc_id}",
           {"password": ""}, 200, token=TOKEN,
           probe=lambda d, _: f"has_password={d.get('has_password')}")
    expect("改密码生效", "PUT", f"/api/mail-accounts/{acc_id}",
           {"password": "NewAuth@2026"}, 200, token=TOKEN,
           probe=lambda d, _: f"has_password={d.get('has_password')}")

    acc2 = expect("重名校验", "POST", "/api/mail-accounts",
                  {"name": f"收票箱-{SUF}", "host": "h", "username": "u"}, 400, token=TOKEN)

# ------------------------------------------------------------------ 外部投递（Agent Mail 通道）

print("\n\033[1m== 外部投递通道（Agent Mail 等外部邮箱对接）==\033[0m")
ING_NO = inv_no(3)
raw_i, ctype_i = multipart([
    ("files", "agentmail.ofd", make_ofd(ING_NO), "application/ofd"),
    ("subject", None, "【发票】来自智能体邮箱", None),
    ("sender", None, "agent-mail@example.com", None),
])
must_deny("无令牌投递被拒", "POST", "/api/inbox/ingest", raw=raw_i, ctype=ctype_i)

if INGEST_TOKEN:
    hdr = {"X-Ingest-Token": INGEST_TOKEN, "X-Message-Id": f"ing-{SUF}-001"}
    expect("带令牌投递成功", "POST", "/api/inbox/ingest", 200, raw=raw_i, ctype=ctype_i, headers=hdr,
           probe=lambda d, _: f"入库={d.get('imported')} 失败={d.get('failed')}")
    expect("同一 Message-ID 重复投递幂等", "POST", "/api/inbox/ingest", 200, raw=raw_i, ctype=ctype_i, headers=hdr,
           probe=lambda d, _: f"duplicate={d.get('duplicate')} 入库={d.get('imported')}")
    expect("错误令牌被拒", "POST", "/api/inbox/ingest", (401, 403), raw=raw_i, ctype=ctype_i,
           headers={"X-Ingest-Token": "wrong-token"})
    expect("登录态也可投递", "POST", "/api/inbox/ingest", 200, raw=raw_i, ctype=ctype_i,
           token=FIN_TOKEN, headers={"X-Message-Id": f"ing-{SUF}-002"},
           probe=lambda d, _: f"入库={d.get('imported')}")
else:
    skip("外部投递成功路径（4 项）", "未设置 WB_INGEST_TOKEN")

# ------------------------------------------------------------------ 收件记录

print("\n\033[1m== 收件记录 ==\033[0m")
msgs = expect("收件记录可查（财务）", "GET", "/api/inbox/messages", 200, token=FIN_TOKEN,
              params={"page_size": 50},
              probe=lambda d, _: f"共 {d.get('total')} 条，入库 {d.get('summary', {}).get('imported')} 张")
must_deny("申请人看不到收件记录", "GET", "/api/inbox/messages", token=APP_TOKEN)

msg_ids = [x["id"] for x in (msgs or {}).get("items", [])][:3]
if msg_ids:
    expect("批量删除收件记录", "POST", "/api/inbox/messages/batch-delete",
           want=200, body={"ids": msg_ids}, token=FIN_TOKEN,
           probe=lambda d, _: f"删除 {d.get('deleted')} 条")
else:
    skip("批量删除收件记录", "收件箱里没有可删的记录（投递路径未跑通）")
expect("空 ids 被拒", "POST", "/api/inbox/messages/batch-delete",
       want=400, body={"ids": []}, token=FIN_TOKEN)
must_deny("申请人不能删收件记录", "POST", "/api/inbox/messages/batch-delete", token=APP_TOKEN,
          body={"ids": [1]})

# ------------------------------------------------------------------ 批量删除

print("\n\033[1m== 批量删除 ==\033[0m")


def make_invoice(no, amount):
    st, d, _ = req("POST", "/api/invoices", {
        "invoice_no": no, "amount": amount, "invoice_date": "2026-09-12",
        "seller_name": "批量删除测试供应商",
    }, token=FIN_TOKEN)
    return d["id"] if st == 201 else None


del_ids = []
for i in range(3):
    got = make_invoice(f"DEL{SUF}{i:02d}", 100 + i)
    if got:
        del_ids.append(got)

# 给其中一张挂影像，验证批量删除会把文件一起清掉
att_id = None
if del_ids:
    # 文件名必须与字节内容一致：后端会按魔数校验「后缀 vs 实际类型」，
    # 拿 .png 的名字传 JPEG 字节会被正确拒掉（400 文件类型与后缀不匹配）。
    # 这里早先就是 .png + JPEG，导致挂影像一直失败、后续那条断言被静默跳过。
    raw_png, ctype_png = multipart([("file", "del.jpg", JPEG, "image/jpeg")])
    st, d, _ = req("POST", f"/api/invoices/{del_ids[0]}/attachments", raw=raw_png, ctype=ctype_png,
                   token=FIN_TOKEN)
    att_id = need("上传影像返回 id", d.get("id") if st == 201 and isinstance(d, dict) else None,
                  f"给发票挂影像失败：HTTP {st} {str(d)[:120]}")

expect("空 ids 被拒", "POST", "/api/invoices/batch-delete",
       want=400, body={"ids": []}, token=FIN_TOKEN)
must_deny("申请人不能批量删除发票", "POST", "/api/invoices/batch-delete", token=APP_TOKEN,
          body={"ids": del_ids})

expect("财务批量删除发票", "POST", "/api/invoices/batch-delete",
       want=200, body={"ids": del_ids}, token=FIN_TOKEN,
       probe=lambda d, _: f"删除 {d.get('deleted')} 张，清理文件 {d.get('files_removed')} 个")
expect("重复删除同一批已无对象", "POST", "/api/invoices/batch-delete",
       want=200, body={"ids": del_ids}, token=FIN_TOKEN,
       probe=lambda d, _: f"denied={d.get('denied_count')}")
if att_id:
    expect("影像文件已随发票删除", "GET", f"/api/attachments/{att_id}/raw", 404, token=FIN_TOKEN)

# 报销单批量删除：草稿可删、待审批必须被拦下并给原因
draft = expect("建草稿单", "POST", "/api/reimbursements",
               {"title": f"批量删除草稿-{SUF}", "applicant_id": emp, "department_id": dept,
                "items": [{"category_id": cat, "occur_date": "2026-09-12", "amount": 88.0}]},
               201, token=FIN_TOKEN)["id"]
pend = expect("建待审批单", "POST", "/api/reimbursements",
              {"title": f"批量删除待审-{SUF}", "applicant_id": emp, "department_id": dept,
               "items": [{"category_id": cat, "occur_date": "2026-09-12", "amount": 66.0}]},
              201, token=FIN_TOKEN)["id"]
expect("提交使其进入待审批", "POST", f"/api/reimbursements/{pend}/submit", 200, token=FIN_TOKEN)

expect("批量删除报销单：能删的删、不能删的说明原因", "POST", "/api/reimbursements/batch-delete",
       want=200, body={"ids": [draft, pend]}, token=FIN_TOKEN,
       probe=lambda d, _: f"删除 {d.get('deleted')}，跳过 {d.get('skipped_count')}："
                          f"{'；'.join(x.get('reason', '') for x in d.get('skipped', []))}")
expect("空 ids 被拒（报销单）", "POST", "/api/reimbursements/batch-delete",
       want=400, body={"ids": []}, token=FIN_TOKEN)
must_deny("申请人不能清空报销单", "POST", "/api/reimbursements/purge-all", token=APP_TOKEN,
          body={"confirm": "清空全部报销单"})

# 主数据批量删除：被引用的必须被拦下
d1 = expect("建待删部门", "POST", "/api/departments",
            {"name": f"待删部门-{SUF}", "code": f"DP{SUF}"}, 201, token=TOKEN)["id"]
d2 = expect("建另一待删部门", "POST", "/api/departments",
            {"name": f"待删部门2-{SUF}", "code": f"DP2{SUF}"}, 201, token=TOKEN)["id"]
expect("批量删除部门：被引用跳过、干净的删除", "POST", "/api/departments/batch-delete",
       want=200, body={"ids": [dept, d1, d2]}, token=TOKEN,
       probe=lambda d, _: f"删除 {d.get('deleted')}，跳过 {d.get('skipped_count')}："
                          f"{'；'.join(x.get('reason', '') for x in d.get('skipped', []))}")
must_deny("财务不能批量删除部门（仅管理员）", "POST", "/api/departments/batch-delete",
          token=FIN_TOKEN, body={"ids": [d1]})
expect("批量删除员工（被报销单引用的跳过）", "POST", "/api/employees/batch-delete",
       want=200, body={"ids": [emp]}, token=TOKEN,
       probe=lambda d, _: f"删除 {d.get('deleted')}，跳过 {d.get('skipped_count')}")
expect("批量删除费用类型", "POST", "/api/categories/batch-delete",
       want=200, body={"ids": [cat]}, token=TOKEN,
       probe=lambda d, _: f"删除 {d.get('deleted')}，跳过 {d.get('skipped_count')}")

# 审计日志
logs = expect("审计日志可查", "GET", "/api/audit-logs", 200, token=TOKEN, params={"page_size": 5},
              probe=lambda d, _: f"共 {d.get('total')} 条")
must_deny("财务不能清理审计日志", "POST", "/api/audit-logs/batch-delete", token=FIN_TOKEN, body={"ids": [1]})
log_ids = [x["id"] for x in (logs or {}).get("items", [])]
if log_ids:
    expect("批量删除审计日志", "POST", "/api/audit-logs/batch-delete",
           want=200, body={"ids": log_ids[:2]}, token=TOKEN,
           probe=lambda d, _: f"删除 {d.get('deleted')} 条")
expect("清空审计日志需要确认词", "POST", "/api/audit-logs/purge",
       want=400, body={"confirm": "随便打的"}, token=TOKEN)
expect("清空审计日志（确认词正确）", "POST", "/api/audit-logs/purge",
       want=200, body={"confirm": "清空审计日志"}, params={"before_days": 0}, token=TOKEN,
       probe=lambda d, _: f"范围={d.get('scope')} 删除 {d.get('deleted')} 条")

# 清空类接口的确认词护栏
expect("清空全部发票需要确认词", "POST", "/api/invoices/purge-all",
       want=400, body={"confirm": "错了"}, token=FIN_TOKEN)
expect("清空散票需要对应确认词", "POST", "/api/invoices/purge-all",
       want=400, body={"confirm": "清空全部发票"}, params={"only_unlinked": True}, token=FIN_TOKEN)
expect("清空散票（确认词正确）", "POST", "/api/invoices/purge-all",
       want=200, body={"confirm": "清空散票"}, params={"only_unlinked": True}, token=FIN_TOKEN,
       probe=lambda d, _: f"删除 {d.get('deleted')} 张，清理文件 {d.get('files_removed')} 个")
must_deny("申请人不能清空发票", "POST", "/api/invoices/purge-all", token=APP_TOKEN,
          body={"confirm": "清空全部发票"})

# 收件记录清空（管理员）
expect("清空收件记录需要确认词", "POST", "/api/inbox/messages/purge",
       want=400, body={"confirm": "x"}, token=TOKEN)
expect("清空收件记录", "POST", "/api/inbox/messages/purge",
       want=200, body={"confirm": "清空收件记录"}, token=TOKEN,
       probe=lambda d, _: f"删除 {d.get('deleted')} 条")
must_deny("财务不能清空收件记录（仅管理员）", "POST", "/api/inbox/messages/purge", token=FIN_TOKEN,
          body={"confirm": "清空收件记录"})

print(f"\n结果：{OK} 通过 / {FAIL} 失败" + (f" / {len(SKIPPED)} 跳过" if SKIPPED else ""))
if SKIPPED:
    # 跳过必须点名。历史上这里只打印过一行黄字，跑完看「72 通过 / 0 失败」
    # 会误以为全绿，实际上外部投递与影像清理那几条根本没执行。
    print("跳过的断言：")
    for item in SKIPPED:
        print(f"  - {item}")
    print("提示：外部投递相关断言需要 WB_INGEST_TOKEN，与容器的 INBOX_INGEST_TOKEN 保持一致")
sys.exit(1 if FAIL else 0)
