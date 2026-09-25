"""在线升级执行器（v2.9.0+）

跑在一个独立的短命容器里，由 app 容器通过 Docker Engine API 拉起。
它存在的唯一理由：切换 app 容器的动作会把 app 容器自己杀掉，
这件事只能由一个「不在 app 容器里」的进程来做。

流程：
  1. 备份数据库（在 db 容器里 pg_dump，再把文件取到升级卷）
  2. docker load 新镜像，并确认架构与本机一致
  3. 用骨架包更新宿主上的 compose / install.sh / deploy / docs
  4. 更新宿主 .env 的 IMAGE_TAG
  5. 克隆 app 容器的全部配置、只换镜像：停旧 → 改名留存 → 起新
  6. 健康检查；不通过就回滚（删新容器、旧容器改回原名启动）

每一步都写进共享卷的 status.json，前端轮询它即可显示进展。
"""
import io
import os
import shutil
import sys
import tarfile
import time

from . import docker_api as dk
from .upgrade_runner import append_log, write_status

TARGET_TAG = (os.getenv("UP_TARGET_TAG") or "").strip()
IMAGE_TAR = (os.getenv("UP_IMAGE_TAR") or "").strip()
BUNDLE_DIR = (os.getenv("UP_BUNDLE_DIR") or "").strip()
PROJECT_DIR = (os.getenv("UP_PROJECT_DIR") or "").strip()
APP_CONTAINER = (os.getenv("UP_APP_CONTAINER") or "").strip()
DB_CONTAINER = (os.getenv("UP_DB_CONTAINER") or "").strip()
UPGRADE_DIR = os.getenv("UPGRADE_DIR") or "/app/data/upgrade"
ARCH = (os.getenv("UP_ARCH") or "").strip()
NOTES = os.getenv("UP_NOTES") or ""

BACKUP_DIR = os.path.join(UPGRADE_DIR, "backups")
BUNDLE_WHITELIST = ("docker-compose.yml", "install.sh", "VERSION", ".env.example", "deploy", "docs")
HEALTH_TIMEOUT = 180
PROBE = ("import urllib.request,sys;"
         "sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)")


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _now_tag() -> str:
    return time.strftime("%H%M%S")


# ---------- 各步骤 ----------

def _find_app() -> dict:
    cand = dk.container_find(APP_CONTAINER) if APP_CONTAINER else None
    if cand:
        return cand
    # UP_APP_CONTAINER 传的是容器内 HOSTNAME：compose 未设 hostname 时它是
    # 12 位短容器 ID（不是容器名）——按名字找不到时按 ID 前缀回退
    if APP_CONTAINER:
        try:
            info = dk.container_inspect(APP_CONTAINER)
            cid = info.get("Id") or ""
            if cid:
                names = ["/%s" % (info.get("Name") or "").lstrip("/")]
                return {"Id": cid, "Names": names}
        except Exception:  # noqa: BLE001
            pass
    raise RuntimeError("找不到应用容器 %s" % (APP_CONTAINER or "(未传名字)"))


def _backup_db() -> str:
    if not DB_CONTAINER:
        append_log("没识别出数据库容器，跳过数据库备份")
        return ""
    cand = dk.container_find(DB_CONTAINER)
    if not cand:
        append_log("数据库容器 %s 不在运行，跳过备份" % DB_CONTAINER)
        return ""
    cid = cand.get("Id") or ""
    env = dk.container_env(cid)
    user = env.get("POSTGRES_USER") or "workbench"
    dbname = env.get("POSTGRES_DB") or "workbench"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    inner = "/tmp/reimburse-backup-%s.sql.gz" % stamp
    cmd = ["sh", "-c", "pg_dump -U %s -d %s | gzip > %s && ls -l %s"
           % (_shq(user), _shq(dbname), inner, inner)]
    code, out = dk.exec_run(cid, cmd, timeout=900)
    if code != 0:
        raise RuntimeError("数据库备份失败（exit %s）：%s" % (code, (out or "").strip()[:300]))

    blob = dk.get_archive(cid, inner, timeout=900)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, "pre-%s-%s.sql.gz" % (TARGET_TAG, stamp))
    _write_from_tar(blob, dest)
    try:
        dk.exec_run(cid, ["rm", "-f", inner], timeout=60)
    except Exception:  # noqa: BLE001
        pass
    append_log("数据库已备份：%s（%.1f MB）"
               % (os.path.basename(dest), os.path.getsize(dest) / 1048576.0))
    return dest


