#!/usr/bin/env python3
"""在线升级链路演练（宿主侧运行，不需要容器权限）

为什么要在宿主侧跑：
    macOS 的 Docker Desktop 默认拒绝「容器通过挂载的 docker.sock 访问 daemon」
    （Linux 服务器没有这个限制）。所以本机没法在容器里验证升级链路，
    改成在宿主上用同一份 docker_api 代码直连 socket 演练一遍——
    验的是同一段逻辑，只是换了发起方。

它做四件事，全程不碰生产容器（临时容器另起一个名字、另用一个端口）：
    1. socket 连通性 + 关键容器发现
    2. 数据库备份链路：在 db 容器里 pg_dump → 把文件取回本地 → 校验是合法 gzip
    3. 克隆重建参数：断言 Config/HostConfig 的取舍正确
    4. 真实重建演练：起一个临时容器（同镜像、换端口）→ 等健康检查 → 删掉

用法：
    python3 tools/upgrade_dryrun.py                 # 默认演练 reimburse-app
    python3 tools/upgrade_dryrun.py --container X --port 18080
"""
import argparse
import gzip
import io
import pathlib
import sys
import tarfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import docker_api as dk  # noqa: E402

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


def group(title):
    print(f"\n\033[1m{title}\033[0m")


def step_socket():
    group("1. Docker 连通性")
    ok, hint = dk.available()
    check("socket 可用", ok, hint)
    if not ok:
        return False
    v = dk.version_info()
    print(f"    Engine {v.get('Version')}（API {v.get('ApiVersion')}），"
          f"socket {dk.socket_path()}")
    return True


def step_discover(app_name, db_name):
    group("2. 容器发现")
    app = dk.container_find(app_name)
    check(f"找到应用容器 {app_name}", bool(app), "（名字不对？用 --container 指定）")
    db = dk.container_find(db_name)
    check(f"找到数据库容器 {db_name}", bool(db))
    return app, db


def step_backup(db):
    group("3. 数据库备份链路（exec + archive）")
    if not db:
        check("跳过（没有 db 容器）", False)
        return
    cid = db["Id"]
    env = dk.container_env(cid)
    user = env.get("POSTGRES_USER") or "workbench"
    name = env.get("POSTGRES_DB") or "workbench"
    inner = "/tmp/dryrun-backup.sql.gz"
    cmd = ["sh", "-c", f"pg_dump -U {user} -d {name} | gzip > {inner} && ls -l {inner}"]
    code, out = dk.exec_run(cid, cmd, timeout=600)
    check("容器内 pg_dump 执行成功", code == 0, (out or "").strip()[:200])
    if code != 0:
        return
    blob = dk.get_archive(cid, inner, timeout=600)
    check("从容器取回归档", len(blob) > 0, f"{len(blob)} 字节")
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        check("归档里有一个文件", len(members) == 1, str([m.name for m in members]))
        data = tf.extractfile(members[0]).read()
    check("取回的内容是 gzip", data[:2] == b"\x1f\x8b", str(data[:4]))
    try:
        raw = gzip.decompress(data)
        check("能解压出 SQL 正文", b"PostgreSQL database dump" in raw or len(raw) > 100,
              f"解压后 {len(raw)} 字节")
    except Exception as exc:  # noqa: BLE001
        check("能解压出 SQL 正文", False, str(exc))
    dk.exec_run(cid, ["rm", "-f", inner], timeout=60)
    print(f"    备份大小 {len(data) / 1024:.0f} KB")


def step_clone_body(app, image):
    group("4. 克隆重建参数")
    info = dk.container_inspect(app["Id"])
    body = dk.clone_container_body(info, image)
    old_name = (info.get("Name") or "").strip("/")
    check("镜像被换成目标版本", body["Image"] == image, body["Image"])
    check("环境变量保留（含数据库连接串）", any("DATABASE_URL" in e for e in body["Env"]))
    check("端口映射保留", bool(body["HostConfig"].get("PortBindings")))
    check("数据卷保留", bool(body["HostConfig"].get("Binds")))
    check("主机名不继承", "Hostname" not in body)
    check("compose 标签保留",
          bool((body.get("Labels") or {}).get("com.docker.compose.project")),
          str(list((body.get("Labels") or {}).keys()))[:120])
    check("没有 None 字段", all(v is not None for v in body.values()))
    print(f"    克隆自 {old_name}，容器配置项 {len(body)} 个")
    return body


def step_rehydrate(app, body, port, image):
    group("5. 真实重建演练（临时容器，不碰生产）")
    tmp_name = "reimburse-upgrade-dryrun"
    old = dk.container_find(tmp_name)
    if old:
        dk.container_remove(old["Id"], force=True)

    body = dict(body)
    body["Env"] = [e for e in body["Env"] if not e.startswith("AUTO_MIGRATE=")]
    body["Env"].append("AUTO_MIGRATE=0")     # 演练不碰数据库结构
    host = dict(body["HostConfig"])
    host["PortBindings"] = {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}
    body["HostConfig"] = host
    body["Labels"] = dict(body.get("Labels") or {})
    body["Labels"]["dryrun"] = "1"

    cid = dk.container_create(tmp_name, body)
    check("临时容器创建成功", bool(cid), cid[:12])
    dk.container_start(cid)
    check("临时容器启动成功", dk.container_health(cid) != "stopped")

    probe = ("import urllib.request,sys;sys.exit(0 if urllib.request.urlopen("
             "'http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)")
    healthy = False
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            code, _ = dk.exec_run(cid, ["python", "-c", probe], timeout=25)
            if code == 0:
                healthy = True
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    check("临时容器通过健康检查（=升级后能起来的证据）", healthy)

    if healthy:
        code, out = dk.exec_run(cid, ["python", "-c",
                                      "import json,urllib.request;"
                                      "print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health'))['version'])"],
                                timeout=30)
        got = (out or "").strip().splitlines()[-1] if out else ""
        check("临时容器报告版本正确", got == image.split(":")[-1], f"报告 {got!r}")

    dk.container_stop(cid, timeout_sec=10)
    dk.container_remove(cid, force=True)
    check("临时容器已清理", dk.container_find(tmp_name) is None)
    print(f"    生产容器 {app['Id'][:12]} 全程未被动过")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="reimburse-app")
    ap.add_argument("--db", default="reimburse-db")
    ap.add_argument("--image", default="", help="默认用应用容器当前的镜像")
    ap.add_argument("--port", type=int, default=18080)
    args = ap.parse_args()

    print(f"\033[1m在线升级链路演练\033[0m（socket: {dk.socket_path()}）")
    if not step_socket():
        print("\n\033[31mDocker 不可用，演练中止\033[0m")
        return 1
    app, db = step_discover(args.container, args.db)
    if not app:
        return 1
    image = args.image or dk.container_image(app["Id"])
    print(f"    应用容器镜像：{image}")
    step_backup(db)
    body = step_clone_body(app, image)
    step_rehydrate(app, body, args.port, image)

    print(f"\n结果：\033[32m{OK} 通过\033[0m / \033[31m{FAIL} 失败\033[0m")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
