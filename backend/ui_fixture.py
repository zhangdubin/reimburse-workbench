"""UI 验收用最小业务数据集。

用途：在**干净的生产库**上快速铺出「每张列表都有数据、每种状态都出现」
的样本，并创建 4 个角色账号，供 tools/verify_ui.mjs 做角色可见性验收。

刻意不做的事情：不灌演示数据（那是 app.seed --demo 的职责），
本脚本只建验收必需的最小集合，且全部走真实接口（顺带再验一遍接口契约）。

用法：
    WB_BASE_URL=http://127.0.0.1:8793 WB_PASSWORD=Adm1n@2026 python ui_fixture.py
"""

import os
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request

BASE = (
    (sys.argv[1] if len(sys.argv) > 1 else None)
    or os.environ.get("WB_BASE_URL")
    or "http://127.0.0.1:8793"
).rstrip("/")

ADMIN_USER = os.environ.get("WB_USER", "admin")
ADMIN_PASSWORD = os.environ.get("WB_PASSWORD", "Adm1n@2026")

# 4 个角色账号统一密码，便于 UI 脚本直接用
ROLE_PWD = os.environ.get("WB_FIXTURE_PWD", "Passw0rd@1")

SUF = uuid.uuid4().hex[:6]
TOKEN = None


def req(method, path, body=None, params=None, token=None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = None
    headers = {}
    if body is not None:
        data = __import__("json").dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        r.add_header(k, v)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=40) as resp:
            raw = resp.read()
            try:
                return resp.status, __import__("json").loads(raw.decode() or "null")
            except Exception:  # noqa: BLE001
                return resp.status, None
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, __import__("json").loads(payload.decode() or "null")
        except Exception:  # noqa: BLE001
            return e.code, payload.decode()[:200]
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def must(label, method, path, body=None, params=None, token=None, want=(200, 201)):
    st, data = req(method, path, body, params, token)
    if st not in want:
        print(f"  \033[31m✗ {label}: HTTP {st} {data}\033[0m")
        sys.exit(2)
    print(f"  \033[32m✓\033[0m {label}")
    return data


def login(username, password):
    st, data = req("POST", "/api/auth/login", {"username": username, "password": password})
    if st != 200:
        print(f"\033[31m登录失败 {username}: HTTP {st} {data}\033[0m")
        sys.exit(2)
    return data["token"]


def make_user(username, name, role, *, employee_id=None, approval_level=0):
    """建账号并把「强制改密」清掉（改成同一个密码），让 UI 能直接登录。"""
    st, data = req("POST", "/api/users", {
        "username": username, "name": name, "role": role,
        "employee_id": employee_id, "approval_level": approval_level,
        "password": ROLE_PWD,
    }, token=TOKEN)
    if st != 201 and "已存在" not in str(data):
        print(f"  \033[31m✗ 建账号 {username}: HTTP {st} {data}\033[0m")
        sys.exit(2)
    t = login(username, ROLE_PWD)
    st, d = req("POST", "/api/auth/password",
                {"old_password": ROLE_PWD, "new_password": ROLE_PWD}, token=t)
    if st != 200 and "不能" not in str(d):
        print(f"  \033[31m✗ 清改密标记 {username}: HTTP {st} {d}\033[0m")
        sys.exit(2)
    print(f"  \033[32m✓\033[0m 账号 {username} / {role}（密码 {ROLE_PWD}）")
    return data["id"]


print(f"目标: {BASE}")
TOKEN = login(ADMIN_USER, ADMIN_PASSWORD)
print("  管理员登录成功")
# 初始管理员的 must_change_password 默认是 True，会让 UI 验收时被
# 「请修改初始密码」弹窗挡住。这里用同一个密码改一次，把标记清掉。
_st, _d = req("POST", "/api/auth/password",
              {"old_password": ADMIN_PASSWORD, "new_password": ADMIN_PASSWORD}, token=TOKEN)
if _st == 200:
    TOKEN = login(ADMIN_USER, ADMIN_PASSWORD)
    print("  已清除初始管理员的强制改密标记")

print("\n== 主数据 ==")
dept = must("部门「销售一部」", "POST", "/api/departments",
            {"name": "销售一部", "code": f"SD{SUF}", "manager": "陈立"}, token=TOKEN,
            want=(201,)).get("id")
