"""接口冒烟测试：覆盖认证 + 主数据 CRUD + 报销单全流程 + 发票 + 统计 + 导出。

生产化改造后有两个关键变化：
1. 所有业务接口都要求登录（Bearer token），凭据从环境变量取，默认用初始管理员。
2. **不再依赖演示数据**。脚本自己创建部门/员工/费用类型/客户/项目/预算，
   并用接口返回的真实 ID 串起后续流程，因此可以在干净的生产库上直接跑。

用法：
    WB_BASE_URL=http://127.0.0.1:8080 WB_PASSWORD=xxx python smoke_test.py
    python smoke_test.py http://127.0.0.1:8080
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

# 目标地址：默认本地 dev 端口。
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

# 每次运行用独立后缀，保证在同一个库上可重复执行（不撞唯一约束）
SUF = uuid.uuid4().hex[:6]


def call(method, path, body=None, params=None, auth=True):
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


def login(username: str = None, password: str = None) -> str:
    """登录并切换全局 token，返回 token。"""
    global TOKEN
    st, data = call(
        "POST", "/api/auth/login",
        {"username": username or WB_USER, "password": password or WB_PASSWORD},
        auth=False,
    )
    if st != 200:
        print(f"\033[31m登录失败：HTTP {st} {data}\033[0m")
        sys.exit(2)
    TOKEN = data["token"]
    return TOKEN


def check(label, method, path, body=None, params=None, expect=200, probe=None, auth=True):
    global OK, FAIL
    st, data = call(method, path, body, params, auth=auth)
    ok = st == expect
    extra = ""
    if ok and probe:
        try:
            extra = " | " + probe(data)
        except Exception as e:  # noqa: BLE001
            ok = False
            extra = f" | probe 失败: {e}"
    if ok:
        OK += 1
        print(f"  \033[32mPASS\033[0m {label}{extra}")
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m {label} -> HTTP {st} {str(data)[:200]}")
    return data


print(f"目标: {BASE}")
print("== 认证 ==")
check("健康检查（免登录）", "GET", "/api/health", auth=False,
      probe=lambda d: f"版本 {d['version']} 库 {d['database']} 迁移 {d.get('schema_revision')}")
check("未登录访问业务接口被拦截", "GET", "/api/reimbursements", auth=False, expect=401)
check("未登录访问统计接口被拦截", "GET", "/api/stats/overview", auth=False, expect=401)
check("未登录访问导出接口被拦截", "GET", "/api/export/reimbursements.csv", auth=False, expect=401)
check("错误密码被拒绝", "POST", "/api/auth/login",
      {"username": WB_USER, "password": "definitely-wrong"}, auth=False, expect=401)
login()
print(f"  \033[32mPASS\033[0m 登录成功（{WB_USER}），会话已建立")
check("当前登录用户", "GET", "/api/auth/me",
      probe=lambda d: f"{d['user']['name']} / {d['user']['role']}")

print("\n== 系统 ==")
check("元信息字典", "GET", "/api/meta",
      probe=lambda d: f"部门 {len(d['departments'])} 员工 {len(d['employees'])} 费用类型 {len(d['categories'])}")
check("系统参数可读", "GET", "/api/settings",
      probe=lambda d: f"{len(d['items'])} 项，二级阈值 {d['approval']['level2_threshold']}")

print("\n== 前置：自建主数据（不依赖演示数据）==")
dept = check("新建部门", "POST", "/api/departments",
             {"name": f"【自检】部门-{SUF}", "code": f"ST{SUF}"}, expect=201,
             probe=lambda d: f"id={d['id']} {d['name']}")["id"]
emp1 = check("新建员工A", "POST", "/api/employees",
             {"name": f"【自检】甲-{SUF}", "employee_no": f"E1{SUF}", "department_id": dept}, expect=201,
             probe=lambda d: f"id={d['id']}")["id"]
emp2 = check("新建员工B", "POST", "/api/employees",
             {"name": f"【自检】乙-{SUF}", "employee_no": f"E2{SUF}", "department_id": dept}, expect=201)["id"]
cat1 = check("新建费用类型（交通）", "POST", "/api/categories",
             {"name": f"【自检】市内交通-{SUF}", "code": f"C1{SUF}", "group_name": "交通费",
              "requires_invoice": True, "single_limit": 1000}, expect=201)["id"]
cat2 = check("新建费用类型（住宿）", "POST", "/api/categories",
             {"name": f"【自检】住宿费-{SUF}", "code": f"C2{SUF}", "group_name": "差旅费"}, expect=201)["id"]
cust = check("新建客户", "POST", "/api/customers",
             {"name": f"【自检】客户-{SUF}", "code": f"CU{SUF}", "industry": "软件"}, expect=201)["id"]
proj = check("新建项目", "POST", "/api/projects",
             {"name": f"【自检】项目-{SUF}", "code": f"PJ{SUF}", "customer_id": cust},
             expect=201)["id"]
bud_year = 2031 + int(SUF[:3], 16) % 60
bud = check("新建预算", "POST", "/api/budgets",
            {"year": bud_year, "month": 1, "department_id": dept, "amount": 100000}, expect=201,
            probe=lambda d: f"{d['department_name']} {d['amount']:,}")["id"]
check("重复预算被拦截", "POST", "/api/budgets",
      {"year": bud_year, "month": 1, "department_id": dept, "amount": 1}, expect=400)

print("\n== 报销单全流程 ==")
new = check("新建报销单", "POST", "/api/reimbursements", {
    "title": f"【自检】差旅费用-{SUF}",
    "applicant_id": emp1, "department_id": dept, "customer_id": cust, "project_id": proj,
    "purpose": "接口冒烟测试",
    "items": [
        {"category_id": cat1, "occur_date": "2026-09-10", "amount": 320.5, "description": "客户现场往返打车"},
        {"category_id": cat2, "occur_date": "2026-09-11", "amount": 688.0, "description": "出差住宿"},
    ],
}, expect=201, probe=lambda d: f"{d['code']} 合计 {d['total_amount']} 状态 {d['status']}")
nid = new["id"]

check("编辑草稿", "PUT", f"/api/reimbursements/{nid}", {
    "title": f"【自检】差旅费用-{SUF}（已改）",
    "applicant_id": emp1, "department_id": dept, "customer_id": cust, "project_id": proj,
    "purpose": "接口冒烟测试-修改",
    "items": [{"category_id": cat1, "occur_date": "2026-09-10", "amount": 320.5, "description": "客户现场往返打车"}],
}, probe=lambda d: f"合计 {d['total_amount']} 明细 {len(d['items'])}")

check("详情读取", "GET", f"/api/reimbursements/{nid}",
      probe=lambda d: f"{d['code']} 明细 {len(d['items'])} 发票 {len(d['invoices'])} 日志 {len(d['logs'])}")
check("不存在的单返回 404", "POST", "/api/reimbursements/999999/submit", {}, expect=404)
check("提交审批", "POST", f"/api/reimbursements/{nid}/submit", {"operator": "自检"},
      probe=lambda d: f"状态 {d['status']} 需 {d['required_level']} 级")
check("重复提交被拦截", "POST", f"/api/reimbursements/{nid}/submit", {}, expect=400)
check("撤回可回到草稿", "POST", f"/api/reimbursements/{nid}/withdraw", {},
      probe=lambda d: f"状态 {d['status']}")
check("重新提交", "POST", f"/api/reimbursements/{nid}/submit", {"operator": "自检"},
      probe=lambda d: f"状态 {d['status']}")
check("审批通过（小额 1 级即完成）", "POST", f"/api/reimbursements/{nid}/approve",
      {"operator": "自检", "approver": "管理员"},
      probe=lambda d: f"状态 {d['status']} 已通过 {d['approved_level']}/{d['required_level']}")
check("付款", "POST", f"/api/reimbursements/{nid}/pay", {"operator": "自检"},
      probe=lambda d: f"状态 {d['status']} 付款时间 {d['pay_at']}")
check("已付款不可编辑", "PUT", f"/api/reimbursements/{nid}", {"title": "x", "items": []}, expect=400)
check("撤销付款", "POST", f"/api/reimbursements/{nid}/unpay", {},
      probe=lambda d: f"状态 {d['status']}")
check("再次付款", "POST", f"/api/reimbursements/{nid}/pay", {},
      probe=lambda d: f"状态 {d['status']}")
check("日志完整性", "GET", f"/api/reimbursements/{nid}/logs",
      probe=lambda d: " -> ".join(x["action"] for x in d))

print("\n== 报销单查询 ==")
page = check("报销单列表", "GET", "/api/reimbursements", params={"page": 1, "page_size": 5},
             probe=lambda d: f"共 {d['total']} 条,本页 {len(d['items'])}")
check("状态筛选+金额排序", "GET", "/api/reimbursements",
      params={"status": "已付款", "sort": "amount_desc", "page_size": 3},
      probe=lambda d: f"共 {d['total']}, 最大 {d['items'][0]['total_amount']:,}"
      if d["items"] else "无已付款单")
check("关键字搜索", "GET", "/api/reimbursements", params={"q": "自检", "page_size": 3},
      probe=lambda d: f"命中 {d['total']}")
check("按部门筛选", "GET", "/api/reimbursements", params={"department_id": dept, "page_size": 3},
      probe=lambda d: f"命中 {d['total']}")
check("按待审级别筛选", "GET", "/api/reimbursements", params={"pending_level": 1, "page_size": 3},
      probe=lambda d: f"命中 {d['total']}")

print("\n== 发票 ==")
inv1 = check("登记发票A", "POST", "/api/invoices", {
    "invoice_no": f"INV{SUF}A", "invoice_code": f"044{SUF}",
    "invoice_type": "增值税电子普通发票", "amount": 320.5, "tax_rate": 6,
    "tax_amount": 18.14, "invoice_date": "2026-09-10",
    "seller_name": f"【自检】顺捷汽车-{SUF}", "buyer_name": "【自检】本公司",
    "category_id": cat1,
}, expect=201, probe=lambda d: f"id={d['id']} 状态 {d['check_status']}")
check("登记重复发票触发预警", "POST", "/api/invoices", {
    "invoice_no": f"INV{SUF}A", "invoice_code": f"044{SUF}",
    "amount": 320.5, "invoice_date": "2026-09-10",
    "seller_name": f"【自检】顺捷汽车-{SUF}", "category_id": cat1,
}, expect=201, probe=lambda d: f"状态 {d['check_status']} / {d['check_result']}")
dup_id = check("再登记一张用于删除", "POST", "/api/invoices", {
    "invoice_no": f"INV{SUF}C", "amount": 88.0, "invoice_date": "2026-09-12",
    "seller_name": f"【自检】供应商C-{SUF}", "category_id": cat1,
}, expect=201)["id"]

inv_list = check("发票列表", "GET", "/api/invoices", params={"page": 1, "page_size": 5},
                 probe=lambda d: f"共 {d['total']}")
check("发票汇总", "GET", "/api/invoices/summary",
      probe=lambda d: f"总 {d['total_count']} 张/{d['total_amount']:,} 待关联 {d['unlinked_count']} 重复组 {d['duplicate_count']}")
check("发票月度趋势", "GET", "/api/invoices/monthly", params={"months": 6},
      probe=lambda d: f"{len(d)} 个月" + (f", 最近 {d[-1]['month']} {d[-1]['amount']:,} ({d[-1]['count']} 张)" if d else ""))
check("仅看未关联", "GET", "/api/invoices", params={"unlinked": True, "page_size": 3},
      probe=lambda d: f"待关联 {d['total']}")
check("仅看异常", "GET", "/api/invoices", params={"check_status": "异常", "page_size": 3},
      probe=lambda d: f"异常 {d['total']}")
check("发票搜索", "GET", "/api/invoices", params={"q": "自检", "page_size": 3},
      probe=lambda d: f"命中 {d['total']}")
check("单张查验", "POST", f"/api/invoices/{inv1['id']}/check",
      probe=lambda d: f"{d['check_status']} - {d['check_result']}")
check("批量查验", "POST", "/api/invoices/batch-check", None,
      probe=lambda d: f"查验 {d['checked']} 通过 {d['ok']} 异常 {d['bad']}")
check("删除测试发票", "DELETE", f"/api/invoices/{dup_id}")
check("关联发票到报销单", "PUT", f"/api/invoices/{inv1['id']}",
      {"invoice_no": f"INV{SUF}A", "amount": 320.5, "invoice_date": "2026-09-10",
       "reimbursement_id": nid}, probe=lambda d: f"挂到单 {d['reimbursement_code']}")

print("\n== 统计 ==")
check("概览 KPI", "GET", "/api/stats/overview",
      probe=lambda d: f"总额 {d['total_amount']:,} 已付 {d['paid_amount']:,} 待审 {d['pending_count']} 超期 {d['overdue_count']}")
check("月度趋势", "GET", "/api/stats/trend", params={"months": 9},
      probe=lambda d: f"{len(d)} 个月" + (f", 首月 {d[0]['month']}={d[0]['amount']:,}" if d else ""))
check("费用类型分布", "GET", "/api/stats/by-category",
      probe=lambda d: f"{len(d)} 类" + (f", Top1 {d[0]['name']} {d[0]['ratio']}%" if d else ""))
check("费用大类分布", "GET", "/api/stats/by-group",
      probe=lambda d: f"{len(d)} 类" + (f", Top1 {d[0]['name']} {d[0]['amount']:,}" if d else ""))
check("部门排名", "GET", "/api/stats/by-department",
      probe=lambda d: f"{len(d)} 部门")
check("人员排名", "GET", "/api/stats/by-employee", params={"limit": 15},
      probe=lambda d: f"{len(d)} 人")
check("客户排名", "GET", "/api/stats/by-customer",
      probe=lambda d: f"{len(d)} 客户")
check("项目排名", "GET", "/api/stats/by-project",
      probe=lambda d: f"{len(d)} 项目")
check("部门月度堆叠", "GET", "/api/stats/by-month-department", params={"months": 6},
      probe=lambda d: f"{len(d['months'])} 月 x {len(d['series'])} 部门")
check("费用类型趋势", "GET", "/api/stats/category-trend", params={"months": 6},
      probe=lambda d: f"{len(d['months'])} 月 x {len(d['series'])} 系列")
check("预算执行", "GET", "/api/stats/budget-execution",
      probe=lambda d: f"{len(d)} 条预算" + (f", 示例执行率 {d[0]['usage_rate']}%" if d else ""))
check("异常预警", "GET", "/api/stats/alerts",
      probe=lambda d: " ".join(f"{k}={v}" for k, v in d["summary"].items()))

print("\n== 主数据列表 ==")
for res in ["departments", "employees", "categories", "customers", "projects", "budgets"]:
    check(f"{res} 列表", "GET", f"/api/{res}")

check("费用明细台账", "GET", "/api/items", params={"page_size": 5},
      probe=lambda d: f"共 {d['total']} 条 / 合计 {d['total_amount']:,}")
check("明细台账-按状态筛选", "GET", "/api/items",
      params={"status": "已付款", "page_size": 3},
      probe=lambda d: f"已付款明细 {d['total']} 条 / {d['total_amount']:,}")
check("明细台账-按费用类型筛选", "GET", "/api/items",
      params={"category_id": cat2, "page_size": 3},
      probe=lambda d: f"命中 {d['total']} 条")

print("\n== 主数据生命周期 ==")
check("编辑部门", "PUT", f"/api/departments/{dept}",
      {"name": f"【自检】部门-{SUF}B", "code": f"ST{SUF}"},
      probe=lambda d: d["name"])
# 已被员工/报销单/预算引用的部门不能删
check("删除被引用部门被拦截", "DELETE", f"/api/departments/{dept}", expect=400)
# 有报销单记录的员工不能删
check("删除被引用员工被拦截", "DELETE", f"/api/employees/{emp1}", expect=400)
# 被明细行与发票引用的费用类型不能删
check("删除被引用费用类型被拦截", "DELETE", f"/api/categories/{cat1}", expect=400)
# 未被任何地方引用的费用类型可以删
free_cat = check("新建待删除费用类型", "POST", "/api/categories",
                 {"name": f"【自检】临时类型-{SUF}", "code": f"C9{SUF}"}, expect=201)["id"]
check("删除未被引用费用类型", "DELETE", f"/api/categories/{free_cat}")
# 预算不被任何实体引用，允许直接删除
check("删除预算", "DELETE", f"/api/budgets/{bud}")

print("\n== 导出 ==")
for f in ["reimbursements.csv", "invoices.csv", "items.csv"]:
    try:
        req = urllib.request.Request(f"{BASE}/api/export/{f}")
        if TOKEN:
            req.add_header("Authorization", f"Bearer {TOKEN}")
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            lines = [x for x in raw.splitlines() if x.strip()]
        ok = r.status == 200 and raw.startswith("\ufeff") and len(lines) > 1
        if ok:
            OK += 1
            print(f"  \033[32mPASS\033[0m 导出 {f} | 表头 {len(lines[0].split(','))} 列 / {len(lines) - 1} 行")
        else:
            FAIL += 1
            print(f"  \033[31mFAIL\033[0m 导出 {f} -> 内容异常")
    except Exception as e:  # noqa: BLE001
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m 导出 {f} -> {e}")

print(f"\n结果：{OK} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
