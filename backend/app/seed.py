"""演示数据生成器与业务数据清理工具。

**演示数据只由显式命令触发，生产启动不会自动灌数据。**

用法：
    python -m app.seed --demo                     # 生成演示数据（库为空时）
    python -m app.seed --purge                    # 清掉业务数据，保留主数据与账号
    python -m app.seed --purge-all                # 连主数据一起清空（账号保留）
    python -m app.seed --reset                    # 清空后重新生成演示数据
    python -m app.seed --demo --months 12 --orders 620
"""

from __future__ import annotations

import argparse
import os
import random
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from . import models as m
from .database import Base, SessionLocal, engine

RND = random.Random(20260916)

# (部门名称, 部门编码, 负责人, 员工工号前缀)
DEPARTMENTS = [
    ("销售一部", "SALE01", "陈立", "SA"),
    ("销售二部", "SALE02", "周敏", "SB"),
    ("渠道销售部", "CHAN01", "刘一帆", "CH"),
    ("大客户部", "KEY01", "黄凯", "KA"),
    ("市场部", "MKT01", "许薇", "MK"),
    ("售前支持部", "PRE01", "郑浩", "PR"),
]

# (姓名, 部门序号, 职位, 级别)
EMPLOYEES = [
    ("陈立", 0, "销售总监", "M3"), ("王卓", 0, "高级客户经理", "P6"), ("李珊", 0, "客户经理", "P5"),
    ("赵鑫", 0, "客户经理", "P4"), ("孙悦", 0, "销售助理", "P3"), ("吴桐", 0, "客户经理", "P5"),
    ("周敏", 1, "销售总监", "M3"), ("林涛", 1, "高级客户经理", "P6"), ("何静", 1, "客户经理", "P5"),
    ("马超", 1, "客户经理", "P4"), ("徐蕾", 1, "销售助理", "P3"),
    ("刘一帆", 2, "渠道总监", "M3"), ("曾毅", 2, "渠道经理", "P5"), ("邓佳", 2, "渠道经理", "P4"),
    ("朱琳", 2, "渠道运营", "P3"),
    ("黄凯", 3, "大客户总监", "M4"), ("范晓", 3, "大客户经理", "P6"), ("薛冰", 3, "大客户经理", "P5"),
    ("顾晨", 3, "解决方案顾问", "P6"),
    ("许薇", 4, "市场总监", "M3"), ("蒋楠", 4, "市场经理", "P5"), ("卢迪", 4, "品牌专员", "P4"),
    ("汪洋", 4, "活动策划", "P4"),
    ("郑浩", 5, "售前负责人", "M3"), ("钱程", 5, "售前工程师", "P5"), ("袁媛", 5, "售前工程师", "P4"),
    ("胡兵", 5, "交付顾问", "P5"), ("秦朗", 1, "客户经理", "P4"),
]

# (费用名称, 编码, 大类, 是否需发票, 单笔限额, 单日限额, 月度限额)
CATEGORIES = [
    ("市内交通费", "C0101", "交通费", False, 500, 0, 0),
    ("城际交通费", "C0102", "交通费", True, 2000, 0, 0),
    ("高速过路费", "C0103", "交通费", True, 500, 0, 0),
    ("停车费", "C0104", "交通费", True, 200, 0, 0),
    ("车辆油费", "C0105", "交通费", True, 1000, 0, 0),
    ("住宿费", "C0201", "差旅费", True, 900, 0, 0),
    ("机票款", "C0202", "差旅费", True, 4000, 0, 0),
    ("火车票款", "C0203", "差旅费", True, 1500, 0, 0),
    ("出差补贴", "C0204", "差旅费", False, 800, 0, 0),
    ("客户招待-餐饮", "C0301", "业务招待费", True, 6000, 0, 30000),
    ("客户招待-礼品", "C0302", "业务招待费", True, 8000, 0, 20000),
    ("客户招待-娱乐", "C0303", "业务招待费", True, 8000, 0, 15000),
    ("广告投放费", "C0401", "市场推广费", True, 100000, 0, 0),
    ("展会费用", "C0402", "市场推广费", True, 200000, 0, 0),
    ("宣传物料制作", "C0403", "市场推广费", True, 50000, 0, 0),
    ("促销活动费", "C0404", "市场推广费", True, 80000, 0, 0),
    ("办公用品", "C0501", "办公通讯费", True, 3000, 0, 0),
    ("通讯费", "C0502", "办公通讯费", True, 1000, 0, 1500),
    ("快递费", "C0503", "办公通讯费", True, 500, 0, 0),
    ("会议费", "C0601", "会议培训费", True, 50000, 0, 0),
    ("培训费", "C0602", "会议培训费", True, 30000, 0, 0),
    ("其他费用", "C0901", "其他费用", True, 5000, 0, 0),
]

