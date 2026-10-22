"""备份页改造（v2.9.28）验收脚本。

三条主线各有用例：
  A 存储可视化：status 给出容器内/宿主路径、可写性、剩余空间、健康度与取包命令
  B 下载链路：一次性票据下载、Range 续传、无凭证拒绝、非法文件名拒绝
  C 策略配置：批量保存、非法值被拒、周期改完 status 立刻反映

用法（dev 实例已起来之后）：
    WB_BASE_URL=http://127.0.0.1:8792 WB_PASSWORD=Adm1n@2026 python backup_ui_test.py
"""

import json
import os
import sys
import tempfile
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
FAILED = []
TOKEN = None


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


def _opener():
    op = urllib.request.build_opener()
    # 沙箱里的 HTTP_PROXY 会把本地回环请求也接管走（502 假象），显式绕开
    op.add_handler(urllib.request.ProxyHandler({}))
    return op


def req(method, path, body=None, params=None, token=None, headers=None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if token:
        hdrs["Authorization"] = "Bearer " + token
    r = urllib.request.Request(url, data=data, method=method)
    for k, v in hdrs.items():
        r.add_header(k, v)
    try:
        with _opener().open(r, timeout=60) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode() if raw else "null")
            except Exception:
                return resp.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload.decode() or "null")
        except Exception:
            return e.code, payload.decode(errors="replace")


def raw_get(path, params=None, token=None, headers=None):
    """下载用的原始请求：要拿到二进制与响应头。"""
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = "Bearer " + token
    r = urllib.request.Request(url, method="GET")
    for k, v in hdrs.items():
        r.add_header(k, v)
    try:
        with _opener().open(r, timeout=60) as resp:
            return resp.status, resp.read(), _hdrs(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), _hdrs(e.headers or {})


def _hdrs(h):
    """响应头名字大小写不敏感（http.client 给的是原样大小写）。"""
    out = {}
    for k in (h.keys() if hasattr(h, "keys") else []):
        out[k.lower()] = h.get(k)
    return out