if dept is None:
    dept = next(d["id"] for d in req("GET", "/api/departments", token=TOKEN)[1] if d["name"] == "销售一部")
emp1 = must("员工 张伟", "POST", "/api/employees",
            {"name": "张伟", "employee_no": f"E{SUF}01", "department_id": dept,
             "position": "大客户经理", "phone": "13800000001"}, token=TOKEN, want=(201,)).get("id")
if emp1 is None:
    emp1 = next(e["id"] for e in req("GET", "/api/employees", token=TOKEN)[1] if e["employee_no"] == f"E{SUF}01")
emp2 = must("员工 李娜", "POST", "/api/employees",
            {"name": "李娜", "employee_no": f"E{SUF}02", "department_id": dept,
             "position": "销售代表"}, token=TOKEN, want=(201,)).get("id")
if emp2 is None:
    emp2 = next(e["id"] for e in req("GET", "/api/employees", token=TOKEN)[1] if e["employee_no"] == f"E{SUF}02")
cats = req("GET", "/api/categories", token=TOKEN)[1]
CAT = {c["name"]: c["id"] for c in cats}
need_cats = [("市内交通费", "交通费"), ("业务招待费", "招待费"), ("差旅住宿费", "差旅费"), ("办公用品", "办公费")]
for nm, grp in need_cats:
    if nm not in CAT:
        r = must(f"费用类型「{nm}」", "POST", "/api/categories",
                 {"name": nm, "code": f"C{SUF}{len(CAT)}", "group_name": grp,
                  "requires_invoice": nm != "市内交通费", "single_limit": 5000},
                 token=TOKEN, want=(201,))
        CAT[nm] = r["id"]
cust = must("客户 前海数字云", "POST", "/api/customers",
            {"name": "前海数字云科技有限公司", "code": f"CU{SUF}", "industry": "软件与信息服务",
             "contact": "王总"}, token=TOKEN, want=(201,))["id"]
proj = must("项目 数字云平台", "POST", "/api/projects",
            {"name": "前海数字云平台建设", "code": f"PJ{SUF}", "customer_id": cust,
             "manager": "陈立", "status": "进行中"}, token=TOKEN, want=(201,))["id"]
YEAR = int(os.environ.get("WB_FIXTURE_YEAR", "2026"))
must("年度预算", "POST", "/api/budgets",
     {"year": YEAR, "month": None, "department_id": dept, "amount": 800000},
     token=TOKEN, want=(201,))

print("\n== 角色账号 ==")
make_user("applicant1", "张伟", "申请人", employee_id=emp1)
make_user("applicant2", "李娜", "申请人", employee_id=emp2)
make_user("approver1", "陈立", "审批人", employee_id=emp1, approval_level=2)
make_user("finance1", "林会计", "财务")
make_user("admin2", "备用管理员", "管理员")

AP1 = login("applicant1", ROLE_PWD)
AP2 = login("applicant2", ROLE_PWD)
APR = login("approver1", ROLE_PWD)
FIN = login("finance1", ROLE_PWD)


def new_order(token, title, amount, *, applicant_id=None, cat="差旅住宿费", customer=None, project=None):
    st, data = req("POST", "/api/reimbursements", {
        "title": title, "applicant_id": applicant_id,
        "department_id": dept, "customer_id": customer, "project_id": project,
        "purpose": "客户现场支持",
        "items": [{"category_id": CAT[cat], "occur_date": f"{YEAR}-09-10",
                   "amount": amount, "description": title}],
    }, token=token)
    if st != 201:
        print(f"\033[31m建单失败 {title}: HTTP {st} {data}\033[0m")
        sys.exit(2)
    return data


print("\n== 报销单（各状态都有） ==")
# 1) 已付款：小额，一级审批即完成
o1 = new_order(AP1, "前海数字云项目现场往返打车", 386.5, cat="市内交通费", customer=cust, project=proj)
must("提交 o1", "POST", f"/api/reimbursements/{o1['id']}/submit", {}, token=AP1)
must("审批 o1", "POST", f"/api/reimbursements/{o1['id']}/approve",
     {"operator": "陈立", "approver": "陈立"}, token=APR)
must("付款 o1", "POST", f"/api/reimbursements/{o1['id']}/pay", {"operator": "林会计"}, token=FIN)

