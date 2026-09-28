"""发票凭证与批量登记接口测试（v2.9.22）

覆盖两件事：
  1. 批量登记发票（POST /api/invoices/batch）——一次提交多张、部分失败不连坐；
  2. 随票凭证（行程单 / 消费水单）——按类型上传、按费用类型的应附单据判断齐备、
     台账筛选与汇总口径。

走 HTTP 打真实接口（含 multipart 上传），与 smoke_test 同一套约定：
用当前登录的管理员凭据，数据自带唯一后缀，可在同一库上反复运行。

用法：
    WB_BASE_URL=http://127.0.0.1:8791 WB_PASSWORD=xxx python docs_test.py
    python docs_test.py http://127.0.0.1:8792
"""

import io
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
    or "http://127.0.0.1:8791"
).rstrip("/")

WB_USER = os.environ.get("WB_USER", "admin")
WB_PASSWORD = os.environ.get("WB_PASSWORD", "Adm1n@2026")

OK = 0
FAIL = 0
TOKEN: str | None = None
SUF = uuid.uuid4().hex[:6]

# 一份最小 PDF 头 + 一份最小 JPEG 头，够过后缀/mime 白名单即可
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 512 + b"\xff\xd9"


def call(method, path, body=None, params=None, auth=True, expect=None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if auth and TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def multipart(parts):
    """构造 multipart 请求体。元素为 (field, value) 或 (field, filename, bytes, mime)。"""
    boundary = "----wbdocs" + uuid.uuid4().hex
    buf = io.BytesIO()
    for p in parts:
        buf.write(f"--{boundary}\r\n".encode())
        if len(p) == 4:
            field, filename, content, ctype = p
            buf.write(
                f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode()
            )
            buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
            buf.write(content if isinstance(content, bytes) else str(content).encode())
        else:
            field, value = p
            buf.write(f'Content-Disposition: form-data; name="{field}"\r\n\r\n'.encode())
            buf.write(str(value).encode())
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def post_file(path, parts, expect=None):
    """发一次 multipart 请求（附件上传走这条）。"""
    raw, ctype = multipart(parts)
    req = urllib.request.Request(BASE + path, data=raw, method="POST")
    req.add_header("Content-Type", ctype)
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  \033[32mPASS\033[0m {name}" + (f" | {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m {name}" + (f" -> {extra}" if extra else ""))
    return cond


def login():
    global TOKEN
    st, data = call(
        "POST", "/api/auth/login",
        {"username": WB_USER, "password": WB_PASSWORD}, auth=False,
    )
    if st != 200:
        print(f"\033[31m登录失败：HTTP {st} {data}\033[0m")
        sys.exit(2)
    TOKEN = data["token"]


def main():
    login()
    print(f"== 发票凭证与批量登记（{BASE}，后缀 {SUF}）==")

    # ---------------- 准备：两类费用类型（一个要行程单、一个要水单） ----------------
    print("\n[1] 费用类型的应附单据")
    cat_ride = call("POST", "/api/categories", {
        "name": f"【自检】市内交通-{SUF}", "code": f"D1{SUF}",
        "group_name": "交通费", "requires_invoice": True, "required_doc": "itinerary",
    }, expect=201)[1]
    check("新建类别带行程单要求", cat_ride.get("required_doc") == "itinerary",
          f"{cat_ride.get('required_doc')} / {cat_ride.get('required_doc_label')}")
    cat_hotel = call("POST", "/api/categories", {
        "name": f"【自检】住宿-{SUF}", "code": f"D2{SUF}",
        "group_name": "差旅费", "required_doc": "folio",
    }, expect=201)[1]
    check("新建类别带水单要求", cat_hotel.get("required_doc") == "folio",
          str(cat_hotel.get("required_doc_label")))
    no_req = call("POST", "/api/categories", {
        "name": f"【自检】其他-{SUF}", "code": f"D3{SUF}",
    }, expect=201)[1]
    check("不填应附单据 = 不作要求", not no_req.get("required_doc"), str(no_req.get("required_doc")))
    st, cats = call("GET", "/api/categories", params={"q": "【自检】", "page_size": 50})
    check("类别列表带 required_doc 字段",
          st == 200 and any(c["id"] == cat_ride["id"] and c["required_doc"] == "itinerary" for c in cats))

    # ---------------- 批量登记 ----------------
    print("\n[2] 批量登记发票")
    base = {
        "invoice_type": "增值税电子普通发票", "tax_rate": 6, "buyer_name": "【自检】本公司",
    }
    items = [
        dict(base, invoice_no=f"DOC{SUF}A", amount=68.0, tax_amount=3.85,
             invoice_date="2026-09-20", seller_name=f"【自检】滴滴出行-{SUF}",
             category_id=cat_ride["id"]),
        dict(base, invoice_no=f"DOC{SUF}B", amount=128.0, tax_amount=7.25,
             invoice_date="2026-09-21", seller_name=f"【自检】滴滴出行-{SUF}",
             category_id=cat_ride["id"]),
        dict(base, invoice_no=f"DOC{SUF}C", amount=688.0, tax_amount=38.94,
             invoice_date="2026-09-22", seller_name=f"【自检】某酒店-{SUF}",
             category_id=cat_hotel["id"]),
    ]
    st, r = call("POST", "/api/invoices/batch", {"items": items}, expect=201)
    check("批量登记 3 张", st == 201 and r["created_count"] == 3, f"HTTP {st} {r}")
    ids = r["ids"]
    check("返回 id 与提交顺序一一对应",
          len(ids) == 3 and r["created"][0]["index"] == 0 and r["created"][2]["index"] == 2)
    check("新票默认缺凭证（有应附单据要求）",
          all(x["doc_status"] == "missing" for x in
              (call("GET", f"/api/invoices/{i}")[1] for i in ids[:2])))

    # 部分失败：缺号 + 金额 0 + 正常一张
    st, r2 = call("POST", "/api/invoices/batch", {"items": [
        dict(base, invoice_no=f"DOC{SUF}D", amount=10.0),
        dict(base, invoice_no="", amount=20.0),
        dict(base, invoice_no=f"DOC{SUF}E", amount=0),
    ]}, expect=201)
    check("部分失败不连坐（成功 1 / 失败 2）",
          st == 201 and r2["created_count"] == 1 and r2["failed_count"] == 2,
          f"created={r2['created_count']} failed={[f['reason'] for f in r2['failed']]}")
    check("失败项带原始下标与原因",
          {f["index"] for f in r2["failed"]} == {1, 2})
    ids.append(r2["ids"][0])

    # 重复号码并入 duplicate_count
    st, r3 = call("POST", "/api/invoices/batch", {"items": [
        dict(base, invoice_no=f"DOC{SUF}A", amount=68.0),
    ]}, expect=201)
    check("批量登记里的重复号码被标记为异常",
          st == 201 and r3["created_count"] == 1 and r3["duplicate_count"] == 1,
          f"duplicate={r3.get('duplicate_count')}")
    ids.append(r3["ids"][0])

    st, _ = call("POST", "/api/invoices/batch", {"items": []}, expect=400)
    check("空列表被拒绝", st == 400, f"HTTP {st}")
    st, _ = call("POST", "/api/invoices/batch",
                 {"items": [dict(base, invoice_no=f"X{SUF}{i}", amount=1.0) for i in range(201)]},
                 expect=400)
    check("超过 200 张被拒绝", st == 400, f"HTTP {st}")

    # ---------------- 凭证上传 ----------------
    print("\n[3] 随票凭证上传")
    ride_id = ids[0]
    st, up = post_file(f"/api/invoices/{ride_id}/attachments",
                       [("file", f"行程单-{SUF}.pdf", PDF, "application/pdf"),
                        ("kind", "itinerary")], expect=201)
    check("单文件上传行程单", st == 201 and up["kind"] == "itinerary" and up["kind_label"] == "行程单",
          f"HTTP {st} {up if st != 201 else up['filename']}")
    att_id = up.get("id") if st == 201 else None

    st, _ = post_file(f"/api/invoices/{ride_id}/attachments",
                      [("file", f"x-{SUF}.pdf", PDF, "application/pdf"), ("kind", "badkind")],
                      expect=400)
    check("非法凭证类型被拒绝", st == 400, f"HTTP {st}")
    st, _ = post_file(f"/api/invoices/{ride_id}/attachments",
                      [("file", f"恶意-{SUF}.exe", b"MZ..", "application/octet-stream"),
                       ("kind", "itinerary")], expect=400)
    check("非法后缀被拒绝（路径穿越/可执行文件防线）", st == 400, f"HTTP {st}")
    st, _ = post_file(f"/api/invoices/{ride_id}/attachments",
                      [("file", f"行程单2-{SUF}.jpg", JPG, "image/jpeg")], expect=201)
    check("不传 kind 时默认发票影像", st == 201 and _.get("kind") == "invoice", f"HTTP {st}")

    # 批量上传：两个行程单 + 一个非法
    st, batch = post_file(f"/api/invoices/{ride_id}/attachments/batch", [
        ("files", f"行程单-A-{SUF}.pdf", PDF, "application/pdf"),
        ("files", f"行程单-B-{SUF}.pdf", PDF, "application/pdf"),
        ("files", f"坏文件-{SUF}.txt", b"x", "text/plain"),
        ("kind", "itinerary"),
    ], expect=201)
    check("批量上传：合法 2 份入库、非法 1 份跳过",
          st == 201 and batch["saved_count"] == 2 and batch["failed_count"] == 1,
          f"saved={batch.get('saved_count')} failed={batch.get('failed')}")
    check("批量上传返回类型标签", batch.get("kind_label") == "行程单")

    st, lst = call("GET", f"/api/invoices/{ride_id}/attachments")
    kinds = {a["kind"] for a in lst} if st == 200 else set()
    check("附件列表返回凭证类型", st == 200 and {"invoice", "itinerary"} <= kinds,
          f"{len(lst) if st == 200 else '?'} 份 / {kinds}")

    st, inv = call("GET", f"/api/invoices/{ride_id}")
    check("上传行程单后凭证齐备", st == 200 and inv["doc_status"] == "ok",
          f"{inv.get('doc_status')} kinds={inv.get('doc_kinds')}")

    hotel_id = ids[2] if len(ids) > 2 else ids[-1]
    st, _ = post_file(f"/api/invoices/{hotel_id}/attachments",
                      [("file", f"水单-{SUF}.pdf", PDF, "application/pdf"), ("kind", "folio")],
                      expect=201)
    check("住宿票上传水单后齐备",
          call("GET", f"/api/invoices/{hotel_id}")[1]["doc_status"] == "ok")

    # 下传 raw
    if att_id:
        req = urllib.request.Request(f"{BASE}/api/attachments/{att_id}/raw")
        req.add_header("Authorization", f"Bearer {TOKEN}")
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read()
            check("凭证可下传且带 nosniff",
                  resp.status == 200 and body.startswith(b"%PDF")
                  and resp.headers.get("X-Content-Type-Options") == "nosniff",
                  f"{len(body)} bytes")

    # ---------------- 筛选与汇总 ----------------
    print("\n[4] 缺凭证筛选与汇总")
    st, lst = call("GET", "/api/invoices", params={"q": f"DOC{SUF}", "doc_status": "missing", "page_size": 50})
    missing_nos = {x["invoice_no"] for x in lst["items"]} if st == 200 else set()
    check("筛「缺应附单据」只命中未附票",
          st == 200 and f"DOC{SUF}A" not in missing_nos and f"DOC{SUF}C" not in missing_nos,
          f"命中 {sorted(missing_nos)}")

    st, lst = call("GET", "/api/invoices", params={"q": f"DOC{SUF}", "doc_status": "complete", "page_size": 50})
    ok_nos = {x["invoice_no"] for x in lst["items"]} if st == 200 else set()
    check("筛「已齐备」命中已附票", {f"DOC{SUF}A", f"DOC{SUF}C"} <= ok_nos, f"命中 {sorted(ok_nos)}")

    st, lst = call("GET", "/api/invoices", params={"q": f"DOC{SUF}", "doc_status": "missing_itinerary", "page_size": 50})
    check("按缺件类型筛（缺行程单）不误伤水单类",
          st == 200 and all(x["invoice_no"] != f"DOC{SUF}C" for x in lst["items"]),
          f"命中 {[x['invoice_no'] for x in lst['items']]}")

    st, lst = call("GET", "/api/invoices", params={"q": f"DOC{SUF}", "doc_status": "has_docs", "page_size": 50})
    check("筛「已附补充材料」", st == 200 and {f"DOC{SUF}A", f"DOC{SUF}C"} <=
          {x["invoice_no"] for x in lst["items"]}, f"命中 {lst['total'] if st == 200 else '?'} 张")

    st, lst = call("GET", "/api/invoices", params={"q": f"DOC{SUF}", "page_size": 50})
    cats_in_list = {(x["invoice_no"], x["doc_status"], x["required_doc_label"]) for x in lst["items"]}
    check("列表同时带齐备状态与应附单据名称",
          all(x[1] in ("ok", "missing", "none") for x in cats_in_list)
          and any(x[2] == "行程单" for x in cats_in_list),
          str(sorted(cats_in_list))[:140])

    st, s1 = call("GET", "/api/invoices/summary")
    check("汇总带缺凭证统计", st == 200 and isinstance(s1.get("missing_doc_count"), int),
          f"缺凭证 {s1.get('missing_doc_count')} 张")

    # 删除凭证 -> 回到缺件
    if att_id:
        st, _ = call("DELETE", f"/api/attachments/{att_id}")
        st2, inv2 = call("GET", f"/api/invoices/{ride_id}")
        # 该票还挂着两张批量上传的行程单，所以仍是齐备；删掉全部行程单才转缺件
        st, lst2 = call("GET", f"/api/invoices/{ride_id}/attachments")
        for a in [a for a in lst2 if a["kind"] == "itinerary"]:
            call("DELETE", f"/api/attachments/{a['id']}")
        st3, inv3 = call("GET", f"/api/invoices/{ride_id}")
        check("删掉全部行程单后回到缺件", inv3["doc_status"] == "missing",
              f"{inv3.get('doc_status')} / 删除路径 {st}->{st2}->{st3}")

    # ---------------- 清理 ----------------
    print("\n[5] 清理与文件回收")
    st, r = call("POST", "/api/invoices/batch-delete", {"ids": ids})
    check("批量删除自检发票", st == 200 and r.get("deleted", 0) >= len(ids),
          f"删除 {r.get('deleted')} 张 / 回收文件 {r.get('files_removed')} 个")
    check("删除时一并回收磁盘影像文件", r.get("files_removed", 0) >= 2,
          f"files_removed={r.get('files_removed')}（两张行程单已在上面单独删除时回收）")
    for c in (cat_ride, cat_hotel, no_req):
        call("DELETE", f"/api/categories/{c['id']}")

    print(f"\n结果：{OK} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
