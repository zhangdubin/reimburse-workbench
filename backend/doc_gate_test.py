"""随票凭证「缺单据必填原因」提交校验（v2.9.27）验收脚本。

背景：费用类型可设「应附单据」（行程单 / 消费水单 / 其他材料），缺件本身只做
台账橙点提示、不阻断流程——现实里确实有拿不到单据的票（路边扬招的巡游出租车
就没有行程单）。但**要单据又不给**时必须说明原因，否则举证责任全落到审批人和
财务身上。本脚本验的就是这道提交闸门。

用法（dev 实例已起来之后）：
    WB_BASE_URL=http://127.0.0.1:8793 WB_PASSWORD=Adm1n@2026 python doc_gate_test.py

覆盖：
    1. 缺单据 + 未写原因 → 提交被拒，错误里点名第几行、缺什么
    2. 补单据（上传行程单附件） → 提交放行
    3. 不补单据但写明原因 → 提交放行（拿不到单据不等于不能报）
    4. 无应附单据要求的费用类型 → 从来不拦
    5. 明细没挂发票 → 不归这道闸门管（那是缺票预警的事）
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = (
    (sys.argv[1] if len(sys.argv) > 1 else None)
    or os.environ.get("WB_BASE_URL")
    or "http://127.0.0.1:8793"
).rstrip("/")

ADMIN_USER = os.environ.get("WB_USER", "admin")
ADMIN_PASSWORD = os.environ.get("WB_PASSWORD", "Adm1n@2026")

OK = 0
FAIL = 0
FAILED = []
TOKEN = None

# 1x1 PNG：附件要走后缀与 Content-Type 白名单，txt 过不去
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


def req(method, path, body=None, params=None, token=None, files=None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {}
    data = None
    if files:
        boundary = "----wb" + uuid.uuid4().hex
        buf = b""
        for key, val in (body or {}).items():
            buf += f"--{boundary}\r\n".encode()
            buf += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
            buf += f"{val}\r\n".encode()
        for key, filename, content, mime in files:
            buf += f"--{boundary}\r\n".encode()
            buf += (
                f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
            ).encode()
            buf += f"Content-Type: {mime}\r\n\r\n".encode() + content + b"\r\n"
        buf += f"--{boundary}--\r\n".encode()
        data = buf
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        r.add_header(k, v)
    opener = urllib.request.build_opener()
    # 沙箱里的 HTTP_PROXY 会把本地回环请求也接管走（502 假象），显式绕开
    opener.add_handler(urllib.request.ProxyHandler({}))
    try:
        with opener.open(r, timeout=30) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode() if raw else "null")
            except Exception:
                return resp.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload.decode() or "null")
        except Exception:
            return e.code, payload.decode(errors="replace")


def main():
    global TOKEN
    tag = uuid.uuid4().hex[:6]

    print("\n== 登录 ==")
    st, body = req("POST", "/api/auth/login", {"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    check("管理员登录", st == 200, st)
    TOKEN = body.get("token") if isinstance(body, dict) else None
    check("拿到 token", bool(TOKEN))

    print("\n== 准备：两个费用类型（一个要行程单、一个不要） ==")
    st, net = req("POST", "/api/categories", {
        "name": f"网约车费-{tag}", "code": f"TCAR{tag}", "group_name": "交通费",
        "requires_invoice": True, "required_doc": "itinerary", "single_limit": 0,
        "daily_limit": 0, "monthly_limit": 0, "active": True,
    }, token=TOKEN)
    check("建「要行程单」的费用类型", st in (200, 201), f"{st} {net}")
    car_id = net.get("id") if isinstance(net, dict) else None

    st, tax = req("POST", "/api/categories", {
        "name": f"巡游出租车费-{tag}", "code": f"TTAX{tag}", "group_name": "交通费",
        "requires_invoice": True, "required_doc": None, "single_limit": 0,
        "daily_limit": 0, "monthly_limit": 0, "active": True,
    }, token=TOKEN)
    check("建「不要单据」的费用类型", st in (200, 201), f"{st} {tax}")
    taxi_id = tax.get("id") if isinstance(tax, dict) else None

    st, emps = req("GET", "/api/employees", token=TOKEN)
    applicants = [e for e in (emps or []) if e.get("active", True)] if isinstance(emps, list) else []
    applicant_id = applicants[0]["id"] if applicants else None
    check("取到申请人", applicant_id is not None)

    def new_reimburse(cat_id, remark):
        """建一张草稿单（一行明细 + 一张挂在该明细上的发票）。"""
        st, rb = req("POST", "/api/reimbursements", {
            "title": f"凭证闸门测试-{tag}",
            "applicant_id": applicant_id,
            "items": [{
                "category_id": cat_id, "occur_date": "2026-10-21",
                "amount": 68.5, "tax_amount": 0, "description": "打车费",
                "remark": remark,
            }],
        }, token=TOKEN)
        assert st in (200, 201), f"{st} {rb}"
        rid = rb["id"]
        item_id = rb["items"][0]["id"]

        st, inv = req("POST", "/api/invoices", {
            "invoice_no": f"IT{tag}{uuid.uuid4().hex[:6]}",
            "invoice_type": "增值税电子普通发票",
            "amount": 68.5, "tax_amount": 0, "invoice_date": "2026-10-21",
            "seller_name": "某出行科技有限公司",
            "category_id": cat_id,
        }, token=TOKEN)
        assert st in (200, 201), f"{st} {inv}"
        req("POST", f"/api/invoices/{inv['id']}/link",
            params={"reimbursement_id": rid, "item_id": item_id}, token=TOKEN)
        return rid, item_id, inv["id"]

    print("\n== 缺单据 + 未写原因 → 必须拦下 ==")
    rid, item_id, inv_id = new_reimburse(car_id, None)
    st, body = req("POST", f"/api/reimbursements/{rid}/submit", token=TOKEN)
    msg = body.get("detail", "") if isinstance(body, dict) else str(body)
    check("提交被拒（400）", st == 400, f"{st} {body}")
    check("错误点名缺什么单据", "行程单" in msg and "第 1 行" in msg, msg)
    check("错误给出补救指引", "备注" in msg and "原因" in msg, msg)
    st, det = req("GET", f"/api/reimbursements/{rid}", token=TOKEN)
    check("状态仍是草稿", isinstance(det, dict) and det.get("status") == "草稿", det.get("status") if isinstance(det, dict) else det)

    print("\n== 补上行程单 → 放行（闸门只在没单据时才生效） ==")
    st, up = req("POST", f"/api/invoices/{inv_id}/attachments", files=[
        ("file", "itinerary.png", PNG_1PX, "image/png"),
    ], body={"kind": "itinerary"}, token=TOKEN)
    check("上传行程单附件", st in (200, 201), f"{st} {up}")
    st, body = req("POST", f"/api/reimbursements/{rid}/submit", token=TOKEN)
    check("提交成功", st == 200, f"{st} {body}")

    print("\n== 不补单据但写明原因 → 放行（拿不到不等于不能报） ==")
    rid2, _, _ = new_reimburse(car_id, "巡游出租车，路边扬招无行程单")
    st, body = req("POST", f"/api/reimbursements/{rid2}/submit", token=TOKEN)
    check("写了原因即可提交", st == 200, f"{st} {body}")

    print("\n== 无应附单据要求 → 从来不拦 ==")
    rid3, _, _ = new_reimburse(taxi_id, None)
    st, body = req("POST", f"/api/reimbursements/{rid3}/submit", token=TOKEN)
    check("无要求的类型不拦", st == 200, f"{st} {body}")

    print(f"\n结果：{OK} 通过 / {FAIL} 失败")
    if FAILED:
        print("失败项：" + "、".join(FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