def main():
    global TOKEN
    tag = uuid.uuid4().hex[:6]
    tmpdir = tempfile.mkdtemp(prefix="wb-bk-test-")

    print("\n== 登录 ==")
    st, body = req("POST", "/api/auth/login", {"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    check("管理员登录", st == 200, st)
    TOKEN = body.get("token") if isinstance(body, dict) else None
    check("拿到 token", bool(TOKEN))

    print("\n== A 存储可视化 ==")
    # 先把备份目录指到临时目录，避免往默认 /app/data/backups 上写
    st, _ = req("PUT", "/api/admin/backup/config/backup_dir", {"value": tmpdir}, token=TOKEN)
    check("备份目录可改（绝对路径校验）", st == 200, st)

    st, s = req("GET", "/api/admin/backup/status", token=TOKEN)
    check("status 200", st == 200, st)
    stg = (s or {}).get("storage") or {}
    loc = stg.get("local") or {}
    check("返回 storage.local", bool(loc), loc)
    # macOS 的 /var 是 /private/var 的软链，比 realpath 才对得上
    check("容器内路径=刚设的目录", os.path.realpath(str(loc.get("path"))) == os.path.realpath(tmpdir),
          f'{loc.get("path")} vs {tmpdir}')
    check("目录存在性", loc.get("exists") is True, loc)
    check("可写性", loc.get("writable") is True, loc)
    check("剩余空间有值", isinstance(loc.get("free"), int) and loc["free"] > 0, loc.get("free"))
    check("返回 health", (s.get("health") or {}).get("text"), s.get("health"))
    check("返回 counts", "counts" in s, list(s.keys()))
    check("返回取包命令", "docker cp" in str(s.get("copy_command") or ""), s.get("copy_command"))
    # 宿主路径：dev 裸跑（非容器）拿不到挂载表，必须给出明确原因而不是空着
    check("宿主路径查不到时给出 reason", loc.get("host_path") is None and loc.get("reason"), loc)

    print("\n== B 备份与下载 ==")
    st, run = req("POST", "/api/admin/backup/run", {}, token=TOKEN)
    check("立即备份", st == 200 and (run or {}).get("result", {}).get("name"), f"{st} {run}")
    name = ((run or {}).get("result") or {}).get("name") if isinstance(run, dict) else None
    if not name:
        print("  !! 备份没跑出文件名，后续用例跳过")
        return

    st, lst = req("GET", "/api/admin/backup/list", token=TOKEN)
    items = (lst or {}).get("items") or []
    hit = [i for i in items if i.get("name") == name]
    check("列表里有新包", bool(hit), items[:2])
    check("列表带 sha256（sidecar）", bool(hit and hit[0].get("sha256")), hit[0] if hit else None)
    size = hit[0].get("size") if hit else 0
    check("列表带宿主路径字段", bool(hit) and "host_path" in hit[0], hit[0] if hit else None)

    st, tk = req("POST", "/api/admin/backup/ticket", {"name": name}, token=TOKEN)
    check("拿到下载票据", st == 200 and (tk or {}).get("ticket"), f"{st} {tk}")
    check("票据带 url", "ticket=" in str((tk or {}).get("url") or ""), tk)
    ticket = (tk or {}).get("ticket") if isinstance(tk, dict) else None

    st, blob, hdrs = raw_get(f"/api/admin/backup/download/{urllib.parse.quote(name)}", params={"ticket": ticket})
    check("票据下载成功（不带 Authorization）", st == 200, st)
    check("下载字节数与列表一致", len(blob) == size, f"{len(blob)} vs {size}")
    check("带 Content-Length", int(hdrs.get("content-length") or 0) == size, hdrs.get("content-length"))
    check("带 Accept-Ranges", hdrs.get("accept-ranges") == "bytes", hdrs.get("accept-ranges"))
    check("带 Content-Disposition", "attachment" in str(hdrs.get("content-disposition") or ""), hdrs.get("content-disposition"))
    check("带 sha 响应头（sidecar，未重算整包）", bool(hdrs.get("x-backup-sha256")), hdrs.get("x-backup-sha256"))

    st, part, hdrs2 = raw_get(
        f"/api/admin/backup/download/{urllib.parse.quote(name)}",
        params={"ticket": ticket}, headers={"Range": "bytes=0-99"},
    )
    check("Range 续传返回 206", st == 206, st)
    check("Range 只回 100 字节", len(part) == 100, len(part))
    check("带 Content-Range", "bytes 0-99" in str(hdrs2.get("content-range") or ""), hdrs2.get("content-range"))

    st, _b, _h = raw_get(f"/api/admin/backup/download/{urllib.parse.quote(name)}")
    check("无凭证下载被拒 401", st == 401, st)
    st, _b, _h = raw_get(f"/api/admin/backup/download/{urllib.parse.quote(name)}", params={"ticket": "1.999.x"})
    check("过期/伪造票据被拒 401", st == 401, st)
    st, _b, _h = raw_get(f"/api/admin/backup/download/{urllib.parse.quote(name)}", token=TOKEN)
    check("Bearer 下载仍可用（回退路径）", st == 200, st)
    st, _ = req("POST", "/api/admin/backup/ticket", {"name": "../evil"}, token=TOKEN)
    check("非法文件名换不到票据", st == 400, st)
    st, _b, _h = raw_get(f"/api/admin/backup/download/not-exist.tar.gz", token=TOKEN)
    check("不存在的包给 404", st == 404, st)

    print("\n== C 策略配置 ==")
    st, r = req("PUT", "/api/admin/backup/config", {"values": {
        "backup_interval_hours": "12", "backup_keep_local": "7", "backup_keep_remote": "3",
    }}, token=TOKEN)
    check("批量保存三项", st == 200 and len((r or {}).get("saved") or {}) == 3, f"{st} {r}")
    st, s2 = req("GET", "/api/admin/backup/status", token=TOKEN)
    cfg = (s2 or {}).get("config") or {}
    check("周期生效 12", float(cfg.get("backup_interval_hours") or 0) == 12.0, cfg)
    check("本地保留 7", str(cfg.get("backup_keep_local")) == "7", cfg)
    check("外挂保留 3", str(cfg.get("backup_keep_remote")) == "3", cfg)

    st, _ = req("PUT", "/api/admin/backup/config", {"values": {"backup_keep_local": "-1"}}, token=TOKEN)
    check("保留份数为负被拒 400", st == 400, st)
    st, _ = req("PUT", "/api/admin/backup/config", {"values": {"backup_dir": "relative/path"}}, token=TOKEN)
    check("相对路径被拒 400", st == 400, st)
    st, _ = req("PUT", "/api/admin/backup/config", {"values": {"backup_interval_hours": "abc"}}, token=TOKEN)
    check("周期非数字被拒 400", st == 400, st)
    st, _ = req("PUT", "/api/admin/backup/config", {"values": {"not_a_key": "1"}}, token=TOKEN)
    check("未知配置项被拒 400", st == 400, st)

    st, p = req("POST", "/api/admin/backup/prune", {}, token=TOKEN)
    check("裁剪接口可用", st == 200 and "local_pruned" in (p or {}), f"{st} {p}")

    # 还原现场：周期改回 24、目录改回默认（删配置即回落环境变量）
    req("PUT", "/api/admin/backup/config", {"values": {"backup_interval_hours": "24"}}, token=TOKEN)
    req("DELETE", "/api/admin/backup/config/backup_dir", token=TOKEN)

    print("\n" + "=" * 56)
    print(f"通过 {OK} / 失败 {FAIL}")
    if FAILED:
        print("失败项：" + "、".join(FAILED))
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