def _shq(text: str) -> str:
    return "'" + str(text).replace("'", "'\\''") + "'"


def _write_from_tar(blob: bytes, dest: str) -> None:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
        member = None
        for item in tf.getmembers():
            if item.isfile():
                member = item
                break
        if member is None:
            raise RuntimeError("从容器取出的归档里没有文件")
        src = tf.extractfile(member)
        if src is None:
            raise RuntimeError("无法读取归档内容")
        with open(dest, "wb") as fh:
            shutil.copyfileobj(src, fh)


def _load_image() -> str:
    if not IMAGE_TAR or not os.path.exists(IMAGE_TAR):
        raise RuntimeError("找不到镜像包：%s" % (IMAGE_TAR or "(未传路径)"))
    ref = "reimburse-workbench:%s" % TARGET_TAG
    append_log("载入镜像 %s（%.0f MB，约 1-3 分钟）" % (ref, os.path.getsize(IMAGE_TAR) / 1048576.0))
    dk.load_image(IMAGE_TAR, timeout=2400)
    if not dk.image_exists(ref):
        raise RuntimeError("镜像载入后仍然找不到 %s" % ref)
    got = dk.image_arch(ref)
    if ARCH and got and got != ARCH:
        raise RuntimeError("镜像架构 %s 与本机 %s 不一致（load 能成功但容器起不来）" % (got, ARCH))
    append_log("镜像就绪（架构 %s）" % (got or "未知"))
    return ref