# 2) 待审批（一级）：中等金额
o2 = new_order(AP1, "客户答谢晚宴招待费", 4200, cat="业务招待费", customer=cust)
must("提交 o2", "POST", f"/api/reimbursements/{o2['id']}/submit", {}, token=AP1)

# 3) 待二级审批：大额，一级已通过
o3 = new_order(AP2, "数字云平台三季度差旅住宿", 26800, applicant_id=emp2, customer=cust, project=proj)
must("提交 o3", "POST", f"/api/reimbursements/{o3['id']}/submit", {}, token=AP2)
must("一级审批 o3", "POST", f"/api/reimbursements/{o3['id']}/approve",
     {"operator": "陈立"}, token=APR)

# 4) 已驳回
o4 = new_order(AP2, "办公用品采购（发票缺失）", 1580, applicant_id=emp2, cat="办公用品")
must("提交 o4", "POST", f"/api/reimbursements/{o4['id']}/submit", {}, token=AP2)
must("驳回 o4", "POST", f"/api/reimbursements/{o4['id']}/reject",
     {"comment": "缺少增值税专用发票，请补充后重新提交"}, token=APR)

# 5) 草稿
new_order(AP1, "10 月客户拜访交通费（待补充明细）", 268, cat="市内交通费", customer=cust)

print("\n== 发票 ==")
inv_rows = [
    ("24417000000000101", 386.5, "深圳市顺捷汽车服务有限公司", "市内交通费"),
    ("24417000000000102", 4200.0, "深圳湾万丽酒店管理有限公司", "业务招待费"),
    ("24417000000000103", 26800.0, "深圳市南山智选假日酒店", "差旅住宿费"),
    ("24417000000000104", 1580.0, "深圳市晨光文具连锁有限公司", "办公用品"),
]
for no, amt, seller, cname in inv_rows:
    st, d = req("POST", "/api/invoices", {
        "invoice_no": no, "invoice_type": "增值税电子普通发票", "amount": amt,
        "tax_rate": 6, "tax_amount": round(amt / 1.06 * 0.06, 2),
        "invoice_date": f"{YEAR}-09-10", "seller_name": seller,
        "buyer_name": "深圳市智联云创科技有限公司", "category_id": CAT[cname],
    }, token=FIN)
    if st != 201:
        print(f"  \033[31m✗ 发票 {no}: HTTP {st} {d}\033[0m")
        sys.exit(2)
    print(f"  \033[32m✓\033[0m 发票 {no}")

# 故意登记一条重复号，让「重复报销检测」有内容
st, d = req("POST", "/api/invoices", {
    "invoice_no": "24417000000000101", "amount": 386.5, "invoice_date": f"{YEAR}-09-11",
    "seller_name": "深圳市顺捷汽车服务有限公司", "category_id": CAT["市内交通费"],
}, token=FIN)
print(f"  \033[32m✓\033[0m 重复发票（检测用）状态={d.get('check_status')}")

# 关联一张到已付款单，让报销单详情能显示发票
inv_id = req("GET", "/api/invoices", params={"page_size": 50}, token=FIN)[1]["items"]
target = next(x for x in inv_id if x["invoice_no"] == "24417000000000101" and x["reimbursement_id"] is None)
must("关联发票到已付款单", "POST", f"/api/invoices/{target['id']}/link",
     params={"reimbursement_id": o1["id"]}, token=FIN)

