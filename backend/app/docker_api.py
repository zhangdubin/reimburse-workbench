"""极简 Docker Engine API 客户端（纯 stdlib，v2.9.0+）

在线升级要在容器里做四件事：载入新镜像、按原配置克隆重建 app 容器、
在 db 容器里跑 pg_dump、读容器健康状态。全部走 Docker Engine 的
Unix socket HTTP 接口实现——不需要在镜像里塞 docker CLI（省 100MB+），
也不用引入 docker-py（本项目一贯不引第三方运行时依赖）。

API 版本固定 v1.41：本项目的已知最老引擎是 Docker 20.10.8，v1.41 正是
20.10 的 API 版本，更新的引擎都向下兼容它。想覆盖可用 DOCKER_API_VERSION。

连接地址取 DOCKER_SOCK，默认 /var/run/docker.sock。
"""
import http.client
import json
import os
import socket
from typing import Any

DEFAULT_SOCK = "/var/run/docker.sock"
API_VERSION = (os.getenv("DOCKER_API_VERSION") or "v1.41").strip() or "v1.41"


class DockerError(RuntimeError):
    """Engine 返回了非 2xx。"""

    def __init__(self, status: int, body: str, path: str = ""):
        super().__init__("Docker API %s %s: %s" % (status, path, (body or "")[:400]))
        self.status = status
        self.body = body


def socket_path() -> str:
    return (os.getenv("DOCKER_SOCK") or "").strip() or DEFAULT_SOCK


class _UnixConnection(http.client.HTTPConnection):
    """把 HTTP 连接建到 Unix socket 上（HTTPConnection 默认只支持 TCP）。"""

    def __init__(self, sock_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self):  # noqa: D401
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._sock_path)
        self.sock = s


def _conn(timeout: float) -> _UnixConnection:
    return _UnixConnection(socket_path(), timeout)


def request(method: str, path: str, body: Any = None, timeout: float = 30.0,
            raw: bytes = None, content_type: str = "application/json") -> bytes:
    """发一个请求并返回响应体。path 不带 /vX.Y 前缀，这里统一补上。"""
    # /_ping 是未版本化端点，带上 /v1.x 反而 400
    if not path.startswith("/v") and not path.startswith("/_"):
        path = "/" + API_VERSION + path
    c = _conn(timeout)
    try:
        if raw is not None:
            c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            c.putheader("Host", "localhost")
            c.putheader("Content-Type", content_type)
            c.putheader("Content-Length", str(len(raw)))
            c.endheaders()
            c.send(raw)
        else:
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
                c.putheader("Host", "localhost")
                c.putheader("Content-Type", content_type)
                c.putheader("Content-Length", str(len(payload)))
                c.endheaders()
                c.send(payload)
            else:
                c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
                c.putheader("Host", "localhost")
                c.endheaders()
        resp = c.getresponse()
        data = resp.read()
        if resp.status >= 400:
            raise DockerError(resp.status, data.decode("utf-8", "replace"), path)
        return data
    finally:
        try:
            c.close()
        except Exception:
            pass


def available() -> tuple[bool, str]:
    """升级前置检查：socket 通不通。返回 (是否可用, 说明)。"""
    p = socket_path()
    if not os.path.exists(p):
        return False, "找不到 Docker socket：%s" % p
    if os.path.isdir(p):
        # 挂载不存在的宿主路径时 Docker 会建出同名目录
        return False, "%s 是目录（宿主上没有这个 socket，请检查 .env 的 DOCKER_SOCK）" % p
    try:
        request("GET", "/_ping", timeout=5)
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, "连接 Docker 失败：%s" % exc


