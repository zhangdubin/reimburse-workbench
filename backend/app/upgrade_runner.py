"""在线升级：下载、校验、把「切换动作」交给独立 worker 容器（v2.9.0+）

为什么切换动作不能在本容器里做：
    重建 app 容器时，compose 会先停掉旧的——也就是当前这个容器自己。
    进程被一起干掉，后面的健康检查与回滚就没人执行了，会留下一个半成品。

所以分两段：
    1) app 容器（本模块）：下载 + sha256 校验 + 解包到升级卷，然后通过
       Docker Engine API 起一个 detached 的 worker 容器，立刻返回。
    2) worker 容器（upgrade_worker.py）：镜像载入、骨架文件覆盖、按原配置
       克隆重建 app 容器、健康检查、失败回滚。它不受 app 容器重建影响。

两边通过升级卷里的 status.json 交换进展：worker 写，app 读给前端看。
"""
import hashlib
import json
import os
import platform
import shutil
import threading
import time

from . import docker_api as dk
from . import upgrade_client as uc

UPGRADE_DIR = os.getenv("UPGRADE_DIR", "/app/data/upgrade")
PKG_DIR = os.path.join(UPGRADE_DIR, "pkg")
BUNDLE_DIR = os.path.join(UPGRADE_DIR, "bundle")
STATUS_PATH = os.path.join(UPGRADE_DIR, "status.json")
LOG_MAX = 240

_lock = threading.Lock()


# ---------- 状态文件 ----------

def _default_status() -> dict:
    return {
        "phase": "idle",
        "ok": None,
        "from_version": uc.current_version(),
        "target_version": "",
        "progress": 0.0,
        "message": "",
        "detail": "",
        "started_at": "",
        "updated_at": "",
        "finished_at": "",
        "operator": "",
        "log": [],
    }


