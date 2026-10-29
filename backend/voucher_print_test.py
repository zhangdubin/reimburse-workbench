"""凭证打印数据接口（v2.9.29）验收脚本。

打印这件事前端负责排版，但「哪些票能打、附件按什么顺序出、有没有越权」
必须后端说了算——本脚本验的就是这条数据链路。

用法（dev 实例已起来之后）：
    WB_BASE_URL=http://127.0.0.1:8792 python voucher_print_test.py http://127.0.0.1:8792

覆盖：
    1. 入参与鉴权：未登录 401 / 空参数 400 / 非数字 id 400 / 单不存在 404
    2. 清单结构：票面摘要、附件 media 判定（pdf/image/other）、统计口径
    3. 打印顺序：发票影像 → 行程单 → 消费水单 → 其他（跟贴票顺序一致）
    4. 越权：别人的报销单 403；ids 模式越权的票被过滤掉而不是静默返回
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
    or "http://127.0.0.1:8792"
).rstrip("/")

ADMIN_USER = os.environ.get("WB_USER", "admin")
ADMIN_PASSWORD = os.environ.get("WB_PASSWORD", "Adm1n@2026")

OK = 0
FAIL = 0
FAILED = []
TOKEN = None

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

# 最小可用 PDF（数电票就是 PDF，这里只关心 mime 判定与 media=pdf 分支）
PDF_MIN = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
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


def req_raw(method, path, token=None):
    """拿原始响应（状态码、文本、Content-Type），给 SVG 端点用。"""
    url = BASE + path
    headers = {"Authorization": "Bearer " + token} if token else {}
    r = urllib.request.Request(url, method=method)
    for k, v in headers.items():
        r.add_header(k, v)
    opener = urllib.request.build_opener()
    opener.add_handler(urllib.request.ProxyHandler({}))
    try:
        with opener.open(r, timeout=30) as resp:
            return resp.status, resp.read().decode(errors="replace"), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace"), e.headers.get("Content-Type", "")


def main():
    global TOKEN
    tag = uuid.uuid4().hex[:6]

    print("\n== 登录 ==")
    st, body = req("POST", "/api/auth/login", {"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    check("管理员登录", st == 200, st)
    TOKEN = body.get("token") if isinstance(body, dict) else None
    check("拿到 token", bool(TOKEN))

    print("\n== 入参与鉴权 ==")
    st, _ = req("GET", "/api/print/vouchers", params={"ids": "1"})
    check("未登录 401", st == 401, st)
    st, _ = req("GET", "/api/print/vouchers", token=TOKEN)
    check("既不传 ids 也不传单号 400", st == 400, st)
    st, _ = req("GET", "/api/print/vouchers", params={"ids": "1,abc"}, token=TOKEN)
    check("非数字发票 id 400", st == 400, st)
    st, _ = req("GET", "/api/print/vouchers", params={"reimbursement_id": 99999999}, token=TOKEN)
    check("报销单不存在 404", st == 404, st)

    print("\n== 准备数据：一张单 + 一张票 + 四类凭证 ==")
    st, emps = req("GET", "/api/employees", token=TOKEN)
    applicant_id = emps[0]["id"] if isinstance(emps, list) and emps else None
    check("取到申请人", applicant_id is not None)

    st, rb = req("POST", "/api/reimbursements", {
        "title": f"凭证打印测试-{tag}",
        "applicant_id": applicant_id,
        "items": [{
            "category_id": None, "occur_date": "2026-10-22",
            "amount": 268.0, "tax_amount": 0, "description": "差旅杂费",
        }],
    }, token=TOKEN)
    check("建报销单", st in (200, 201), f"{st} {rb}")
    rid = rb["id"]
    item_id = rb["items"][0]["id"]

    st, inv = req("POST", "/api/invoices", {
        "invoice_no": f"VP{tag}",
        "invoice_type": "电子发票（普通发票）",
        "amount": 268.0, "tax_amount": 0, "invoice_date": "2026-10-22",
        "seller_name": "某某酒店管理有限公司",
    }, token=TOKEN)
    check("建发票", st in (200, 201), f"{st} {inv}")
    inv_id = inv["id"]
    req("POST", f"/api/invoices/{inv_id}/link",
        params={"reimbursement_id": rid, "item_id": item_id}, token=TOKEN)

    # 故意乱序上传，验证接口会按打印顺序重排
    for kind, name, payload, mime in [
        ("other", "other.png", PNG_1PX, "image/png"),
        ("folio", "folio.png", PNG_1PX, "image/png"),
        ("invoice", "invoice.pdf", PDF_MIN, "application/pdf"),
        ("itinerary", "itinerary.png", PNG_1PX, "image/png"),
    ]:
        st, _ = req(
            "POST", f"/api/invoices/{inv_id}/attachments",
            files=[("file", name, payload, mime)],
            body={"kind": kind}, token=TOKEN,
        )
        check(f"上传凭证 [{kind}]", st in (200, 201), st)

    print("\n== 按报销单取清单 ==")
    st, d = req("GET", "/api/print/vouchers", params={"reimbursement_id": rid}, token=TOKEN)
    check("接口 200", st == 200, f"{st} {d}")
    check("返回报销单摘要", isinstance(d.get("reimbursement"), dict) and d["reimbursement"].get("code"), d.get("reimbursement"))
    check("带出 1 张发票", d["stats"]["invoice_count"] == 1, d["stats"])
    check("带出 4 份凭证", d["stats"]["doc_count"] == 4, d["stats"])
    check("PDF 判定 1 份", d["stats"]["pdf_count"] == 1, d["stats"])
    check("图片判定 3 份", d["stats"]["image_count"] == 3, d["stats"])

    kinds = [a["kind"] for a in d["invoices"][0]["attachments"]]
    check("打印顺序：发票→行程单→水单→其他", kinds == ["invoice", "itinerary", "folio", "other"], kinds)
    medias = [a["media"] for a in d["invoices"][0]["attachments"]]
    check("media 判定正确", medias == ["pdf", "image", "image", "image"], medias)
    check("票面摘要带金额与销售方",
          d["invoices"][0]["amount"] == 268.0 and "酒店" in (d["invoices"][0]["seller_name"] or ""),
          d["invoices"][0])
    check("附件带可下载地址", d["invoices"][0]["attachments"][0]["url"].startswith("/api/attachments/"),
          d["invoices"][0]["attachments"][0])
    check("附件带扩展名字段", all("ext" in a for a in d["invoices"][0]["attachments"]),
          d["invoices"][0]["attachments"][0])

    print("\n== mime 不可信时按后缀兜底 ==")
    inv2 = req("POST", "/api/invoices", {
        "invoice_no": f"VPE{tag}", "invoice_type": "电子发票（普通发票）",
        "amount": 66.0, "invoice_date": "2026-10-26", "seller_name": "兜底判定测试",
    }, token=TOKEN)[1]
    st, _ = req("POST", f"/api/invoices/{inv2['id']}/link",
                params={"reimbursement_id": rid, "item_id": item_id}, token=TOKEN)
    # 邮件收票/部分手机浏览器给不出 mime：全是 application/octet-stream，
    # 只认 mime 会把能打的图片/PDF 全判成「原件」，打印出来就是占位卡
    for kind, name, payload in [
        ("invoice", "train_ticket.jpg", PNG_1PX),      # 假 jpg（只验判定，不验内容）
        ("itinerary", "eticket.pdf", PDF_MIN),
    ]:
        st, _ = req(
            "POST", f"/api/invoices/{inv2['id']}/attachments",
            files=[("file", name, payload, "application/octet-stream")],
            body={"kind": kind}, token=TOKEN,
        )
        check(f"octet-stream 上传 [{name}]", st in (200, 201), st)
    st, d5 = req("GET", "/api/print/vouchers", params={"ids": str(inv2["id"])}, token=TOKEN)
    medias2 = [a["media"] for a in d5["invoices"][0]["attachments"]]
    check("octet-stream 按后缀判成 image/pdf（不再误判原件）",
          medias2 == ["image", "pdf"], medias2)
    exts2 = [a["ext"] for a in d5["invoices"][0]["attachments"]]
    check("ext 字段给出真实后缀", exts2 == ["jpg", "pdf"], exts2)

    print("\n== OFD 原件：media 判定 + SVG 预览端点 ==")
    from ofd_render_test import build_ofd

    st, _ = req(
        "POST", f"/api/invoices/{inv2['id']}/attachments",
        files=[("file", "e_invoice.ofd", build_ofd(pages=2), "application/octet-stream")],
        body={"kind": "invoice"}, token=TOKEN,
    )
    check("OFD 上传（octet-stream）", st in (200, 201), st)
    st, d6 = req("GET", "/api/print/vouchers", params={"ids": str(inv2["id"])}, token=TOKEN)
    ofds = [a for a in d6["invoices"][0]["attachments"] if a.get("ext") == "ofd"]
    check("OFD 判成 media=ofd", len(ofds) == 1 and ofds[0]["media"] == "ofd",
          [a.get("media") for a in d6["invoices"][0]["attachments"]])
    check("OFD 给出 svg_url", bool(ofds) and ofds[0].get("svg_url", "").endswith("/ofd.svg"),
          ofds and ofds[0].get("svg_url"))
    if ofds:
        svg_url = ofds[0]["svg_url"]
        st, body, ctype = req_raw("GET", svg_url + "?page=1", token=TOKEN)
        check("SVG 第 1 页 200 且是 svg", st == 200 and ctype.startswith("image/svg+xml")
              and body.startswith("<svg"), f"{st} {ctype}")
        check("SVG 内容含票面文本", "第1页" in body, body[:80])
        st, body2, _ = req_raw("GET", svg_url + "?page=2", token=TOKEN)
        check("SVG 第 2 页 200（多页）", st == 200 and "第2页" in body2, st)
        st, _, _ = req_raw("GET", svg_url + "?page=3", token=TOKEN)
        check("SVG 超页 404", st == 404, st)
        st, _, _ = req_raw("GET", svg_url.replace("/ofd.svg", "/raw"), token=TOKEN)
        check("raw 原件仍可下载", st == 200, st)

    print("\n== 按 ids 取清单 ==")
    st, d2 = req("GET", "/api/print/vouchers", params={"ids": str(inv_id)}, token=TOKEN)
    check("ids 模式 200", st == 200, f"{st} {d2}")
    check("ids 模式不返回报销单摘要", d2.get("reimbursement") is None, d2.get("reimbursement"))
    check("ids 模式票据一致", [i["id"] for i in d2["invoices"]] == [inv_id], d2["invoices"])
    st, d3 = req("GET", "/api/print/vouchers", params={"ids": f"{inv_id},99999999"}, token=TOKEN)
    check("不存在的 id 被忽略并计入 missing", d3["stats"]["missing_invoices"] == 1, d3["stats"])

    print("\n== 越权 ==")
    username = f"vp{tag}"
    st, u = req("POST", "/api/users", {
        "username": username, "name": "凭证打印越权测试", "role": "申请人",
        "password": "Vp#Test2026x", "active": True,
    }, token=TOKEN)
    check("建一个无员工绑定的普通账号", st in (200, 201), f"{st} {u}")
    st, lg = req("POST", "/api/auth/login", {"username": username, "password": "Vp#Test2026x"})
    utoken = lg.get("token") if isinstance(lg, dict) else None
    check("普通账号登录", bool(utoken), st)

    st, _ = req("GET", "/api/print/vouchers", params={"reimbursement_id": rid}, token=utoken)
    check("别人的报销单 403", st == 403, st)
    st, d4 = req("GET", "/api/print/vouchers", params={"ids": str(inv_id)}, token=utoken)
    check("别人的票被过滤（不静默返回）",
          st == 200 and d4["invoices"] == [] and d4["stats"]["missing_invoices"] == 1, d4.get("stats"))

    print("\n== 报销单批量打印（v2.9.37）==")
    st, rb2 = req("POST", "/api/reimbursements", {
        "title": f"批量打印测试-{tag}",
        "applicant_id": applicant_id,
        "items": [{
            "category_id": None, "occur_date": "2026-10-23",
            "amount": 132.5, "tax_amount": 0, "description": "市内交通",
        }],
    }, token=TOKEN)
    check("再建一张报销单", st in (200, 201), f"{st} {rb2}")
    rid2 = rb2["id"]

    st, _ = req("GET", "/api/print/reimbursements")
    check("未登录 401", st == 401, st)
    st, _ = req("GET", "/api/print/reimbursements", token=TOKEN)
    check("空 ids 400", st == 400, st)
    st, _ = req("GET", "/api/print/reimbursements", params={"ids": "1,abc"}, token=TOKEN)
    check("非数字报销单 id 400", st == 400, st)
    st, _ = req("GET", "/api/print/reimbursements",
                params={"ids": ",".join(str(i) for i in range(1, 102))}, token=TOKEN)
    check("超过 100 张 400", st == 400, st)

    st, d5 = req("GET", "/api/print/reimbursements", params={"ids": f"{rid2},{rid}"}, token=TOKEN)
    check("一次取回 2 张单", st == 200 and d5.get("stats", {}).get("count") == 2, f"{st} {d5 if st != 200 else d5.get('stats')}")
    check("按传入顺序返回（贴票顺序不串行）",
          [i["id"] for i in d5.get("items", [])] == [rid2, rid],
          [i["id"] for i in d5.get("items", [])])
    first = (d5.get("items") or [{}])[0]
    check("带齐明细 / 关联发票 / 流转记录",
          {"items", "invoices", "logs"} <= set(first), sorted(first.keys())[:12])
    check("合计金额为两张单之和",
          abs((d5.get("stats") or {}).get("total_amount", 0) - 400.5) < 0.01,
          (d5.get("stats") or {}).get("total_amount"))

    st, d6 = req("GET", "/api/print/reimbursements", params={"ids": f"{rid},99999999"}, token=TOKEN)
    sk = (d6.get("stats") or {}).get("skipped") or []
    check("不存在的单被跳过并说明原因",
          (d6.get("stats") or {}).get("skipped_count") == 1 and "不存在" in sk[0].get("reason", ""),
          d6.get("stats"))
    check("跳过的单不影响其余单据", len(d6.get("items") or []) == 1, len(d6.get("items") or []))

    st, d7 = req("GET", "/api/print/reimbursements", params={"ids": str(rid)}, token=utoken)
    check("别人的单被过滤（不静默返回）",
          st == 200 and d7.get("items") == [] and (d7.get("stats") or {}).get("skipped_count") == 1,
          d7.get("stats"))

    print(f"\n结果：{OK} 通过 / {FAIL} 失败")
    if FAILED:
        print("失败项：" + "、".join(FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