CUSTOMERS = [
    ("深圳前海数字科技有限公司", "CRM001", "王建国", "信息技术", "互联网"),
    ("广州云启智能制造有限公司", "CRM002", "李慧", "智能制造", "制造业"),
    ("东莞恒通电子股份有限公司", "CRM003", "张鹏", "电子信息", "制造业"),
    ("珠海格创能源集团有限公司", "CRM004", "陈曦", "新能源", "能源"),
    ("佛山联达物流有限公司", "CRM005", "刘敏", "物流运输", "物流"),
    ("惠州比亚迪供应链中心", "CRM006", "赵磊", "汽车供应链", "汽车"),
    ("中山医康医疗科技有限公司", "CRM007", "孙丽", "医疗器械", "医疗"),
    ("厦门海丝跨境电商有限公司", "CRM008", "周洋", "跨境电商", "零售"),
    ("成都天府软件园运营公司", "CRM009", "杨帆", "园区运营", "地产"),
    ("武汉长江智算中心", "CRM010", "马骏", "算力服务", "信息技术"),
    ("杭州数智零售集团", "CRM011", "林芳", "零售连锁", "零售"),
    ("上海浦江新材料有限公司", "CRM012", "徐涛", "新材料", "制造业"),
]

PROJECTS = [
    ("前海数字云平台一期", "PRJ001", 0, "王卓", "商务谈判", "进行中"),
    ("云启智能工厂升级", "PRJ002", 1, "李珊", "方案交流", "进行中"),
    ("恒通电子供应链协同", "PRJ003", 2, "赵鑫", "招投标", "进行中"),
    ("格创能源数据中台", "PRJ004", 3, "黄凯", "合同签署", "进行中"),
    ("联达物流调度系统", "PRJ005", 4, "林涛", "商务谈判", "进行中"),
    ("惠州供应链数字化", "PRJ006", 5, "范晓", "方案交流", "进行中"),
    ("医康医疗信息化改造", "PRJ007", 6, "薛冰", "招投标", "进行中"),
    ("海丝跨境数字营销", "PRJ008", 7, "蒋楠", "需求调研", "进行中"),
    ("天府软件园智慧园区", "PRJ009", 8, "曾毅", "方案交流", "进行中"),
    ("长江智算中心建设", "PRJ010", 9, "顾晨", "商务谈判", "进行中"),
    ("数智零售会员体系", "PRJ011", 10, "周敏", "合同签署", "进行中"),
    ("浦江新材料产线改造", "PRJ012", 11, "王卓", "需求调研", "进行中"),
    ("前海二期扩容项目", "PRJ013", 0, "吴桐", "商务谈判", "进行中"),
    ("恒通电子售后维保", "PRJ014", 2, "马超", "售后服务", "进行中"),
    ("渠道伙伴年度大会", "PRJ015", 2, "刘一帆", "活动执行", "已结束"),
    ("2026 华南数字峰会", "PRJ016", 4, "汪洋", "活动执行", "进行中"),
    ("新能源行业解决方案库", "PRJ017", 5, "郑浩", "内部建设", "进行中"),
    ("大客户联合创新实验室", "PRJ018", 3, "黄凯", "战略合作", "进行中"),
]