def read_status() -> dict:
    data = _default_status()
    try:
        with open(STATUS_PATH, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            data.update(saved)
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass
    # 本容器版本已经等于目标版本，说明升级确实成功了
    if data.get("phase") == "switching" and uc.current_version() == data.get("target_version"):
        data["phase"] = "done"
        data["ok"] = True
        data["message"] = "升级完成"
    data["current_version"] = uc.current_version()
    data["docker_ready"], data["docker_hint"] = dk.available()
    return data


def write_status(**kw) -> dict:
    """原子写：先写临时文件再 rename，避免前端读到半个 JSON。"""
    with _lock:
        data = _default_status()
        try:
            with open(STATUS_PATH, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                data.update(saved)
        except Exception:  # noqa: BLE001
            pass
        data.update(kw)
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(UPGRADE_DIR, exist_ok=True)
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATUS_PATH)
        return data


def append_log(message: str) -> None:
    """把一行进展同时打到容器日志与 status.log（前端「升级详情」看的就是它）。"""
    stamp = time.strftime("%H:%M:%S")
    line = "[%s] %s" % (stamp, message)
    print("[upgrade] " + message, flush=True)
    with _lock:
        data = _default_status()
        try:
            with open(STATUS_PATH, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                data.update(saved)
        except Exception:  # noqa: BLE001
            pass
        log = list(data.get("log") or [])
        log.append(line)
        data["log"] = log[-LOG_MAX:]
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(UPGRADE_DIR, exist_ok=True)
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATUS_PATH)


def busy() -> bool:
    phase = (read_status().get("phase") or "idle")
    return phase in ("downloading", "verifying", "extracting", "switching")


# ---------- 前置检查 ----------

def host_arch() -> str:
    machine = (platform.machine() or "").lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine.startswith("armv7"):
        return "armv7"
    return machine


def preflight(db=None) -> list:
    """返回阻塞项列表（空 = 可以升级）。"""
    problems = []
    ok, hint = dk.available()
    if not ok:
        problems.append("容器拿不到 Docker：%s（升级需要在 compose 里挂载 DOCKER_SOCK）" % hint)
    cfg_ok, cfg_msg = uc.configured(db)
    if not cfg_ok:
        problems.append(cfg_msg)
    if busy():
        problems.append("已有一次升级正在进行中")
    if not os.access(UPGRADE_DIR, os.W_OK):
        problems.append("升级目录不可写：%s" % UPGRADE_DIR)
    return problems


# ---------- 从自身容器推断挂载 ----------

def _self_container() -> dict | None:
    me = os.getenv("HOSTNAME") or ""
    if not me:
        return None
    return dk.container_find(me) or None


def _mount_source(container: dict, destination: str) -> str:
    """找到挂到指定容器内路径的宿主机来源（卷名或宿主绝对路径）。"""
    info = dk.container_inspect(container.get("Id") or "")
    for mount in info.get("Mounts") or []:
        if (mount.get("Destination") or "").rstrip("/") == destination.rstrip("/"):
            return mount.get("Name") or mount.get("Source") or ""
    return ""


def _labels(container: dict) -> dict:
    info = dk.container_inspect(container.get("Id") or "")
    return (info.get("Config") or {}).get("Labels") or {}


# ---------- 下载与校验 ----------

def _sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: str, cfg: dict, expect_size: int = 0, label: str = "") -> str:
    """流式下载 + 边下边算 sha256，返回十六进制摘要。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    h = hashlib.sha256()
    done = 0
    started = time.time()
    with uc.open_asset_stream(url, cfg, timeout=120) as resp, open(tmp, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0) or expect_size
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if total:
                pct = done * 100.0 / total
                # 每 5% 或首次写一次状态，别把 status.json 写爆
                if pct - float(read_status().get("progress") or 0) * 100 >= 5:
                    speed = done / max(time.time() - started, 0.1) / 1048576.0
                    write_status(progress=round(done / total, 4),
                                 message="正在下载 %s %.0f%%（%.1f MB/s）" % (label or "", pct, speed))
    os.replace(tmp, dest)
    return h.hexdigest()


def _download_and_verify(asset: dict, dest: str, cfg: dict, label: str, expect_sha: str) -> str:
    sha = _download(asset.get("url") or "", dest, cfg,
                    expect_size=int(asset.get("size") or 0), label=label)
    if expect_sha and sha.lower() != expect_sha.lower():
        raise RuntimeError("%s 校验失败：期望 %s… 实际 %s…（文件可能损坏或清单不匹配）"
                           % (label, expect_sha[:12], sha[:12]))
    append_log("%s 下载完成并校验通过（%.1f MB）" % (label, os.path.getsize(dest) / 1048576.0))
    return sha


# ---------- 启动 worker 容器 ----------

def _start_worker(self_c: dict, target_tag: str, image_tar: str, bundle_ready: bool,
                  notes: str) -> str:
    """用当前镜像起一个 detached worker：它来做真正的切换，不受 app 重建影响。"""
    my_image = dk.container_image(self_c.get("Id") or "")
    if not my_image:
        raise RuntimeError("读不到当前容器镜像名")

    sock_source = _mount_source(self_c, "/var/run/docker.sock")
    if not sock_source:
        raise RuntimeError("当前容器没有挂载 Docker socket：请在 compose 的 app 服务加上 "
                           "DOCKER_SOCK 挂载后重装一次（本次升级之后才能在线升级）")
    upgrade_volume = _mount_source(self_c, UPGRADE_DIR)
    if not upgrade_volume:
        raise RuntimeError("当前容器没有挂载升级卷（%s）" % UPGRADE_DIR)

    labels = _labels(self_c)
    project_dir = (os.getenv("HOST_PROJECT_DIR") or "").strip()
    binds = ["%s:/var/run/docker.sock" % sock_source, "%s:%s" % (upgrade_volume, UPGRADE_DIR)]
    if project_dir and os.path.isdir(project_dir):
        binds.append("%s:%s" % (project_dir, project_dir))

    env = {
        "PATH": "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": "/app/backend",
        "TZ": os.getenv("TZ", "Asia/Shanghai"),
        "DOCKER_SOCK": "/var/run/docker.sock",
        "UPGRADE_DIR": UPGRADE_DIR,
        "UP_TARGET_TAG": target_tag,
        "UP_IMAGE_TAR": image_tar,
        "UP_BUNDLE_DIR": BUNDLE_DIR if bundle_ready else "",
        "UP_PROJECT_DIR": project_dir,
        "UP_APP_CONTAINER": os.getenv("HOSTNAME", ""),
        "UP_COMPOSE_PROJECT": labels.get("com.docker.compose.project") or "",
        "UP_COMPOSE_SERVICE": labels.get("com.docker.compose.service") or "app",
        "UP_DB_CONTAINER": _guess_db_container(labels),
        "UP_NOTES": (notes or "")[:400],
        "UP_ARCH": host_arch(),
        "UP_REQUIRE_HEALTH": "1",
    }
    body = {
        "Image": my_image,
        "Cmd": ["python", "-m", "app.upgrade_worker"],
        "Env": ["%s=%s" % (k, v) for k, v in env.items()],
        "Labels": {
            "reimburse.upgrade": "worker",
            "reimburse.upgrade.target": target_tag,
        },
        "HostConfig": {
            "Binds": binds,
            "AutoRemove": False,        # 保留容器便于事后看日志，成功时由 app 侧清理
            "RestartPolicy": {"Name": "no"},
            "SecurityOpt": ["seccomp=unconfined"],
        },
    }
    cleanup_worker()       # 上一轮可能留下同名容器（失败排查用），先清掉
    cid = dk.container_create("reimburse-upgrader", body)
    dk.container_start(cid)
    return cid

def _guess_db_container(labels: dict) -> str:
    """从网络里的兄弟容器猜 db 容器名（优先 compose 同项目的 db 服务）。"""
    project = labels.get("com.docker.compose.project") or ""
    try:
        data = json.loads(dk.request("GET", "/containers/json", timeout=15).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    for item in data or []:
        lb = item.get("Labels") or {}
        if lb.get("com.docker.compose.project") != project:
            continue
        if lb.get("com.docker.compose.service") == "db":
            for n in item.get("Names") or []:
                return n.strip("/")
    return ""


# ---------- 主入口 ----------

def start_upgrade(db, version: str, operator: str = "") -> dict:
    """下载并交接给 worker。立即返回，进展看 status。"""
    problems = preflight(db)
    if problems:
        return {"ok": False, "error": "；".join(problems)}

    rel = uc.fetch_release(db)
    if not rel.get("ok"):
        return {"ok": False, "error": rel.get("error") or "读取 Release 失败"}
    if version and uc.ver_tuple(version) != uc.ver_tuple(rel.get("version") or ""):
        rel = uc.fetch_release(db)
    target = (rel.get("version") or "").strip()
    if not target:
        return {"ok": False, "error": "Release 里没有可识别的版本号"}
    if not uc.is_newer(target, uc.current_version()):
        return {"ok": False, "error": "目标版本 %s 不比当前版本 %s 新" % (target, uc.current_version())}

    manifest = rel.get("manifest")
    arch = host_arch()
    asset = uc.platform_asset(manifest, arch) if manifest else None
    if manifest and not asset:
        return {"ok": False, "error": "manifest 里没有 %s 架构的镜像包" % arch}
    if asset:
        # manifest 里的 images.<arch> 项只有 name/sha256/size，没有下载地址；
        # 真正的 URL 在 Release assets 里，按文件名对上（对不上=Release 与 manifest 不一致）
        rel_asset = (rel.get("assets") or {}).get(asset.get("name") or "")
        if not rel_asset or not rel_asset.get("url"):
            return {"ok": False, "error": "Release 资产里找不到 %s 的下载地址（manifest 与 Release 不一致？）"
                     % (asset.get("name") or "?")}
        asset = dict(asset)
        asset["url"] = rel_asset["url"]
    if not manifest:
        if uc.require_manifest(db):
            return {"ok": False, "error": "Release 缺少 manifest.json，无法校验完整性；"
                                          "如需强行升级请在配置里关掉「必须校验清单」"}
        for name, item in (rel.get("assets") or {}).items():
            if name.endswith("-linux-%s.tar.gz" % arch):
                asset = item
                break
        if not asset:
            return {"ok": False, "error": "Release 里找不到 %s 架构的镜像包" % arch}

    bundle_asset = (rel.get("assets") or {}).get("reimburse-workbench-%s-app.tar.gz" % target)

    write_status(phase="downloading", ok=None, target_version=target, progress=0.0,
                 message="准备下载 %s" % target, detail=rel.get("notes") or "",
                 started_at=time.strftime("%Y-%m-%d %H:%M:%S"), finished_at="",
                 from_version=uc.current_version(), operator=operator or "", log=[])
    append_log("目标版本 %s（当前 %s，架构 %s）" % (target, uc.current_version(), arch))

    t = threading.Thread(
        target=_worker_thread,
        args=(db, rel, target, asset, bundle_asset, arch),
        name="upgrade-downloader", daemon=True,
    )
    t.start()
    return {"ok": True, "status": "started", "target_version": target,
            "message": "已开始下载升级包，可在本页查看进度"}


def _worker_thread(db, rel: dict, target: str, asset: dict, bundle_asset: dict, arch: str) -> None:
    try:
        cfg = uc._cfg(db)
        os.makedirs(PKG_DIR, exist_ok=True)
        image_tar = os.path.join(PKG_DIR, "reimburse-workbench-%s-linux-%s.tar.gz" % (target, arch))
        expect = (asset.get("sha256") or asset.get("digest") or "").strip()
        _download_and_verify(asset, image_tar, cfg, "镜像包", expect)

        bundle_ready = False
        if bundle_asset and bundle_asset.get("url"):
            write_status(phase="verifying", progress=0.6, message="正在下载安装骨架包")
            bundle_tar = os.path.join(PKG_DIR, "app-bundle.tar.gz")
            bexp = (bundle_asset.get("sha256") or bundle_asset.get("digest") or "").strip()
            _download_and_verify(bundle_asset, bundle_tar, cfg, "骨架包", bexp)
            write_status(phase="extracting", progress=0.8, message="正在解包安装骨架")
            if os.path.isdir(BUNDLE_DIR):
                shutil.rmtree(BUNDLE_DIR, ignore_errors=True)
            os.makedirs(BUNDLE_DIR, exist_ok=True)
            import tarfile
            with tarfile.open(bundle_tar, "r:gz") as tf:
                _safe_extract(tf, BUNDLE_DIR)
            bundle_ready = True
            append_log("骨架包已解包（compose / install.sh / deploy）")
        else:
            append_log("该 Release 没有骨架包，跳过 compose / install.sh 的更新")

        self_c = _self_container()
        if not self_c:
            raise RuntimeError("找不到自身容器（HOSTNAME 与容器名不一致？）")

        write_status(phase="switching", progress=0.95, message="正在交接给升级执行器")
        cid = _start_worker(self_c, target, image_tar, bundle_ready, rel.get("notes") or "")
        append_log("升级执行器已启动（%s），即将重建应用容器…" % cid[:12])
        write_status(phase="switching", progress=0.98,
                     message="正在切换容器（页面会短暂无法访问，稍后自动恢复）")
    except Exception as exc:  # noqa: BLE001
        append_log("升级中断：%s" % exc)
        write_status(phase="failed", ok=False, message="升级未完成", detail=str(exc),
                     finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))


def _safe_extract(tf, dest: str) -> None:
    """防目录穿越：只解到 dest 里面。"""
    base = os.path.realpath(dest)
    for member in tf.getmembers():
        target = os.path.realpath(os.path.join(dest, member.name))
        if not (target == base or target.startswith(base + os.sep)):
            raise RuntimeError("骨架包里有越界路径：%s" % member.name)
    tf.extractall(dest)


def cleanup_worker() -> None:
    """升级结束后把执行器容器收掉（它自己是 AutoRemove=False 的）。"""
    try:
        c = dk.container_find("reimburse-upgrader")
        if c:
            dk.container_remove(c.get("Id") or "", force=True)
    except Exception:  # noqa: BLE001
        pass
