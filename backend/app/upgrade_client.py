"""在线升级：版本来源与运行时配置（v2.9.0+）

版本从 GitHub Release 读。约定一个 release 里放三类资产：

  manifest.json                                   ← 版本清单（含 sha256 / 更新说明）
  reimburse-workbench-<ver>-linux-amd64.tar.gz     ← 镜像包（docker load 即用）
  reimburse-workbench-<ver>-app.tar.gz             ← 骨架包（compose / install.sh / deploy / docs）

有 manifest 才能校验完整性；没有就只做「有新版」提示，不自动升级
（除非管理员在配置里显式关掉校验要求）。

配置存 setting 表（admin 专用端点读写），env 作兜底，5 秒内存缓存——
与 jev_client 完全同构，改完配置无需重启。
"""
import json
import os
import ssl
import time
import urllib.error
import urllib.request

from . import models as m

_API = "https://api.github.com"
_CACHE_TTL = 5.0
_config_cache = {"at": 0.0, "db_id": None, "data": None}

# 允许前端配置的 key（白名单）
ALLOWED_KEYS = (
    "upgrade_repo",            # owner/repo
    "upgrade_token",           # GitHub token（私有库或提高限额）
    "upgrade_proxy",           # 出网代理，留空跟随 AI_PROXY
    "upgrade_auto_check",      # 1 = 定时检查
    "upgrade_interval_hours",  # 检查间隔
    "upgrade_allow_prerelease",# 1 = 预发布版也提示
    "upgrade_require_manifest",# 1 = 必须有 manifest 才允许升级
)

DESCRIPTIONS = {
    "upgrade_repo": "GitHub 仓库（owner/repo），Release 里有安装包",
    "upgrade_token": "GitHub 访问令牌（私有库必填；公开库可留空）",
    "upgrade_proxy": "拉取升级包走的代理，留空则跟随 AI_PROXY",
    "upgrade_auto_check": "1 = 后台定时检查新版本",
    "upgrade_interval_hours": "检查间隔（小时，默认 6）",
    "upgrade_allow_prerelease": "1 = 预发布版也提示升级",
    "upgrade_require_manifest": "1 = 必须带 manifest.json 才允许一键升级（推荐）",
}

_ENV_FALLBACK = {
    "upgrade_repo": "UPGRADE_REPO",
    "upgrade_token": "UPGRADE_TOKEN",
    "upgrade_proxy": "UPGRADE_PROXY",
    "upgrade_auto_check": "UPGRADE_AUTO_CHECK",
    "upgrade_interval_hours": "UPGRADE_INTERVAL_HOURS",
    "upgrade_allow_prerelease": "UPGRADE_ALLOW_PRERELEASE",
    "upgrade_require_manifest": "UPGRADE_REQUIRE_MANIFEST",
}


def _load_runtime_config(db) -> dict:
    data = {}
    for key in ALLOWED_KEYS:
        val = ""
        env_name = _ENV_FALLBACK.get(key) or ""
        if env_name:
            val = (os.getenv(env_name) or "").strip()
        if db is not None:
            try:
                row = db.get(m.Setting, key)
            except Exception:  # noqa: BLE001
                row = None
            if row is not None and (row.value or "").strip():
                val = (row.value or "").strip()
        data[key] = val
    return data


def _cfg(db=None) -> dict:
    now = time.time()
    db_id = id(db) if db is not None else None
    cached = _config_cache.get("data")
    if cached is not None and _config_cache.get("db_id") == db_id and now - float(_config_cache.get("at") or 0) < _CACHE_TTL:
        return cached
    data = _load_runtime_config(db)
    _config_cache.update({"at": now, "db_id": db_id, "data": data})
    return data


def invalidate_upgrade_config_cache() -> None:
    _config_cache.update({"at": 0.0, "db_id": None, "data": None})


def configured(db=None) -> tuple[bool, str]:
    cfg = _cfg(db)
    repo = (cfg.get("upgrade_repo") or "").strip()
    if not repo or "/" not in repo:
        return False, "还没配置升级源仓库（格式 owner/repo）"
    return True, "ok"


def interval_hours(db=None) -> float:
    try:
        val = float((_cfg(db).get("upgrade_interval_hours") or "6").strip())
    except ValueError:
        val = 6.0
    return min(max(val, 0.25), 168.0)


def auto_check_on(db=None) -> bool:
    return (_cfg(db).get("upgrade_auto_check") or "0").strip() == "1"


def require_manifest(db=None) -> bool:
    return (_cfg(db).get("upgrade_require_manifest") or "1").strip() != "0"


def allow_prerelease(db=None) -> bool:
    return (_cfg(db).get("upgrade_allow_prerelease") or "0").strip() == "1"


# ---------- 版本号 ----------

def current_version() -> str:
    """延迟导入：main 会导入 routers，这里顶层导入会成环。"""
    try:
        from .main import APP_VERSION
        return str(APP_VERSION)
    except Exception:  # noqa: BLE001
        return "0.0.0"


def ver_tuple(text: str) -> tuple:
    """"v2.9.1" → (2, 9, 1)；遇到非数字段就停（"2.9.1-beta" → (2,9,1)）。"""
    parts = []
    for chunk in str(text or "").strip().lstrip("vV").split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        if num == "":
            break
        parts.append(int(num))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:4])