# 费用类型 -> (金额下限, 金额上限, 说明模板)
AMOUNT_RANGE = {
    "C0101": (45, 480, ["客户现场往返打车", "机场往返打车", "拜访客户交通", "市内公务出行"]),
    "C0102": (180, 1800, ["高铁往返客户现场", "城际大巴票", "跨城商务出行"]),
    "C0103": (25, 420, ["自驾拜访客户高速费", "项目现场往返过路费"]),
    "C0104": (8, 180, ["客户园区停车费", "机场停车费"]),
    "C0105": (260, 950, ["项目现场往返油费", "客户巡访车辆加油"]),
    "C0201": (320, 880, ["客户所在地住宿", "出差住宿费（标准间）"]),
    "C0202": (680, 3600, ["深圳-客户所在地往返机票", "出差航班"]),
    "C0203": (120, 1400, ["出差高铁票", "往返火车票"]),
    "C0204": (150, 780, ["出差期间伙食补贴", "出差补贴"]),
    "C0301": (380, 5600, ["客户项目组聚餐", "商务宴请", "客户来访招待餐费"]),
    "C0302": (260, 7600, ["客户节日慰问礼品", "项目签约纪念品"]),
    "C0303": (560, 7200, ["客户团队团建活动", "商务休闲招待"]),
    "C0401": (3000, 28000, ["信息流广告投放", "行业媒体投放"]),
    "C0402": (6000, 42000, ["行业展会标准展位", "峰会赞助及展台搭建"]),
    "C0403": (1200, 14000, ["宣传册与易拉宝制作", "产品手册印刷"]),
    "C0404": (1500, 22000, ["渠道促销活动支持", "客户答谢活动费用"]),
    "C0501": (80, 2600, ["办公耗材采购", "打印纸及文具"]),
    "C0502": (120, 860, ["月度手机通讯费", "商务通讯补贴"]),
    "C0503": (25, 280, ["合同寄送快递费", "样机寄送费用"]),
    "C0601": (1500, 20000, ["渠道伙伴会议场地", "季度销售会议"]),
    "C0602": (600, 11000, ["解决方案认证培训", "销售能力培训"]),
    "C0901": (120, 3200, ["未归类业务支出"]),
}

TITLE_TPL = [
    "{cust}项目差旅费用报销",
    "{cust}商务拜访费用报销",
    "{month}月{cust}客户招待费用",
    "{proj}项目现场支持费用",
    "{cust}招投标阶段费用报销",
    "{proj}会议及培训费用",
    "{cust}渠道推广活动费用",
    "{month}月市场活动费用报销",
]

# 费用发生主题：决定费用类型与标题，权重贴近真实销售费用结构
# （差旅/招待为主，市场推广与会议占比小但单笔金额大）
THEMES = [
    {
        "name": "差旅",
        "w": 36,
        "codes": [["C0101", 3], ["C0102", 2], ["C0201", 3], ["C0202", 1.2],
                  ["C0203", 1.5], ["C0204", 2], ["C0104", 1.5], ["C0105", 1]],
        "titles": ["{cust}项目差旅费用报销", "{proj}项目现场支持费用", "{cust}商务拜访费用报销"],
    },
    {
        "name": "招待",
        "w": 31,
        "codes": [["C0301", 6], ["C0302", 2.5], ["C0303", 1.5]],
        "titles": ["{month}月{cust}客户招待费用", "{cust}客户关系维护费用", "{proj}商务洽谈招待费用"],
    },
    {
        "name": "交通",
        "w": 20,
        "codes": [["C0101", 4], ["C0103", 2], ["C0104", 2], ["C0105", 1.5]],
        "titles": ["{cust}商务拜访费用报销", "{cust}招投标阶段费用报销", "{month}月市内公务出行费用"],
    },
    {
        "name": "会议",
        "w": 4,
        "codes": [["C0601", 2], ["C0602", 1.5], ["C0101", 3]],
        "titles": ["{proj}会议及培训费用", "{month}月销售例会费用", "{cust}方案汇报会议费用"],
    },
    {
        "name": "推广",
        "w": 3,
        "codes": [["C0401", 3], ["C0402", 1.5], ["C0403", 2.5], ["C0404", 2]],
        "titles": ["{cust}渠道推广活动费用", "{month}月市场活动费用报销", "{proj}展会及物料费用"],
    },
    {
        "name": "办公",
        "w": 6,
        "codes": [["C0501", 3], ["C0502", 3], ["C0503", 2.5]],
        "titles": ["{month}月销售办公费用", "{proj}项目办公耗材费用", "{month}月通讯及快递费用"],
    },
]

SELLERS = [
    ("深圳市顺捷汽车服务有限公司", "91440300MA5E1X2K3A"),
    ("中国南方航空股份有限公司", "91440000100017600N"),
    ("广州铁路（集团）公司", "91440000100010011T"),
    ("深圳福朋喜来登酒店", "914403007123456789"),
    ("深圳市海悦餐饮管理有限公司", "91440300MA5F8H9J2B"),
    ("深圳礼尚往来文化礼品有限公司", "91440300MA5G3K7L9C"),
    ("今日头条广告（深圳）有限公司", "91440300098260123X"),
    ("深圳市会展中心运营有限公司", "91440300192185783Y"),
    ("深圳印之美印刷包装有限公司", "91440300MA5D6N4P7Q"),
    ("中国移动通信集团广东有限公司", "91440000710931672L"),
    ("顺丰速运（集团）有限公司", "91440300708461304W"),
    ("深圳前海亚太会议服务有限公司", "91440300MA5H2M8R4D"),
]

