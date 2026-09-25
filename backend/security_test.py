"""权限 / 分级审批 / 影像 / 审计 的验证脚本。

这是生产化改造的验收工具，和 smoke_test.py（功能冒烟）分开：
smoke_test 关心「功能对不对」，本脚本关心「权限守没守住」。

设计成**自己造数据**：在干净库上跑，创建两个部门、两个审批人、两个申请人，
再逐条验证越权路径。所有断言都以「必须被拒绝」为主——权限的漏网之鱼才是真风险。

用法：
    WB_BASE_URL=http://127.0.0.1:8792 WB_PASSWORD=xxx python security_test.py
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
    or "http://127.0.0.1:8792"
).rstrip("/")

ADMIN_USER = os.environ.get("WB_USER", "admin")
ADMIN_PASSWORD = os.environ.get("WB_PASSWORD", "Adm1n@2026")

OK = 0
FAIL = 0
TOKEN = None

# 造出来的账号
PWD = "Test@2026x"
ACCOUNTS: dict[str, dict] = {}


def req(method, path, body=None, params=None, token=None, raw=None, ctype=None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = None
    headers = {}
    if raw is not None:
        data = raw
        headers["Content-Type"] = ctype
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        r.add_header(k, v)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=40) as resp:
            body_bytes = resp.read()
            try:
                return resp.status, json.loads(body_bytes.decode() or "null"), body_bytes
            except (json.JSONDecodeError, UnicodeDecodeError):
                # 二进制响应（如影像下载）不走 JSON：返回原始 bytes 供 probe 比对
                return resp.status, None, body_bytes
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload.decode() or "null"), payload
        except (json.JSONDecodeError, UnicodeDecodeError):
            try:
                return e.code, payload.decode()[:200], payload
            except UnicodeDecodeError:
                return e.code, f"{len(payload)} bytes", payload
    except Exception as e:  # noqa: BLE001
        return -1, str(e), b""


def expect(label, method, path, want=None, body=None, *, token=None, params=None, raw=None,
           ctype=None, probe=None):
    """断言状态码。

    参数位置刻意做得宽容一些，下面两种写法等价：
        expect("标签", "POST", "/x", 201, body={...})
        expect("标签", "POST", "/x", {...}, 201)
    如果只给了请求体没给状态码，默认期望 200。
    """
    global OK, FAIL
    if isinstance(want, dict):
        want, body = body, want
    if want is None:
        want = 200
    st, data, rawbytes = req(method, path, body, params, token, raw, ctype)
    accepted = want if isinstance(want, (tuple, set, list)) else (want,)
    ok = st in accepted
    extra = ""
    if ok and probe:
        try:
            extra = " | " + probe(data, rawbytes)
        except Exception as e:  # noqa: BLE001
            ok = False
            extra = f" | probe 失败: {e}"
    if ok:
        OK += 1
        print(f"  \033[32mPASS\033[0m {label}{extra}")
    else:
        FAIL += 1
        detail = str(data)[:220] if data is not None else f"{len(rawbytes)} bytes"
        print(f"  \033[31mFAIL\033[0m {label} -> HTTP {st}（期望 {accepted}）{detail}")
    return data


def must_deny(label, method, path, body=None, *, token=None):
    """越权用例：403 或 401 都算守住了，200/201 就是漏洞。"""
    return expect(label, method, path, (401, 403), token=token, body=body)


def login(username, password):
    st, data, _ = req("POST", "/api/auth/login", {"username": username, "password": password})
    if st != 200:
        print(f"\033[31m登录失败 {username}: HTTP {st} {data}\033[0m")
        sys.exit(2)
    return data["token"]


def make_user(username, name, role, *, employee_id=None, approval_level=0, password=PWD):
    st, data, _ = req("POST", "/api/users", {
        "username": username, "name": name, "role": role,
        "employee_id": employee_id, "approval_level": approval_level,
        "password": password,
    }, token=TOKEN)
    if st != 201:
        print(f"\033[31m创建账号失败 {username}: HTTP {st} {data}\033[0m")
        sys.exit(2)
    # 管理员代设的密码带「强制修改」标记，这里先改掉，避免影响后续用例
    t = login(username, password)
    st, d, _ = req("POST", "/api/auth/password",
                   {"old_password": password, "new_password": password + "Z9"}, token=t)
    if st != 200:
        print(f"\033[31m首次改密失败 {username}: HTTP {st} {d}\033[0m")
        sys.exit(2)
    t = login(username, password + "Z9")
    ACCOUNTS[username] = {"token": t, "role": role, "id": data["id"], "name": name}
    return data["id"]


print(f"目标: {BASE}")
print("\n\033[1m== 前置：管理员登录与初始状态 ==\033[0m")
TOKEN = login(ADMIN_USER, ADMIN_PASSWORD)
expect("管理员登录", "GET", "/api/auth/me", 200, token=TOKEN,
       probe=lambda d, _: f"{d['user']['name']} / {d['user']['role']}")
expect("管理员被要求改初始密码", "GET", "/api/auth/me", 200, token=TOKEN,
       probe=lambda d, _: f"must_change_password={d['user']['must_change_password']}")

print("\n\033[1m== 认证边界 ==\033[0m")
must_deny("无 token 访问报销单", "GET", "/api/reimbursements", token=None)
must_deny("无 token 访问统计", "GET", "/api/stats/overview", token=None)
must_deny("无 token 访问用户管理", "GET", "/api/users", token=None)
must_deny("伪造 token 被拒", "GET", "/api/reimbursements", token="not-a-real-token")
expect("错误密码登录失败", "POST", "/api/auth/login",
       {"username": ADMIN_USER, "password": "wrong"}, 401)
st_unknown, d_unknown, _ = req("POST", "/api/auth/login", {"username": "no-such-user", "password": "x"})
expect("不存在的用户登录失败", "POST", "/api/auth/login",
       {"username": "no-such-user", "password": "x"}, 401)
expect("账号不存在与密码错误的提示一致（防枚举）", "POST", "/api/auth/login",
       {"username": ADMIN_USER, "password": "wrong"}, 401,
       probe=lambda d, _: f"提示文案相同={'用户名或密码错误' in str(d)}")

print("\n\033[1m== 前置：构造两个部门 / 员工 / 四种角色账号 ==\033[0m")
suffix = uuid.uuid4().hex[:6]
dept_a = expect("建部门A", "POST", "/api/departments",
                {"name": f"测试一部-{suffix}", "code": f"TA{suffix}"}, 201, token=TOKEN)["id"]
dept_b = expect("建部门B", "POST", "/api/departments",
                {"name": f"测试二部-{suffix}", "code": f"TB{suffix}"}, 201, token=TOKEN)["id"]
emp_a1 = expect("建员工A1", "POST", "/api/employees",
                {"name": f"测试甲{suffix}", "employee_no": f"TA1{suffix}", "department_id": dept_a},
                201, token=TOKEN)["id"]
emp_a2 = expect("建员工A2", "POST", "/api/employees",
                {"name": f"测试乙{suffix}", "employee_no": f"TA2{suffix}", "department_id": dept_a},
                201, token=TOKEN)["id"]
emp_b1 = expect("建员工B1", "POST", "/api/employees",
                {"name": f"测试丙{suffix}", "employee_no": f"TB1{suffix}", "department_id": dept_b},
                201, token=TOKEN)["id"]
cat = expect("建费用类型", "POST", "/api/categories",
             {"name": f"测试费用-{suffix}", "code": f"TC{suffix}"}, 201, token=TOKEN)["id"]

make_user(f"app1_{suffix}", f"申请甲{suffix}", "申请人", employee_id=emp_a1)
make_user(f"app2_{suffix}", f"申请乙{suffix}", "申请人", employee_id=emp_a2)
make_user(f"apr1_{suffix}", f"审批A{suffix}", "审批人", employee_id=emp_a1, approval_level=1)
# A 部门的二级审批人：大额单也在 A 部门，必须由同部门的二级来审
make_user(f"apr2a_{suffix}", f"二级审批A{suffix}", "审批人", employee_id=emp_a2, approval_level=2)
# B 部门审批人：用来验证跨部门审批被拒
make_user(f"apr2_{suffix}", f"审批B{suffix}", "审批人", employee_id=emp_b1, approval_level=2)
make_user(f"fin_{suffix}", f"财务{suffix}", "财务")
make_user(f"nolevel_{suffix}", f"无级审批{suffix}", "审批人", employee_id=emp_a1, approval_level=0)
print(f"  \033[32mPASS\033[0m 已创建 7 个测试账号（2 申请人 / 4 审批人 / 1 财务）")

AP1 = ACCOUNTS[f"app1_{suffix}"]["token"]
AP2 = ACCOUNTS[f"app2_{suffix}"]["token"]
APR1 = ACCOUNTS[f"apr1_{suffix}"]["token"]
APR2A = ACCOUNTS[f"apr2a_{suffix}"]["token"]
APR2 = ACCOUNTS[f"apr2_{suffix}"]["token"]
FIN = ACCOUNTS[f"fin_{suffix}"]["token"]
NOLV = ACCOUNTS[f"nolevel_{suffix}"]["token"]


def new_order(token, title, amount, applicant_id=None):
    st, data, _ = req("POST", "/api/reimbursements", {
        "title": title,
        "applicant_id": applicant_id,
        "items": [{"category_id": cat, "occur_date": "2026-09-15", "amount": amount,
                   "description": title}],
    }, token=token)
    if st != 201:
        print(f"\033[31m建单失败: HTTP {st} {data}\033[0m")
        sys.exit(2)
    return data


print("\n\033[1m== 角色权限矩阵 ==\033[0m")
expect("申请人可建单", "POST", "/api/reimbursements",
       {"title": "权限矩阵-可建单探针", "items": []}, 201, token=AP1)
must_deny("申请人不能改主数据（部门）", "POST", "/api/departments",
          {"name": f"越权部门-{suffix}"}, token=AP1)
must_deny("申请人不能改客户", "POST", "/api/customers", {"name": f"越权客户-{suffix}"}, token=AP1)
must_deny("申请人不能访问用户管理", "GET", "/api/users", token=AP1)
must_deny("申请人不能访问审计日志", "GET", "/api/audit-logs", token=AP1)
must_deny("申请人不能访问异常预警", "GET", "/api/stats/alerts", token=AP1)
must_deny("申请人不能访问预算执行", "GET", "/api/stats/budget-execution", token=AP1)
must_deny("申请人不能查预算列表", "GET", "/api/budgets", token=AP1)
must_deny("申请人不能改费用类型", "POST", "/api/categories", {"name": f"越权类型-{suffix}"}, token=AP1)
must_deny("申请人不能登记发票", "POST", "/api/invoices",
          {"invoice_no": f"INV{suffix}", "amount": 100}, token=AP1)

expect("审批人可看统计看板", "GET", "/api/stats/overview", 200, token=APR1)
expect("审批人可看异常预警", "GET", "/api/stats/alerts", 200, token=APR1)
must_deny("审批人不能付款", "POST", "/api/reimbursements/1/pay", token=APR1)
must_deny("审批人不能访问用户管理", "GET", "/api/users", token=APR1)
must_deny("审批人不能改部门", "POST", "/api/departments", {"name": f"越权部门2-{suffix}"}, token=APR1)
must_deny("审批人不能改员工", "POST", "/api/employees",
          {"name": "越权员工", "employee_no": f"XX{suffix}"}, token=APR1)

expect("财务可看预算", "GET", "/api/budgets", 200, token=FIN)
expect("财务可维护费用类型", "POST", "/api/categories",
       {"name": f"财务建类型-{suffix}", "code": f"TF{suffix}"}, 201, token=FIN)
# 年份按 suffix 派生，保证重复执行同一库时不会撞唯一约束
budget_year = 2030 + int(suffix[:3], 16) % 50
expect("财务可维护预算", "POST", "/api/budgets",
       {"year": budget_year, "month": 12, "department_id": dept_a, "amount": 1000}, 201, token=FIN)
must_deny("财务不能管账号", "POST", "/api/users",
          {"username": f"hack_{suffix}", "name": "hack", "password": PWD}, token=FIN)
must_deny("财务不能改动部门", "POST", "/api/departments", {"name": f"越权部门3-{suffix}"}, token=FIN)
must_deny("财务不能审批", "POST", "/api/reimbursements/1/approve", token=FIN)
expect("管理员可访问审计日志", "GET", "/api/audit-logs", 200, token=TOKEN)

print("\n\033[1m== 数据可见范围隔离 ==\033[0m")
o_a1 = new_order(AP1, f"甲的单-{suffix}", 500)
o_a2 = new_order(AP2, f"乙的单-{suffix}", 700)
o_b1 = new_order(TOKEN, f"B部门的单-{suffix}", 900, applicant_id=emp_b1)

expect("申请人只看到自己的单", "GET", "/api/reimbursements", 200,
       params={"page_size": 200}, token=AP1,
       probe=lambda d, _: f"共 {d['total']} 条，全部属于本人="
       f"{all(x['applicant_id'] == emp_a1 for x in d['items'])}")
expect("申请人看不到同事的单（按 id 直取）", "GET", f"/api/reimbursements/{o_a2['id']}", (403, 404), token=AP1)
expect("审批人只看到本部门的单", "GET", "/api/reimbursements", 200,
       params={"page_size": 200}, token=APR2,
       probe=lambda d, _: f"共 {d['total']} 条，全部属于B部门="
       f"{all(x['department_id'] == dept_b for x in d['items'])}")
expect("审批人看不到其他部门的单", "GET", f"/api/reimbursements/{o_a1['id']}", (403, 404), token=APR2)
expect("财务可看到全部单", "GET", "/api/reimbursements", 200, params={"page_size": 200}, token=FIN,
       probe=lambda d, _: f"共 {d['total']} 条")
expect("申请人只能为自己建单（applicant_id 被强制覆盖）", "POST", "/api/reimbursements",
       {"title": f"伪造申请人-{suffix}", "applicant_id": emp_b1, "items": []}, 201, token=AP1,
       probe=lambda d, _: f"实际申请人 id={d['applicant_id']}（期望 {emp_a1}）")
expect("申请人建单的申请人就是自己", "GET", "/api/reimbursements", 200, params={"page_size": 1}, token=AP1,
       probe=lambda d, _: f"最新单申请人={d['items'][0]['applicant_id']}")

print("\n\033[1m== 分级审批 ==\033[0m")
small = new_order(AP1, f"小额单-{suffix}", 800)
expect("小额单提交后需要 1 级审批", "POST", f"/api/reimbursements/{small['id']}/submit", 200, token=AP1,
       probe=lambda d, _: f"required={d['required_level']} 状态={d['status']} 待审级别={d['pending_level']}")
expect("一级审批通过后直接完成", "POST", f"/api/reimbursements/{small['id']}/approve", 200, token=APR1,
       probe=lambda d, _: f"状态={d['status']} 已通过级数={d['approved_level']}")

big = new_order(AP1, f"大额单-{suffix}", 50000)
expect("大额单提交后需要 2 级审批", "POST", f"/api/reimbursements/{big['id']}/submit", 200, token=AP1,
       probe=lambda d, _: f"required={d['required_level']} 待审级别={d['pending_level']}")
expect("一级审批后转入二级待审", "POST", f"/api/reimbursements/{big['id']}/approve", 200, token=APR1,
       probe=lambda d, _: f"状态={d['status']} 已通过={d['approved_level']}/{d['required_level']} 待审={d['pending_level']}")
must_deny("一级审批人不能越级审二级", "POST", f"/api/reimbursements/{big['id']}/approve", token=APR1)
expect("二级审批通过后完成", "POST", f"/api/reimbursements/{big['id']}/approve", 200, token=APR2A,
       probe=lambda d, _: f"状态={d['status']} 已通过={d['approved_level']}/{d['required_level']}")

big2 = new_order(AP1, f"无级别审批测试-{suffix}", 60000)
expect("提交无级别审批测试单", "POST", f"/api/reimbursements/{big2['id']}/submit", 200, token=AP1)
must_deny("无审批级别的审批人被拒", "POST", f"/api/reimbursements/{big2['id']}/approve", token=NOLV)
cross = new_order(AP1, f"跨部门审批-{suffix}", 300)
expect("提交跨部门测试单", "POST", f"/api/reimbursements/{cross['id']}/submit", 200, token=AP1)
expect("B部门审批人看不到A部门的单", "POST", f"/api/reimbursements/{cross['id']}/approve", (403, 404), token=APR2)
expect("驳回必须填原因", "POST", f"/api/reimbursements/{cross['id']}/reject", 400, token=APR1)
expect("驳回成功并回落可编辑状态", "POST", f"/api/reimbursements/{cross['id']}/reject",
       {"comment": "发票不全，请补充"}, 200, token=APR1,
       probe=lambda d, _: f"状态={d['status']} 原因={d['reject_reason']}")
expect("驳回后申请人可撤回重编", "POST", f"/api/reimbursements/{cross['id']}/submit", 200, token=AP1,
       probe=lambda d, _: f"状态={d['status']}")
expect("审批日志记录了级别", "GET", f"/api/reimbursements/{big['id']}/logs", 200, token=TOKEN,
       probe=lambda d, _: " / ".join(f"{x['action']}@{x['level']}" for x in d))

print("\n\033[1m== 发票影像上传 ==\033[0m")


def multipart(field, filename, content, mime="application/octet-stream"):
    """手工拼 multipart，这样才能自由指定 filename（urllib 没有现成的表单构造器）。"""
    boundary = "----wbtest" + uuid.uuid4().hex
    body = io.BytesIO()
    body.write(f"--{boundary}\r\n".encode())
    body.write(
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode()
    )
    body.write(f"Content-Type: {mime}\r\n\r\n".encode())
    body.write(content)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    return body.getvalue(), f"multipart/form-data; boundary={boundary}"


inv = expect("财务登记发票", "POST", "/api/invoices",
             {"invoice_no": f"INV{suffix}0001", "amount": 1234.56, "invoice_date": "2026-09-15",
              "seller_name": "测试供应商"}, 201, token=FIN)
inv_id = inv["id"]

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 40
raw, ctype = multipart("file", "invoice.png", PNG, "image/png")
up = expect("上传发票影像成功", "POST", f"/api/invoices/{inv_id}/attachments", 201, token=FIN,
            raw=raw, ctype=ctype,
            probe=lambda d, _: f"{d['filename']} {d['size']} bytes")
aid = up["id"] if up else None

raw, ctype = multipart("file", "evil.exe", b"MZ\x90\x00")
expect("拒绝 .exe 上传", "POST", f"/api/invoices/{inv_id}/attachments", 400, token=FIN, raw=raw, ctype=ctype)

raw, ctype = multipart("file", "shot.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 100, "image/jpeg")
expect("接受 jpg 上传", "POST", f"/api/invoices/{inv_id}/attachments", 201, token=FIN, raw=raw, ctype=ctype)

raw, ctype = multipart("file", "../../../../etc/passwd", b"root:x:0:0")
expect("路径穿越文件名被拦", "POST", f"/api/invoices/{inv_id}/attachments", 400, token=FIN, raw=raw, ctype=ctype)

raw, ctype = multipart("file", "huge.pdf", b"%PDF-1.4" + b"0" * (11 * 1024 * 1024), "application/pdf")
expect("超过大小限制被拦", "POST", f"/api/invoices/{inv_id}/attachments", 400, token=FIN, raw=raw, ctype=ctype)

raw, ctype = multipart("file", "empty.pdf", b"", "application/pdf")
expect("空文件被拦", "POST", f"/api/invoices/{inv_id}/attachments", 400, token=FIN, raw=raw, ctype=ctype)

if aid:
    expect("下载影像内容与上传一致", "GET", f"/api/attachments/{aid}/raw", 200, token=FIN,
           probe=lambda d, rb: f"{len(rb)} bytes, 一致={rb == PNG}")
    expect("未登录下载影像被拦", "GET", f"/api/attachments/{aid}/raw", 401)
    expect("附件列表可见", "GET", f"/api/invoices/{inv_id}/attachments", 200, token=FIN,
           probe=lambda d, _: f"{len(d)} 个附件")
    expect("删除影像", "DELETE", f"/api/attachments/{aid}", 200, token=FIN)
    expect("删除后文件不可下载", "GET", f"/api/attachments/{aid}/raw", 404, token=FIN)

print("\n\033[1m== 操作审计 ==\033[0m")
logs = expect("审计日志可查询", "GET", "/api/audit-logs", 200, token=TOKEN,
              params={"page_size": 200},
              probe=lambda d, _: f"共 {d['total']} 条")
actions = {x["action"] for x in logs.get("items", [])} if logs else set()
for need in ["登录", "提交报销单", "审批通过", "上传发票影像"]:
    hit = any(need in a for a in actions)
    if hit:
        OK += 1
        print(f"  \033[32mPASS\033[0m 审计记录了「{need}」")
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m 审计缺少「{need}」，现有动作：{sorted(actions)[:12]}")
expect("审计记录了操作人", "GET", "/api/audit-logs", 200, token=TOKEN, params={"page_size": 5},
       probe=lambda d, _: f"最近 {len(d['items'])} 条均有操作人="
       f"{all(x['username'] for x in d['items'])}")
expect("审计记录登录失败", "GET", "/api/audit-logs", 200, token=TOKEN,
       params={"action": "登录失败", "page_size": 5},
       probe=lambda d, _: f"登录失败记录 {d['total']} 条")
expect("审计可按动作过滤", "GET", "/api/audit-logs", 200, token=TOKEN,
       params={"action": "创建账号", "page_size": 5},
       probe=lambda d, _: f"创建账号 {d['total']} 条")

print("\n\033[1m== 系统参数 ==\033[0m")
expect("读取系统参数", "GET", "/api/settings", 200, token=TOKEN,
       probe=lambda d, _: f"{len(d['items'])} 项，二级审批阈值 {d['approval']['level2_threshold']}")
expect("修改二级审批阈值", "PUT", "/api/settings/approval_level2_threshold",
       {"value": "15000"}, 200, token=TOKEN, probe=lambda d, _: f"{d['key']}={d['value']}")
threshold_test = new_order(AP1, f"阈值生效验证-{suffix}", 18000)
expect("改阈值后 1.8 万需要二级", "POST", f"/api/reimbursements/{threshold_test['id']}/submit",
       200, token=AP1, probe=lambda d, _: f"required={d['required_level']}")
expect("恢复默认阈值", "PUT", "/api/settings/approval_level2_threshold",
       {"value": "20000"}, 200, token=TOKEN)

print("\n\033[1m== 会话与账号治理 ==\033[0m")
tmp_user = f"tmp_{suffix}"
uid = make_user(tmp_user, f"临时{suffix}", "申请人", employee_id=emp_a1)
tmp_token = ACCOUNTS[tmp_user]["token"]
expect("临时账号可用", "GET", "/api/auth/me", 200, token=tmp_token)
expect("停用账号", "PUT", f"/api/users/{uid}", {"active": False}, 200, token=TOKEN)
expect("停用后原 token 立即失效", "GET", "/api/auth/me", 401, token=tmp_token)

st, me, _ = req("GET", "/api/auth/me", token=TOKEN)
admin_id = me["user"]["id"]
expect("不能删除当前登录账号", "DELETE", f"/api/users/{admin_id}", 400, token=TOKEN)

# 最后一个管理员不可降级
expect("不能停用唯一的管理员", "PUT", f"/api/users/{admin_id}", {"active": False}, 400, token=TOKEN)

expect("弱密码被拒", "POST", "/api/users",
       {"username": f"weak_{suffix}", "name": "弱密码", "role": "申请人", "password": "123"},
       (400,), token=TOKEN)

# 改密码后旧会话失效
NEW_PWD = ADMIN_PASSWORD + "New1"
expect("管理员修改密码", "POST", "/api/auth/password",
       {"old_password": ADMIN_PASSWORD, "new_password": NEW_PWD}, 200, token=TOKEN)
expect("改密后旧 token 立即失效", "GET", "/api/auth/me", 401, token=TOKEN)
expect("原密码不再可用", "POST", "/api/auth/login",
       {"username": ADMIN_USER, "password": ADMIN_PASSWORD}, 401)
TOKEN = login(ADMIN_USER, NEW_PWD)
expect("用新密码改回原密码", "POST", "/api/auth/password",
       {"old_password": NEW_PWD, "new_password": ADMIN_PASSWORD}, 200, token=TOKEN)
TOKEN = login(ADMIN_USER, ADMIN_PASSWORD)
expect("已恢复为原密码", "GET", "/api/auth/me", 200, token=TOKEN)

print(f"\n结果：{OK} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