def is_newer(candidate: str, base: str) -> bool:
    return ver_tuple(candidate) > ver_tuple(base)


# ---------- GitHub 访问 ----------

def _opener(proxy: str, verify_ssl: bool):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler())
    if not verify_ssl:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def _proxy_for(cfg: dict) -> str:
    return (cfg.get("upgrade_proxy") or "").strip() or (os.getenv("AI_PROXY") or "").strip()


def _verify_ssl() -> bool:
    return (os.getenv("AI_VERIFY_SSL", "1") or "1").strip() != "0"


def _gh_json(url: str, cfg: dict, timeout: float = 20.0) -> dict:
    """GET 一个 GitHub API 端点。出错抛 RuntimeError（带可读原因）。"""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "reimburse-workbench-upgrade",
    }
    token = (cfg.get("upgrade_token") or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _opener(_proxy_for(cfg), _verify_ssl()).open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        if exc.code == 404:
            raise RuntimeError("仓库或 Release 不存在（404）：检查 owner/repo 与 token 权限")
        if exc.code in (401, 403):
            remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
            if remaining == "0":
                raise RuntimeError("GitHub 接口限流（未登录 60 次/小时）：填一个 token 可提到 5000 次")
            raise RuntimeError("GitHub 鉴权失败（%s）：token 是否有效／是否有 repo 读权限" % exc.code)
        raise RuntimeError("GitHub 返回 %s：%s" % (exc.code, detail))
    except urllib.error.URLError as exc:
        raise RuntimeError("连不上 GitHub（%s）：内网环境请在配置里填代理" % exc.reason)


def fetch_release(db=None, timeout: float = 20.0) -> dict:
    """拉最新 Release 并解析 manifest。返回 dict（ok/error/...），不抛异常。"""
    cfg = _cfg(db)
    ok, msg = configured(db)
    if not ok:
        return {"ok": False, "error": msg}

    repo = (cfg.get("upgrade_repo") or "").strip().strip("/")
    try:
        rel = _gh_json("%s/repos/%s/releases/latest" % (_API, repo), cfg, timeout=timeout)
    except RuntimeError as exc:
        # 只有 prerelease 时 releases/latest 会 404，可按配置退一步取列表首个
        if allow_prerelease(db):
            try:
                items = _gh_json("%s/repos/%s/releases?per_page=5" % (_API, repo), cfg, timeout=timeout)
                rel = next((x for x in items if not x.get("draft")), None) or {}
                if not rel:
                    return {"ok": False, "error": str(exc)}
            except RuntimeError as exc2:
                return {"ok": False, "error": str(exc2)}
        else:
            return {"ok": False, "error": str(exc)}

    tag = (rel.get("tag_name") or "").strip()
    version = tag.lstrip("vV")
    if not version:
        return {"ok": False, "error": "Release 没有 tag_name"}

    assets = {}
    for a in rel.get("assets") or []:
        assets[(a.get("name") or "")] = {
            "name": a.get("name") or "",
            "size": int(a.get("size") or 0),
            "url": a.get("browser_download_url") or "",
            "digest": (a.get("digest") or "").replace("sha256:", ""),
        }

    manifest = None
    manifest_err = ""
    man_asset = assets.get("manifest.json")
    if man_asset and man_asset.get("url"):
        try:
            manifest = _gh_json_raw(man_asset["url"], cfg, timeout=timeout)
        except RuntimeError as exc:
            manifest_err = str(exc)
    else:
        manifest_err = "该 Release 没有 manifest.json"

    cur = current_version()
    return {
        "ok": True,
        "current_version": cur,
        "version": version,
        "tag": tag,
        "name": rel.get("name") or tag,
        "notes": (rel.get("body") or "")[:8000],
        "published_at": rel.get("published_at") or "",
        "prerelease": bool(rel.get("prerelease")),
        "html_url": rel.get("html_url") or "",
        "has_update": is_newer(version, cur),
        "assets": assets,
        "manifest": manifest,
        "manifest_error": manifest_err,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _gh_json_raw(url: str, cfg: dict, timeout: float = 20.0):
    """manifest.json 走资产下载地址（可能是 objects.githubusercontent.com）。"""
    headers = {"User-Agent": "reimburse-workbench-upgrade", "Accept": "application/json"}
    token = (cfg.get("upgrade_token") or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _opener(_proxy_for(cfg), _verify_ssl()).open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("下载 manifest.json 失败：%s" % exc)


def open_asset_stream(url: str, cfg: dict, timeout: float = 60.0):
    """打开资产下载流（下载 368MB 镜像包用），返回 response 对象，调用方负责 close。"""
    headers = {"User-Agent": "reimburse-workbench-upgrade"}
    token = (cfg.get("upgrade_token") or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers, method="GET")
    return _opener(_proxy_for(cfg), _verify_ssl()).open(req, timeout=timeout)


def platform_asset(manifest: dict, arch: str) -> dict | None:
    """从 manifest 里挑本机架构的镜像资产。兼容 images 字典与 files 列表两种写法。"""
    if not isinstance(manifest, dict):
        return None
    images = manifest.get("images")
    if isinstance(images, dict):
        item = images.get(arch)
        if isinstance(item, dict):
            return item
    files = manifest.get("files")
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, dict):
                continue
            if arch in (item.get("name") or "") or arch == (item.get("arch") or ""):
                return item
    return None