def version_info() -> dict:
    try:
        return json.loads(request("GET", "/version", timeout=10).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# ---------- 镜像 ----------

def image_exists(ref: str) -> bool:
    q = json.dumps({"reference": [ref]})
    try:
        data = json.loads(request("GET", "/images/json?filters=%s" % _q(q), timeout=15).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return False
    return bool(data)


def image_arch(ref: str) -> str:
    try:
        data = json.loads(request("GET", "/images/%s/json" % _path(ref), timeout=15).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    return (data.get("Architecture") or "").strip()


def load_image(tar_path: str, timeout: float = 1800.0) -> str:
    """流式 POST /images/load 上传镜像 tar（支持 .gz，Engine 自己识别）。

    368MB 的包在这一步会走 1~3 分钟，所以超时给得很宽。
    """
    size = os.path.getsize(tar_path)
    c = _conn(timeout)
    try:
        c.putrequest("POST", "/%s/images/load" % API_VERSION, skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", "localhost")
        c.putheader("Content-Type", "application/x-tar")
        c.putheader("Content-Length", str(size))
        c.endheaders()
        with open(tar_path, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                c.send(chunk)
        resp = c.getresponse()
        data = resp.read().decode("utf-8", "replace")
        if resp.status >= 400:
            raise DockerError(resp.status, data, "/images/load")
        return data
    finally:
        try:
            c.close()
        except Exception:
            pass


# ---------- 容器 ----------

def container_find(name: str) -> dict | None:
    """按名字找容器（精确匹配，排除 compose 生成的别名干扰）。"""
    q = json.dumps({"name": [name]})
    data = json.loads(request("GET", "/containers/json?all=1&filters=%s" % _q(q), timeout=15).decode("utf-8"))
    for item in data or []:
        for n in item.get("Names") or []:
            if n.strip("/") == name:
                return item
    return None


def container_inspect(cid: str) -> dict:
    return json.loads(request("GET", "/containers/%s/json" % cid, timeout=20).decode("utf-8"))


def container_rename(cid: str, new_name: str) -> None:
    request("POST", "/containers/%s/rename?name=%s" % (cid, _q1(new_name)), timeout=20)


def container_create(name: str, body: dict) -> str:
    data = json.loads(request("POST", "/containers/create?name=%s" % _q1(name), body=body, timeout=30).decode("utf-8"))
    cid = data.get("Id") or ""
    if not cid:
        raise DockerError(500, "创建容器没有返回 Id：%s" % data, "/containers/create")
    return cid


def container_start(cid: str) -> None:
    request("POST", "/containers/%s/start" % cid, timeout=60)


def container_stop(cid: str, timeout_sec: int = 20) -> None:
    request("POST", "/containers/%s/stop?t=%d" % (cid, timeout_sec), timeout=timeout_sec + 30)


def container_remove(cid: str, force: bool = True) -> None:
    request("DELETE", "/containers/%s?force=%d&v=0" % (cid, 1 if force else 0), timeout=60)


def container_health(cid: str) -> str:
    """返回 healthy / unhealthy / starting；容器没定义 HEALTHCHECK 时返回空串。"""
    try:
        info = container_inspect(cid)
    except Exception:  # noqa: BLE001
        return "gone"
    state = info.get("State") or {}
    if not state.get("Running"):
        return "stopped"
    health = state.get("Health") or {}
    return (health.get("Status") or "").strip()


def exec_run(cid: str, cmd: list, timeout: float = 600.0) -> tuple[int, str]:
    """在容器里跑一条命令并等它结束，返回 (退出码, 输出)。

    非 TTY 模式下 Engine 用「8 字节帧头 + 载荷」的多路复用流返回 stdout/stderr，
    这里把帧头剥掉拼成一整段文本——升级时要用它跑 pg_dump，得看得到报错。
    """
    created = json.loads(request("POST", "/containers/%s/exec" % cid, body={
        "AttachStdout": True, "AttachStderr": True, "Tty": False, "Cmd": cmd,
    }, timeout=20).decode("utf-8"))
    eid = created.get("Id") or ""
    if not eid:
        raise DockerError(500, "创建 exec 失败：%s" % created, "/containers/exec")
    c = _conn(timeout)
    try:
        payload = json.dumps({"Detach": False, "Tty": False}).encode("utf-8")
        c.putrequest("POST", "/%s/exec/%s/start" % (API_VERSION, eid), skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", "localhost")
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", str(len(payload)))
        c.endheaders()
        c.send(payload)
        resp = c.getresponse()
        blob = resp.read()
    finally:
        try:
            c.close()
        except Exception:
            pass

    out = []
    i = 0
    while i + 8 <= len(blob):
        size = int.from_bytes(blob[i + 4:i + 8], "big")
        frame = blob[i + 8:i + 8 + size]
        out.append(frame.decode("utf-8", "replace"))
        i += 8 + size
    if i == 0 and blob:      # 不是多路复用流（理论上不会走到），原样返回
        out.append(blob.decode("utf-8", "replace"))

    info = json.loads(request("GET", "/exec/%s/json" % eid, timeout=20).decode("utf-8"))
    return int(info.get("ExitCode") or 0), "".join(out)


def get_archive(cid: str, path: str, timeout: float = 300.0) -> bytes:
    """把容器里的一个文件/目录拷出来（返回 tar 字节流）。

    升级前的数据库备份走这条路：pg_dump 在 db 容器里生成 .sql.gz，
    再从这里取出来落到升级卷上——不用给 db 容器额外挂备份目录。
    """
    from urllib.parse import quote
    return request("GET", "/containers/%s/archive?path=%s" % (cid, quote(path, safe="")),
                   timeout=timeout)


def clone_container_body(info: dict, image: str) -> dict:
    """按既有容器的配置克隆一份「创建容器」参数，只把镜像换成新版。

    直接回传 inspect 出来的 Config / HostConfig 是最省事的做法，但里面有些
    字段带着旧容器的痕迹，必须剔除：
      - Hostname / Domainname：旧容器 ID，拷过去会导致网络标识错乱
      - AutoRemove：升级容器不能自删（出问题还得进去看）
    其余（环境变量、端口、数据卷、网络、seccomp、compose 标签）原样保留——
    尤其是 compose 标签，留着 compose 才认得出这个容器。
    """
    cfg = info.get("Config") or {}
    host = dict(info.get("HostConfig") or {})
    body = {
        "Image": image,
        "Env": cfg.get("Env") or [],
        "Cmd": cfg.get("Cmd"),
        "Entrypoint": cfg.get("Entrypoint"),
        "WorkingDir": cfg.get("WorkingDir") or "",
        "User": cfg.get("User") or "",
        "Labels": cfg.get("Labels") or {},
        "ExposedPorts": cfg.get("ExposedPorts") or {},
        "Healthcheck": cfg.get("Healthcheck"),
        "StopSignal": cfg.get("StopSignal") or "",
        "AttachStdout": False,
        "AttachStderr": False,
        "Tty": False,
        "OpenStdin": False,
        "StdinOnce": False,
        "HostConfig": host,
    }
    for key in ("AutoRemove", "ContainerIDFile", "CgroupParent", "Cgroup"):
        host.pop(key, None)
    host["AutoRemove"] = False
    return {k: v for k, v in body.items() if v is not None}


def container_env(cid: str) -> dict:
    info = container_inspect(cid)
    env = {}
    for item in ((info.get("Config") or {}).get("Env") or []):
        if "=" in item:
            k, v = item.split("=", 1)
            env[k] = v
    return env


def container_name(cid: str) -> str:
    try:
        return (container_inspect(cid).get("Name") or "").strip("/")
    except Exception:  # noqa: BLE001
        return ""


def container_image(cid: str) -> str:
    try:
        info = container_inspect(cid)
    except Exception:  # noqa: BLE001
        return ""
    return ((info.get("Config") or {}).get("Image") or "").strip()


def container_labels(cid: str) -> dict:
    try:
        return (container_inspect(cid).get("Config") or {}).get("Labels") or {}
    except Exception:  # noqa: BLE001
        return {}


# ---------- 小工具 ----------

def _q(value: str) -> str:
    """filters 参数要做 URL 编码，否则 JSON 里的 & # 会把 query 截断。"""
    from urllib.parse import quote
    return quote(value, safe="")


def _q1(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


def _path(ref: str) -> str:
    """镜像引用含 / 和 : ，在路径里必须编码。"""
    from urllib.parse import quote
    return quote(ref, safe="")