def multipart(fields, files):
    """手工拼 multipart：标准库没有现成的，但格式很简单。"""
    boundary = "----wb" + uuid.uuid4().hex
    buf = b""
    for k, v in fields.items():
        buf += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, filename, content, ctype in files:
        buf += (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{k}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
        buf += content + b"\r\n"
    buf += f"--{boundary}--\r\n".encode()
    return buf, f"multipart/form-data; boundary={boundary}"


def ingest(subject, sender, filename, payload, ctype, msg_id):
    """走外部投递口投一封「邮件」，与 Agent Mail / 邮件网关同一条通路。"""
    body, ct = multipart({"subject": subject, "sender": sender, "date": f"{YEAR}-09-15 10:30:00"},
                         [("files", filename, payload, ctype)])
    r = urllib.request.Request(BASE + "/api/inbox/ingest", data=body, method="POST")
    r.add_header("Content-Type", ct)
    token = os.environ.get("WB_INGEST_TOKEN", "")
    if token:
        r.add_header("X-Ingest-Token", token)
    r.add_header("X-Message-Id", msg_id)
    try:
        with urllib.request.urlopen(r, timeout=40) as resp:
            return resp.status, __import__("json").loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


print("\n== 收票邮箱与收件记录 ==")
st, d = req("POST", "/api/mail-accounts", {
    "name": f"财务部收票箱-{SUF}",
    "host": "imap.exmail.qq.com", "port": 993, "use_ssl": True,
    "username": f"finance-{SUF}@example.com", "password": "MailAuth@2026",
    "folder": "INBOX", "only_unseen": True, "mark_seen": True,
    "since_days": 30, "max_per_sync": 30,
    "subject_keywords": "发票,电子发票", "active": True,
    "remark": "验收用占位账号，真实凭据请在生产环境替换",
}, token=TOKEN)
if st == 201:
    print(f"  \033[32m✓\033[0m 收票邮箱 #{d['id']}（未连真实 IMAP，仅用于界面验收）")
else:
    print(f"  \033[31m✗ 收票邮箱: HTTP {st} {d}\033[0m")
    sys.exit(2)

OFD_TMPL = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice>
  <InvoiceNumber>{no}</InvoiceNumber>
  <InvoiceCode>044001900111</InvoiceCode>
  <IssueTime>{YEAR}-09-15</IssueTime>
  <SellerName>{seller}</SellerName>
  <BuyerName>深圳市智联云创科技有限公司</BuyerName>
  <TotalAmtWithTax>{amt}</TotalAmtWithTax>
  <TotalTax>{tax}</TotalTax>
  <TaxRate>6</TaxRate>
</Invoice>"""

# 纯数字后缀：识别引擎只认 digits，hex 里的 a-f 会让号码提取失败
DIGITS = str(int(SUF, 16) % 1000000).zfill(6)


def make_ofd(no, seller, amt):
    import io
    import zipfile

    xml = OFD_TMPL.format(no=no, seller=seller, amt=f"{amt:.2f}",
                          tax=f"{amt / 1.06 * 0.06:.2f}", YEAR=YEAR)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("OFD.xml", "<OFD/>")
        z.writestr("Doc_0/OriginalInvoice.xml", xml)
    return buf.getvalue()


INBOX_SEED = [
    ("【电子发票】深圳市顺捷汽车服务有限公司 开票通知", "noreply@fapiao-sj.com",
     688.0, "深圳市顺捷汽车服务有限公司", "直达邮件 1"),
    ("客户拜访交通费发票（9 月）", "finance@sjyc.com",
     1240.0, "深圳市云途出行科技有限公司", "直达邮件 2"),
]

# 投递口要令牌。忘了给 WB_INGEST_TOKEN 不会报错、只会安静地少两张票，
# 收件箱那几条断言就会莫名其妙地不过（或过得很可疑）。这里先说清楚。
if not os.environ.get("WB_INGEST_TOKEN"):
    print("\n\033[33m⚠ 未设置 WB_INGEST_TOKEN：直达邮件会被投递口拒收，"
          "收件箱将没有收件记录。\033[0m")
    print("  前端验收要覆盖收件箱时，请带上令牌重跑：")
    print("  WB_INGEST_TOKEN=<与后端 INBOX_INGEST_TOKEN 一致> python ui_fixture.py\n")

for i, (subj, sender, amt, seller, label) in enumerate(INBOX_SEED):
    no = f"24417000000{DIGITS}{i:02d}"[:20]
    st, d = ingest(subj, sender, f"发票_{no}.ofd", make_ofd(no, seller, amt),
                   "application/octet-stream", f"fixture-{SUF}-{i}")
    if st == 200 and isinstance(d, dict) and d.get("ok"):
        print(f"  \033[32m✓\033[0m {label}：入库 {d.get('imported')} 张（{no}）")
    else:
        print(f"  \033[33m! {label} 投递返回 HTTP {st}：{d}（不影响界面验收）\033[0m")

print("\n\033[1m完成。可用账号：\033[0m")
print(f"  管理员    admin        / {ADMIN_PASSWORD}")
print(f"  申请人    applicant1   / {ROLE_PWD}")
print(f"  申请人    applicant2   / {ROLE_PWD}")
print(f"  审批人    approver1    / {ROLE_PWD}（2 级，部门内可审）")
print(f"  财务      finance1     / {ROLE_PWD}")