BUYER = ("深圳市智联云创科技有限公司", "91440300MA5C8T1Q6E")


def _rand_dt(d: date, lo=9, hi=20) -> datetime:
    return datetime.combine(d, time(RND.randint(lo, hi - 1), RND.randint(0, 59)))


def seed(db: Session, months: int = 12, orders: int = 620) -> dict:
    today = date.today()

    # ---- 部门 ----
    depts = [m.Department(name=n, code=c, manager=g) for n, c, g, _ in DEPARTMENTS]
    db.add_all(depts)
    db.flush()

    # ---- 员工 ----
    emps = []
    seq = {d.id: 1 for d in depts}
    emp_prefix = {d.id: DEPARTMENTS[i][3] for i, d in enumerate(depts)}
    for name, di, position, level in EMPLOYEES:
        dept = depts[di]
        code = f"{emp_prefix[dept.id]}{seq[dept.id]:03d}"
        seq[dept.id] += 1
        emps.append(
            m.Employee(
                name=name,
                employee_no=code,
                department_id=dept.id,
                position=position,
                level=level,
                email=f"{code.lower()}@zhilianyun.com",
                phone=f"138{RND.randint(10000000, 99999999)}",
                bank_account=f"622202{RND.randint(1000000000000, 9999999999999)}",
            )
        )
    db.add_all(emps)
    db.flush()

    # ---- 费用类型 ----
    cats = [
        m.ExpenseCategory(
            name=n, code=c, group_name=g, requires_invoice=ri,
            single_limit=sl, daily_limit=dl, monthly_limit=ml,
        )
        for n, c, g, ri, sl, dl, ml in CATEGORIES
    ]
    db.add_all(cats)
    db.flush()
    cat_by_code = {c.code: c for c in cats}
    amount_range = {cat_by_code[code].id: v for code, v in AMOUNT_RANGE.items()}

    # ---- 客户 & 项目 ----
    custs = [
        m.Customer(name=n, code=c, contact=ct, industry=ind, remark=rm)
        for n, c, ct, ind, rm in CUSTOMERS
    ]
    db.add_all(custs)
    db.flush()
    projs = [
        m.Project(name=n, code=c, customer_id=custs[ci].id, manager=mg, stage=st, status=stt)
        for n, c, ci, mg, st, stt in PROJECTS
    ]
    db.add_all(projs)
    db.flush()

    # ---- 报销单 ----
    status_pool = (
        [m.ST_PAID] * 58 + [m.ST_APPROVED] * 12 + [m.ST_PENDING] * 14
        + [m.ST_REJECTED] * 8 + [m.ST_DRAFT] * 8
    )
    invoice_seq = 0
    dup_pool: list[tuple[str, str]] = []
    codes_used: set[tuple[str, str]] = set()

    for i in range(orders):
        days_ago = RND.randint(0, months * 30 - 1)
        occur = today - timedelta(days=days_ago)
        emp = RND.choice(emps)
        proj = RND.choice(projs)
        cust = next((c for c in custs if c.id == proj.customer_id), RND.choice(custs))
        # 先按权重抽「费用主题」，再在主题内取费用类型 —— 保证数据结构贴近真实
        theme = RND.choices(THEMES, weights=[t["w"] for t in THEMES])[0]
        title = RND.choice(theme["titles"]).format(
            cust=cust.name[:6], proj=proj.name, month=occur.month
        )
        status = status_pool[i % len(status_pool)] if i < len(status_pool) else RND.choice(status_pool)

        n_items = RND.choices([1, 2, 3, 4, 5], weights=[30, 32, 22, 11, 5])[0]
        pick_codes = theme["codes"]

        items = []
        for _ in range(n_items):
            code = RND.choices([c for c, _ in pick_codes], weights=[w for _, w in pick_codes])[0]
            cat = cat_by_code[code]
            lo, hi, descs = amount_range[cat.id]
            amt = round(RND.uniform(lo, hi), 2)
            # 少量明细故意超过单笔限额，用于演示限额预警
            if cat.single_limit and RND.random() < 0.07:
                amt = round(float(cat.single_limit) * RND.uniform(1.08, 1.75), 2)
            items.append(
                m.ReimbursementItem(
                    category_id=cat.id,
                    occur_date=occur - timedelta(days=RND.randint(0, 3)),
                    amount=amt,
                    tax_amount=round(amt / 1.06 * 0.06, 2),
                    description=RND.choice(descs),
                )
            )

        r = m.Reimbursement(
            code=f"BX{occur.strftime('%Y%m')}{i + 1:04d}",
            title=title,
            applicant_id=emp.id,
            department_id=emp.department_id,
            project_id=proj.id,
            customer_id=cust.id,
            purpose=f"{proj.name}推进过程中的{('差旅/招待等' if n_items > 2 else '必要')}业务支出",
            status=status,
            occur_start=min(x.occur_date for x in items),
            occur_end=max(x.occur_date for x in items),
            items=items,
            total_amount=round(sum(x.amount for x in items), 2),
        )
        db.add(r)
        db.flush()

        # ---- 发票：需发票的明细大多有票 ----
        inv_cnt = 0
        for it in items:
            cat = next(c for c in cats if c.id == it.category_id)
            if not cat.requires_invoice:
                continue
            if RND.random() > 0.88:
                continue  # 留一部分缺票，触发预警
            invoice_seq += 1
            seller, tax_no = RND.choice(SELLERS)
            # 6% 概率复用历史发票号，用于演示「重复报销」预警
            if RND.random() < 0.06 and dup_pool:
                no, code_ = RND.choice(dup_pool)
                is_dup = True
            else:
                no = f"{RND.randint(10000000, 99999999)}"
                code_ = f"0440{RND.randint(1000000000, 9999999999)}"
                dup_pool.append((no, code_))
                is_dup = False

            if r.status in (m.ST_DRAFT, m.ST_REJECTED) and RND.random() < 0.5:
                check = m.CHECK_UNVERIFIED
            elif is_dup:
                check = m.CHECK_BAD
            else:
                check = m.CHECK_OK

            # 约 12% 的发票先录入台账、暂未关联报销单（用于演示「待关联」）
            linked = RND.random() > 0.12
            db.add(
                m.Invoice(
                    invoice_no=no,
                    invoice_code=code_,
                    invoice_type=RND.choice([
                        "增值税电子普通发票", "电子发票（普通发票）",
                        "增值税专用发票", "电子发票（增值税专用发票）",
                    ]),
                    amount=it.amount,
                    tax_rate=6.0,
                    tax_amount=it.tax_amount,
                    invoice_date=it.occur_date,
                    seller_name=seller,
                    seller_tax_no=tax_no,
                    buyer_name=BUYER[0],
                    buyer_tax_no=BUYER[1],
                    check_status=check,
                    check_result=(
                        f"疑似重复报销：发票号码 {no} 已存在" if check == m.CHECK_BAD
                        else ("基础校验通过" if check == m.CHECK_OK else None)
                    ),
                    category_id=it.category_id,
                    reimbursement_id=r.id if linked else None,
                    item_id=it.id if linked else None,
                    file_name=f"invoice_{no}.pdf",
                    remark=None if linked else "待关联报销单",
                )
            )
            inv_cnt += 1

        r.invoice_count = (
            db.query(m.Invoice).filter(m.Invoice.reimbursement_id == r.id).count()
        )

        # ---- 状态与流转日志 ----
        operator = emp.name
        db.add(m.ApprovalLog(reimbursement_id=r.id, action="创建", operator=operator,
                             to_status=m.ST_DRAFT, comment="创建报销单",
                             created_at=_rand_dt(r.occur_start + timedelta(days=4))))
        if r.status != m.ST_DRAFT:
            sub = _rand_dt(r.occur_end + timedelta(days=RND.randint(2, 7)))
            r.submit_at = sub
            db.add(m.ApprovalLog(reimbursement_id=r.id, action="提交", operator=operator,
                                 from_status=m.ST_DRAFT, to_status=m.ST_PENDING,
                                 created_at=sub))
        if r.status in (m.ST_APPROVED, m.ST_PAID, m.ST_REJECTED):
            appr = _rand_dt((r.submit_at or datetime.now()) + timedelta(days=RND.randint(1, 5)))
            r.approve_at = appr
            r.approver = RND.choice(["陈立", "周敏", "黄凯", "许薇", "刘一帆", "财务-林会计"])
            if r.status == m.ST_REJECTED:
                r.reject_reason = RND.choice([
                    "发票信息与明细金额不一致，请核对后重新提交",
                    "该笔招待费用超出部门月度预算，请补充审批说明",
                    "缺少客户签字确认单，请补充附件",
                    "费用发生日期与出差申请单不匹配",
                ])
                db.add(m.ApprovalLog(reimbursement_id=r.id, action="驳回", operator=r.approver,
                                     from_status=m.ST_PENDING, to_status=m.ST_REJECTED,
                                     comment=r.reject_reason, created_at=appr))
            else:
                db.add(m.ApprovalLog(reimbursement_id=r.id, action="通过", operator=r.approver,
                                     from_status=m.ST_PENDING, to_status=m.ST_APPROVED,
                                     comment="费用真实、票据合规，同意报销", created_at=appr))
        if r.status == m.ST_PAID:
            r.pay_at = _rand_dt((r.approve_at or datetime.now()) + timedelta(days=RND.randint(1, 6)))
            db.add(m.ApprovalLog(reimbursement_id=r.id, action="付款", operator="财务-林会计",
                                 from_status=m.ST_APPROVED, to_status=m.ST_PAID,
                                 comment="已通过银行转账支付", created_at=r.pay_at))

    # ---- 预算 ----
    year = today.year
    for dept in depts:
        for month in range(1, 13):
            base = {"SALE01": 220000, "SALE02": 200000, "CHAN01": 150000,
                    "KEY01": 260000, "MKT01": 420000, "PRE01": 120000}[dept.code]
            db.add(m.Budget(year=year, month=month, department_id=dept.id,
                            amount=round(base * RND.uniform(0.9, 1.15), 2), remark="部门月度费用预算"))
    for code, total in [("C0301", 1200000), ("C0302", 700000), ("C0401", 3000000),
                        ("C0402", 5000000), ("C0201", 1800000), ("C0202", 2600000)]:
        db.add(m.Budget(year=year, month=None, category_id=cat_by_code[code].id,
                        amount=total, remark="年度费用科目预算"))

    db.commit()
    return {
        "departments": db.query(m.Department).count(),
        "employees": db.query(m.Employee).count(),
        "categories": db.query(m.ExpenseCategory).count(),
        "customers": db.query(m.Customer).count(),
        "projects": db.query(m.Project).count(),
        "reimbursements": db.query(m.Reimbursement).count(),
        "items": db.query(m.ReimbursementItem).count(),
        "invoices": db.query(m.Invoice).count(),
        "budgets": db.query(m.Budget).count(),
    }