def _apply_bundle() -> None:
    if not BUNDLE_DIR or not os.path.isdir(BUNDLE_DIR):
        append_log("没有骨架包，跳过 compose / install.sh 更新")
        return
    if not PROJECT_DIR or not os.path.isdir(PROJECT_DIR):
        append_log("拿不到宿主项目目录，跳过 compose / install.sh 更新")
        return
    updated = []
    for name in BUNDLE_WHITELIST:
        src = os.path.join(BUNDLE_DIR, name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(PROJECT_DIR, name)
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            updated.append(name)
        except Exception as exc:  # noqa: BLE001
            append_log("更新 %s 失败（跳过）：%s" % (name, exc))
    if updated:
        append_log("已更新宿主文件：%s" % "、".join(updated))


def _update_env() -> None:
    if not PROJECT_DIR:
        return
    path = os.path.join(PROJECT_DIR, ".env")
    if not os.path.exists(path):
        append_log("宿主上没有 .env，跳过 IMAGE_TAG 更新")
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except Exception as exc:  # noqa: BLE001
        append_log("读 .env 失败（跳过）：%s" % exc)
        return
    hit = False
    for i, line in enumerate(lines):
        if line.startswith("IMAGE_TAG="):
            lines[i] = "IMAGE_TAG=%s" % TARGET_TAG
            hit = True
    if not hit:
        lines.append("IMAGE_TAG=%s" % TARGET_TAG)
    tmp = path + ".upgrade-new"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    shutil.move(tmp, path)
    append_log("宿主 .env 的 IMAGE_TAG → %s" % TARGET_TAG)


def _clone_body(info: dict, ref: str) -> dict:
    """薄包装：真正的字段裁剪在 docker_api.clone_container_body（纯 stdlib，
    方便在宿主侧直接演练这条链路）。"""
    return dk.clone_container_body(info, ref)


def _rollback(prev_id: str, name: str, new_id: str) -> bool:
    """把旧容器恢复成正式服务。"""
    if new_id:
        try:
            dk.container_stop(new_id, timeout_sec=10)
        except Exception:  # noqa: BLE001
            pass
        try:
            dk.container_remove(new_id, force=True)
        except Exception as exc:  # noqa: BLE001
            append_log("清理新容器时出错：%s" % exc)
    try:
        dk.container_rename(prev_id, name)
        dk.container_start(prev_id)
        append_log("已回滚：%s 用旧镜像重新启动" % name)
        return True
    except Exception as exc:  # noqa: BLE001
        append_log("回滚失败，需要人工介入（容器名可能是 %s-*）：%s" % (name, exc))
        return False


def _wait_healthy(cid: str, timeout: int = HEALTH_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    tries = 0
    while time.time() < deadline:
        tries += 1
        state = dk.container_health(cid)
        if state in ("unhealthy", "stopped", "gone"):
            append_log("新容器状态异常：%s" % state)
            return False
        try:
            code, _out = dk.exec_run(cid, ["python", "-c", PROBE], timeout=25)
            if code == 0:
                append_log("健康检查通过（第 %d 次探测）" % tries)
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    append_log("等待 %d 秒仍未通过健康检查" % timeout)
    return False


def _swap(app_id: str) -> None:
    ref = "reimburse-workbench:%s" % TARGET_TAG
    info = dk.container_inspect(app_id)
    name = (info.get("Name") or "").strip("/") or APP_CONTAINER
    old_image = (info.get("Config") or {}).get("Image") or ""
    body = _clone_body(info, ref)

    append_log("停止旧容器 %s（当前镜像 %s）" % (name, old_image))
    dk.container_stop(app_id, timeout_sec=30)
    prev_name = "%s-prev-%s" % (name, _now_tag())
    dk.container_rename(app_id, prev_name)

    write_status(phase="switching", progress=0.99, message="正在启动新版本容器…")
    append_log("用 %s 创建新容器 %s" % (ref, name))
    new_id = dk.container_create(name, body)
    try:
        dk.container_start(new_id)
    except Exception as exc:  # noqa: BLE001
        append_log("新容器启动失败：%s" % exc)
        _rollback(app_id, name, new_id)
        raise RuntimeError("新版本容器无法启动，已回滚到 %s" % old_image)

    if _wait_healthy(new_id):
        try:
            dk.container_remove(app_id, force=True)
        except Exception:  # noqa: BLE001
            pass
        # 注意：不能在这里删 reimburse-upgrader —— 那就是本进程自己。
        # 残留的执行器容器由新起来的 app 在启动时清理。
        append_log("升级完成：%s 现在运行 %s" % (name, ref))
        write_status(phase="done", ok=True, progress=1.0,
                     message="升级完成，已运行 %s" % TARGET_TAG, detail=NOTES,
                     finished_at=_stamp())
        return

    append_log("新版本未通过健康检查，开始回滚")
    rolled = _rollback(app_id, name, new_id)
    if rolled:
        raise RuntimeError("新版本健康检查未通过，已回滚到 %s" % old_image)
    raise RuntimeError("新版本健康检查未通过，且回滚失败——请人工检查容器 %s-*" % name)


# ---------- 入口 ----------

def main() -> int:
    write_status(phase="switching", progress=0.95, message="升级执行器启动中…", started_at=_stamp())
    try:
        if not TARGET_TAG:
            raise RuntimeError("缺少目标版本号")
        append_log("执行器启动：目标 %s，架构 %s" % (TARGET_TAG, ARCH or "?"))
        app = _find_app()
        append_log("应用容器：%s（%s）" % (app.get("Names") or APP_CONTAINER, (app.get("Id") or "")[:12]))

        _backup_db()
        _load_image()
        _apply_bundle()
        _update_env()
        _swap(app.get("Id") or "")
        return 0
    except Exception as exc:  # noqa: BLE001
        append_log("升级失败：%s" % exc)
        write_status(phase="failed", ok=False, message="升级失败", detail=str(exc), finished_at=_stamp())
        return 1


if __name__ == "__main__":
    sys.exit(main())
