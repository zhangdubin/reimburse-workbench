"""生产环境初始化：建初始管理员、补齐默认参数。

与演示数据完全分开：演示数据只在显式执行 `python -m app.seed --demo` 时生成，
正式部署时容器启动只会走到这里，库是干净的。

初始管理员的密码只从环境变量取，且首次登录强制修改。
"""

from __future__ import annotations

import os
import secrets

from sqlalchemy.orm import Session

from . import approvals as ap
from . import models as m
from . import security as sec
from .database import SessionLocal


def ensure_admin(db: Session) -> dict:
    """没有管理员账号时创建一个。已存在则不动任何东西。

    密码优先级：ADMIN_PASSWORD 环境变量 > 自动生成随机密码并打印到日志。
    随机密码只在首次初始化时出现一次，请立即保存。
    """
    existing = db.query(m.AppUser).filter(m.AppUser.role == m.ROLE_ADMIN).count()
    if existing:
        return {"created": False, "reason": "已存在管理员账号"}

    username = (os.getenv("ADMIN_USERNAME") or "admin").strip()
    name = (os.getenv("ADMIN_NAME") or "系统管理员").strip()
    password = os.getenv("ADMIN_PASSWORD") or ""
    generated = False
    if not password:
        # 16 字节 url-safe 随机串，足够强且不易打错
        password = secrets.token_urlsafe(12)
        generated = True
    else:
        sec.check_password_strength(password)

    if db.query(m.AppUser).filter(m.AppUser.username == username).first():
        return {"created": False, "reason": f"用户名 {username} 已被占用"}

    db.add(
        m.AppUser(
            username=username,
            name=name,
            role=m.ROLE_ADMIN,
            password_hash=sec.hash_password(password),
            active=True,
            must_change_password=True,
        )
    )
    db.commit()

    if generated:
        print("=" * 64)
        print("[init] 已创建初始管理员账号，请立即保存并首次登录后修改密码")
        print(f"[init]   用户名: {username}")
        print(f"[init]   初始密码: {password}")
        print("=" * 64)
    else:
        print(f"[init] 已创建初始管理员账号：{username}（密码来自 ADMIN_PASSWORD）")
    return {"created": True, "username": username, "password": password if generated else None}


def bootstrap() -> dict:
    """容器启动时调用：参数补齐 + 管理员初始化。任何一步失败都不阻塞启动。"""
    result: dict = {}
    db = SessionLocal()
    try:
        result["settings_added"] = ap.ensure_default_settings(db)
        result.update(ensure_admin(db))
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[init] 初始化失败（服务继续启动）：{exc}")
        result["error"] = str(exc)
    finally:
        db.close()
    return result


# ---------------------------------------------------------------------------
# 运维 CLI：忘记管理员密码时的救援入口
#
#   python -m app.bootstrap --list-admin            看有哪些管理员
#   python -m app.bootstrap --reset-admin           重置为随机密码并打印
#   python -m app.bootstrap --reset-admin --password 'Xx@12345678'
#
# 刻意只允许重置管理员，避免它变成「任意改别人密码」的后门。
# ---------------------------------------------------------------------------


def _reset_admin(db: Session, username: str | None, password: str | None) -> int:
    q = db.query(m.AppUser).filter(m.AppUser.role == m.ROLE_ADMIN)
    if username:
        q = q.filter(m.AppUser.username == username)
    admins = q.order_by(m.AppUser.id).all()
    if not admins:
        print(f"[reset] 没有找到管理员账号{'：' + username if username else ''}")
        return 1

    # 只重置一个：指定了用户名就用它，否则取 id 最小的那个（通常是初始管理员）
    target = admins[0]
    raw = password or secrets.token_urlsafe(12)
    if password:
        sec.check_password_strength(password)
    target.password_hash = sec.hash_password(raw)
    target.active = True
    target.must_change_password = True
    db.commit()
    # 改密必须踢掉该账号的所有会话，否则旧 token 还能用
    killed = sec.revoke_all_sessions(db, target.id)

    print("=" * 64)
    print(f"[reset] 已重置管理员：{target.username}（{target.name}）")
    print(f"[reset] 新密码：{raw}")
    print(f"[reset] 已吊销 {killed} 个在途会话；下次登录会要求再改一次密码")
    if len(admins) > 1:
        others = "、".join(a.username for a in admins if a.id != target.id)
        print(f"[reset] 其余管理员未受影响：{others}")
    print("=" * 64)
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .migrate import upgrade_to_head

    ap_ = argparse.ArgumentParser(description="生产初始化与管理员救援工具")
    ap_.add_argument("--list-admin", action="store_true", help="列出所有管理员账号")
    ap_.add_argument("--reset-admin", action="store_true", help="重置管理员密码（默认取最早的账号）")
    ap_.add_argument("--user", default=None, help="配合 --reset-admin 指定用户名")
    ap_.add_argument("--password", default=None, help="新密码；不传则随机生成并打印")
    ap_.add_argument("--init", action="store_true", help="执行一次常规初始化（参数补齐 + 建管理员）")
    args = ap_.parse_args(argv)

    if not (args.list_admin or args.reset_admin or args.init):
        ap_.print_help()
        return 0

    upgrade_to_head()
    db = SessionLocal()
    try:
        if args.list_admin:
            rows = (
                db.query(m.AppUser)
                .filter(m.AppUser.role == m.ROLE_ADMIN)
                .order_by(m.AppUser.id)
                .all()
            )
            if not rows:
                print("暂无管理员账号")
            for u in rows:
                state = "启用" if u.active else "停用"
                print(f"  #{u.id:<4} {u.username:<20} {u.name:<12} {state}  上级别={u.approval_level}")
            return 0
        if args.reset_admin:
            return _reset_admin(db, args.user, args.password)
        if args.init:
            info = bootstrap()
            print(f"[init] {info}")
            return 0
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