def ensure_seed() -> dict | None:
    """库为空且显式要求时才灌入演示数据。

    **生产环境默认关闭**：只有 SEED_DEMO=1 才会执行，避免演示数据被带进正式库。
    需要演示数据请显式执行 `python -m app.seed --demo`。
    初始化失败（例如多副本并发启动撞唯一约束）不应阻塞服务启动，
    这里捕获异常并回滚，服务照常提供只读访问。
    """
    if os.getenv("SEED_DEMO", "0").lower() not in ("1", "true", "yes"):
        return None

    db = SessionLocal()
    try:
        if db.query(m.Employee).count() > 0:
            return None
        return seed(db)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[seed] 初始化演示数据失败（服务继续启动）：{exc}")
        return None
    finally:
        db.close()


# 清除顺序：先子表后主表，避免外键约束报错
PURGE_ORDER = [
    m.InvoiceAttachment,
    m.Invoice,
    m.ApprovalLog,
    m.ReimbursementItem,
    m.Reimbursement,
    m.Budget,
    m.AuditLog,
    m.UserSession,
    m.MailMessage,
]

# 主数据（连主数据一起清时才动）
MASTER_ORDER = [
    m.Project,
    m.Customer,
    m.ExpenseCategory,
    m.Employee,
    m.Department,
]


