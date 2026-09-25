"""扫码解析接口测试（v2.8.0）

为什么直调函数而不是打 HTTP：
  生产管理员口令已改、不在脚本里，而扫码功能真正要守的两件事是
  「类型识别」与「数据权限」，这两点在函数层就能完整覆盖。
  所以这里用临时 SQLite 库造最小数据，直接调 scan.resolve(code, user, db)，
  不依赖任何生产凭据，也不会碰生产库。

用法：
    python scan_test.py
"""
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 必须在 import app.* 之前改 DATABASE_URL —— database.py 在模块级读环境变量。
# 用 /tmp 下的临时目录，跑完即弃，不落进仓库。
TMP = pathlib.Path(tempfile.mkdtemp(prefix="scan_test_"))
os.environ["DATABASE_URL"] = "sqlite:///" + str(TMP / "t.db")

from app import database as dbm  # noqa: E402
from app import models as m  # noqa: E402
from app.routers import scan  # noqa: E402

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


def main():
    m.Base.metadata.create_all(bind=dbm.engine)
    db = dbm.SessionLocal()

    # ---- 最小数据集：两个部门 / 两个员工 / 四类角色 ----
    d1 = m.Department(name="技术部")
    d2 = m.Department(name="销售部")
    db.add_all([d1, d2])
    db.flush()

    e1 = m.Employee(name="张三", employee_no="E001", department_id=d1.id)
    e2 = m.Employee(name="李四", employee_no="E002", department_id=d2.id)
    e3 = m.Employee(name="王主管", employee_no="E003", department_id=d1.id)
    db.add_all([e1, e2, e3])
    db.flush()

    u_own = m.AppUser(username="zhangsan", password_hash="x", name="张三",
                      role=m.ROLE_APPLICANT, employee_id=e1.id)
    u_other = m.AppUser(username="lisi", password_hash="x", name="李四",
                        role=m.ROLE_APPLICANT, employee_id=e2.id)
    u_appr = m.AppUser(username="lead", password_hash="x", name="王主管",
                       role=m.ROLE_APPROVER, employee_id=e3.id, approval_level=1)
    u_fin = m.AppUser(username="caiwu", password_hash="x", name="财务小李", role=m.ROLE_FINANCE)
    u_admin = m.AppUser(username="root", password_hash="x", name="管理员", role=m.ROLE_ADMIN)
    db.add_all([u_own, u_other, u_appr, u_fin, u_admin])
    db.flush()

    r_d1 = m.Reimbursement(code="BX20260924001", title="差旅费报销", applicant_id=e1.id,
                           department_id=d1.id, total_amount=1234.56, status=m.ST_DRAFT)
    r_d2 = m.Reimbursement(code="BX20260924002", title="招待费报销", applicant_id=e2.id,
                           department_id=d2.id, total_amount=800.0, status=m.ST_DRAFT)
    db.add_all([r_d1, r_d2])
    db.flush()

    v_own = m.Invoice(invoice_no="26912000000479801234", amount=600.0,
                      seller_name="深圳市某某科技有限公司", created_by="zhangsan")
    db.add(v_own)
    db.commit()

    def resolve(code, user):
        return scan.resolve(code=code, user=user, db=db)

    print("\n[1] 报销单识别")
    res = resolve("BX20260924001", u_own)
    check("本人扫自己的单 → 命中且带标题/摘要",
          res["found"] and res["type"] == "reimbursement" and res["id"] == r_d1.id,
          str(res))
    check("摘要含申请人/部门/金额/状态",
          "张三" in res["subtitle"] and "技术部" in res["subtitle"] and "1,234.56" in res["subtitle"],
          res["subtitle"])
    check("单号大小写不敏感（bx... → 命中）", resolve("bx20260924001", u_own)["found"])
    check("两侧空白自动裁剪", resolve("  BX20260924001  ", u_own)["found"])

    print("\n[2] 数据权限（扫码不能越权）")
    check("他人扫不到不属于自己的单", not resolve("BX20260924001", u_other)["found"])
    check("申请人扫别人部门的单也扫不到", not resolve("BX20260924002", u_own)["found"])
    check("审批人可见本部门（技术部）单", resolve("BX20260924001", u_appr)["found"])
    check("审批人看不到非本部门（销售部）单", not resolve("BX20260924002", u_appr)["found"])
    check("财务可见全部单", resolve("BX20260924002", u_fin)["found"])
    check("管理员可见全部单", resolve("BX20260924002", u_admin)["found"])

    print("\n[3] 发票识别")
    res = resolve("26912000000479801234", u_fin)
    check("发票号 → 命中发票", res["found"] and res["type"] == "invoice" and res["id"] == v_own.id, str(res))
    check("发票摘要含销售方与金额", "某某科技" in res["subtitle"] and "600.00" in res["subtitle"], res["subtitle"])
    check("登记人可扫到自己登记的散票", resolve("26912000000479801234", u_own)["found"])
    check("无关员工扫不到别人的散票", not resolve("26912000000479801234", u_other)["found"])

    print("\n[4] 清单批次号")
    res = resolve("INV-LIST-20260924-N5", u_own)
    check("INV-LIST- → invoice_list 类型", res["found"] and res["type"] == "invoice_list", str(res))

    print("\n[5] 兜底与异常输入")
    check("不存在的单号 → found=false 且带提示",
          (lambda x: (not x["found"]) and bool(x.get("hint")))(resolve("BX99999999999", u_admin)))
    check("乱码 → found=false", not resolve("HELLO-WORLD", u_admin)["found"])
    check("短数字（不足 8 位）→ 不误判为发票号", not resolve("1234567", u_admin)["found"])

    db.close()
    print(f"\n结果：{OK}/{OK + FAIL} 通过")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