def purge(
    db: Session,
    *,
    include_master: bool = False,
    keep_audit: bool = True,
    clear_files: bool = True,
    reset_mail: bool = True,
) -> dict:
    """清除业务数据。

    include_master=False（默认）：只清报销单/明细/发票/预算等业务数据，
      保留部门、员工、费用类型等主数据，以及账号——正式上线时把演示单据清掉即可。
    include_master=True：连主数据一起清，适合完全从零开始录入。
      账号会保留，但会把 employee_id 解绑，避免外键指向已删除的员工。
    clear_files：同时删除 UPLOAD_DIR 里的影像文件。只删库不删文件会留下
      一堆再也对不上号的孤儿文件，所以默认开启。
    reset_mail：把邮箱账号的同步进度清零，便于清库后重新收票。
    """
    counts: dict[str, int] = {}
    for model in PURGE_ORDER:
        if keep_audit and model is m.AuditLog:
            continue
        counts[model.__tablename__] = db.query(model).delete()

    if include_master:
        # 员工被删之前先解开账号上的绑定，否则外键悬空
        db.query(m.AppUser).update({"employee_id": None}, synchronize_session=False)
        for model in MASTER_ORDER:
            counts[model.__tablename__] = db.query(model).delete()

    if reset_mail:
        db.query(m.MailAccount).update(
            {
                "last_sync_at": None,
                "last_sync_status": None,
                "last_sync_detail": None,
                "imported_total": 0,
            },
            synchronize_session=False,
        )

    db.commit()

    if clear_files:
        counts["uploads_removed"] = clear_upload_files()
    return counts


def clear_upload_files() -> int:
    """清空影像目录。返回删除的文件数。"""
    from . import filestore as fs

    n = 0
    for path in fs.UPLOAD_DIR.glob("*"):
        if not path.is_file():
            continue
        try:
            path.unlink()
            n += 1
        except OSError:
            pass
    return n


def main() -> None:
    ap = argparse.ArgumentParser(
        description="销售费用报销工作台 - 数据初始化 / 清理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "正式部署常用：\n"
            "  python -m app.seed --purge        清掉演示单据，保留主数据与账号\n"
            "  python -m app.seed --purge-all    连主数据一起清空（账号保留）\n"
            "  python -m app.seed --demo         灌入一批演示数据（仅供演示/培训）\n"
        ),
    )
    ap.add_argument("--demo", action="store_true", help="生成演示数据")
    ap.add_argument("--purge", action="store_true", help="清除业务数据，保留主数据与账号")
    ap.add_argument("--purge-all", action="store_true", help="连主数据一起清除，账号保留")
    ap.add_argument(
        "--purge-business",
        "--clean",
        dest="purge_business",
        action="store_true",
        help="交付前清库：清空全部业务数据与主数据、删除影像文件，只保留账号与系统参数",
    )
    ap.add_argument("--keep-files", action="store_true", help="清库时保留 UPLOAD_DIR 里的影像文件")
    ap.add_argument("--reset", action="store_true", help="等价于 --purge --demo")
    ap.add_argument("--months", type=int, default=12, help="生成多少个月区间的数据")
    ap.add_argument("--orders", type=int, default=620, help="生成多少张报销单")
    ap.add_argument("--yes", action="store_true", help="跳过确认（脚本化部署用）")
    args = ap.parse_args()

    if not (args.demo or args.purge or args.purge_all or args.reset or args.purge_business):
        ap.print_help()
        return

    # 保证表结构是最新的再动数据
    from .migrate import upgrade_to_head

    upgrade_to_head()

    full_clean = args.purge_business or args.purge_all or args.reset
    destructive = args.purge or full_clean
    if destructive and not args.yes:
        tip = "连同全部主数据（部门/员工/费用类型/客户/项目）与影像文件" if full_clean else "全部业务数据"
        answer = input(f"即将删除{tip}，且不可恢复。输入 yes 继续：").strip().lower()
        if answer != "yes":
            print("已取消")
            return

    db = SessionLocal()
    try:
        if destructive:
            counts = purge(
                db,
                include_master=full_clean,
                keep_audit=not full_clean,
                clear_files=not args.keep_files,
            )
            print("已清除：")
            for k, v in counts.items():
                if v:
                    print(f"  {k:24s} {v}")
            if args.purge_business:
                print("账号与系统参数已保留，管理员可直接登录继续使用。")

        if args.demo:
            if db.query(m.Employee).count() > 0:
                print("库中已有数据，跳过演示数据生成。如需重建请用 --reset")
                return
            stats = seed(db, months=args.months, orders=args.orders)
            print("演示数据生成完成：")
            for k, v in stats.items():
                print(f"  {k:16s} {v}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
