"""在线升级（v2.9.0）测试

测的都是「不值得真去 GitHub 拉一次包」的纯逻辑与安全边界：
  1. 版本号比较（tag 前缀、预发布后缀、跨位）
  2. 升级配置的读写与白名单（防止往 setting 表乱写 key）
  3. 升级前置检查：没有 Docker socket 时必须明确拦住
  4. 从 manifest 里按架构挑镜像包（挑错架构 = load 成功但起不来）
  5. 克隆重建 app 容器时，哪些字段必须剔除（主机名/AutoRemove 带旧容器痕迹）
  6. 从 db 容器取回的 tar 里正确取出备份文件
  7. Release 解析与 has_update 判定（mock GitHub 响应，不联网）

用法：
    python upgrade_test.py
"""
import io
import os
import pathlib
import sys
import tarfile
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="upgrade_test_"))
os.environ["DATABASE_URL"] = "sqlite:///" + str(TMP / "t.db")
os.environ["UPGRADE_DIR"] = str(TMP / "upgrade")

from app import database as dbm  # noqa: E402
from app import docker_api as dk  # noqa: E402
from app import models as m  # noqa: E402
from app import upgrade_client as uc  # noqa: E402
from app import upgrade_runner as ur  # noqa: E402
from app import upgrade_worker as uw  # noqa: E402

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


# ---------- 版本号 ----------

def test_version():
    group("版本号比较")
    check("2.9.0 > 2.8.0", uc.is_newer("2.9.0", "2.8.0"))
    check("2.8.0 不比 2.9.0 新", not uc.is_newer("2.8.0", "2.9.0"))
    check("同版本不升级", not uc.is_newer("2.9.0", "2.9.0"))
    check("2.10.0 > 2.9.9（不是字符串比较）", uc.is_newer("2.10.0", "2.9.9"))
    check("v 前缀可识别", uc.is_newer("v2.9.1", "2.9.0"))
    check("预发布后缀不影响主版本比较", uc.is_newer("2.9.0-beta.1", "2.8.9"))
    check("2.9.0 与其 beta 视为同版本", not uc.is_newer("2.9.0-beta", "2.9.0"))
    check("ver_tuple 补零", uc.ver_tuple("2.9") == (2, 9, 0))
    check("ver_tuple 容忍空值", uc.ver_tuple("") == (0, 0, 0))


# ---------- 配置 ----------

def test_config(db):
    group("升级配置读写")
    uc.invalidate_upgrade_config_cache()
    ok, msg = uc.configured(db)
    check("未配置时 configured=False", not ok, msg)

    db.add(m.Setting(key="upgrade_repo", value="acme/reimburse", remark=""))
    db.commit()
    uc.invalidate_upgrade_config_cache()
    ok, msg = uc.configured(db)
    check("填了 owner/repo 后 configured=True", ok, msg)

    db.get(m.Setting, "upgrade_repo").value = "acme"
    db.commit()
    uc.invalidate_upgrade_config_cache()
    ok, _ = uc.configured(db)
    check("仓库写成裸名字（缺 /）视为未配置", not ok)

    db.get(m.Setting, "upgrade_repo").value = "acme/reimburse"
    db.commit()
    db.add(m.Setting(key="upgrade_interval_hours", value="12", remark=""))
    db.commit()
    uc.invalidate_upgrade_config_cache()
    check("检查间隔可读取", uc.interval_hours(db) == 12.0)
    db.get(m.Setting, "upgrade_interval_hours").value = "abc"
    db.commit()
    uc.invalidate_upgrade_config_cache()
    check("间隔非法时回落 6 小时", uc.interval_hours(db) == 6.0)
    db.get(m.Setting, "upgrade_interval_hours").value = "999"
    db.commit()
    uc.invalidate_upgrade_config_cache()
    check("间隔超上限被夹到 168 小时", uc.interval_hours(db) == 168.0)

    uc.invalidate_upgrade_config_cache()
    check("自动检查默认关", not uc.auto_check_on(db))
    check("默认要求 manifest", uc.require_manifest(db))
    db.add(m.Setting(key="upgrade_require_manifest", value="0", remark=""))
    db.add(m.Setting(key="upgrade_auto_check", value="1", remark=""))
    db.commit()
    uc.invalidate_upgrade_config_cache()
    check("配置可关闭 manifest 要求", not uc.require_manifest(db))
    check("自动检查可打开", uc.auto_check_on(db))
    check("白名单外的 key 不在列表里", "upgrade_evil" not in uc.ALLOWED_KEYS)


# ---------- 前置检查 ----------

def test_preflight(db):
    group("升级前置检查")
    # upgrade_repo 在 test_config 里已经写成 acme/reimburse，这里直接用
    uc.invalidate_upgrade_config_cache()
    ok, _msg = uc.configured(db)
    check("前置条件里仓库已配置", ok)

    # 注意：开发机（Docker Desktop）上 /var/run/docker.sock 往往真的存在，
    # 那样这条断言就会随机器状态飘。所以显式把 socket 指到一个不存在的路径。
    prev = os.environ.get("DOCKER_SOCK")
    os.environ["DOCKER_SOCK"] = str(TMP / "no-such" / "docker.sock")
    try:
        problems = ur.preflight(db)
        joined = "；".join(problems)
        check("没有 Docker socket 时被拦住", any("Docker" in p for p in problems), joined)

        ok, hint = dk.available()
        check("socket 探测给出可读原因", (not ok) and bool(hint), hint)
    finally:
        if prev is None:
            os.environ.pop("DOCKER_SOCK", None)
        else:
            os.environ["DOCKER_SOCK"] = prev

    # 反过来：本机真的连得上 Docker 时，前置检查就不该再报 Docker 的问题
    real_ok, real_hint = dk.available()
    if real_ok:
        joined2 = "；".join(ur.preflight(db))
        check("socket 可用时不误报 Docker 问题", "Docker" not in joined2, joined2)
    else:
        print(f"  \033[33m·\033[0m 本机 Docker 不可用（{real_hint}），跳过放行分支")


# ---------- manifest ----------

def test_manifest():
    group("安装包挑选")
    manifest = {
        "version": "2.9.0",
        "images": {
            "amd64": {"name": "a-amd64.tar.gz", "sha256": "aa", "size": 1},
            "arm64": {"name": "a-arm64.tar.gz", "sha256": "bb", "size": 2},
        },
    }
    got = uc.platform_asset(manifest, "amd64")
    check("amd64 机器拿到 amd64 包", got and got["sha256"] == "aa")
    got = uc.platform_asset(manifest, "arm64")
    check("arm64 机器拿到 arm64 包", got and got["sha256"] == "bb")
    check("manifest 里没有的架构返回空", uc.platform_asset(manifest, "armv7") is None)
    check("manifest 不是字典时不炸", uc.platform_asset(None, "amd64") is None)

    files_form = {"files": [{"name": "x-linux-arm64.tar.gz", "sha256": "cc"}]}
    got = uc.platform_asset(files_form, "arm64")
    check("files 列表写法也认", got and got["sha256"] == "cc")


# ---------- 克隆重建 ----------

def test_clone_body():
    group("容器克隆重建参数")
    info = {
        "Name": "/reimburse-app",
        "Config": {
            "Image": "reimburse-workbench:2.8.0",
            "Hostname": "abc123def456",
            "Env": ["A=1", "MAIL_SECRET=x"],
            "Cmd": ["uvicorn", "app.main:app"],
            "Labels": {"com.docker.compose.project": "reimburse-workbench"},
            "ExposedPorts": {"8000/tcp": {}},
            "Healthcheck": {"Test": ["CMD", "x"]},
        },
        "HostConfig": {
            "Binds": ["reimburse-uploads:/app/data/uploads"],
            "PortBindings": {"8000/tcp": [{"HostPort": "8080"}]},
            "AutoRemove": False,
            "ContainerIDFile": "",
            "RestartPolicy": {"Name": "unless-stopped"},
            "SecurityOpt": ["seccomp=unconfined"],
        },
    }
    body = uw._clone_body(info, "reimburse-workbench:2.9.0")
    check("镜像被替换成新版", body["Image"] == "reimburse-workbench:2.9.0")
    check("环境变量原样保留", "MAIL_SECRET=x" in body["Env"])
    check("端口映射原样保留", body["HostConfig"]["PortBindings"]["8000/tcp"][0]["HostPort"] == "8080")
    check("数据卷原样保留", body["HostConfig"]["Binds"] == ["reimburse-uploads:/app/data/uploads"])
    check("seccomp 设置保留（老引擎必须）", body["HostConfig"]["SecurityOpt"] == ["seccomp=unconfined"])
    check("compose 标签保留（compose 仍认得这个容器）",
          body["Labels"].get("com.docker.compose.project") == "reimburse-workbench")
    check("不带旧容器主机名", "Hostname" not in body)
    check("AutoRemove 明确关掉", body["HostConfig"]["AutoRemove"] is False)
    check("没有 None 值字段（API 会拒）", all(v is not None for v in body.values()))


# ---------- 从容器取文件 ----------

def test_tar_extract():
    group("从 db 容器取备份文件")
    buf = io.BytesIO()
    payload = b"\x1f\x8b fake-gzip"
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo("reimburse-backup.sql.gz")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    dest = str(TMP / "out.sql.gz")
    uw._write_from_tar(buf.getvalue(), dest)
    check("归档里的第一个文件被取出", open(dest, "rb").read() == payload)

    empty = io.BytesIO()
    with tarfile.open(fileobj=empty, mode="w"):
        pass
    try:
        uw._write_from_tar(empty.getvalue(), dest)
        check("空归档应报错", False)
    except Exception:
        check("空归档给出可读错误", True)


# ---------- Release 解析（mock） ----------

def test_release_parse(db):
    group("Release 解析与升级判定")
    fake = {
        "tag_name": "v2.9.0",
        "name": "2.9.0",
        "body": "本次更新：在线升级",
        "published_at": "2026-09-24T15:00:00Z",
        "prerelease": False,
        "html_url": "https://example/release",
        "assets": [
            {"name": "manifest.json", "size": 100, "browser_download_url": "http://x/manifest.json"},
            {"name": "reimburse-workbench-2.9.0-linux-amd64.tar.gz", "size": 999,
             "browser_download_url": "http://x/img.tar.gz"},
            {"name": "reimburse-workbench-2.9.0-app.tar.gz", "size": 10,
             "browser_download_url": "http://x/app.tar.gz"},
        ],
    }
    manifest = {"version": "2.9.0", "images": {"amd64": {"name": "img", "sha256": "deadbeef", "size": 999}}}

    orig_json = uc._gh_json
    orig_raw = uc._gh_json_raw
    uc._gh_json = lambda url, cfg, timeout=20.0: fake if "/releases/" in url else {}
    uc._gh_json_raw = lambda url, cfg, timeout=20.0: manifest
    try:
        uc.invalidate_upgrade_config_cache()
        rel = uc.fetch_release(db)
        check("Release 读取成功", rel.get("ok"), rel.get("error"))
        check("版本号去掉了 v 前缀", rel.get("version") == "2.9.0")
        # 直接和当前版本比：跑测试时仓库里就是这个版本，写死 True 会假红
        check("has_update 与当前版本一致",
              rel.get("has_update") == uc.is_newer("2.9.0", uc.current_version()),
              f"当前 {uc.current_version()}")
        check("manifest 解析成功", bool(rel.get("manifest")))
        check("assets 收集完整", len(rel.get("assets") or {}) == 3)
        check("更新说明带上", "在线升级" in (rel.get("notes") or ""))
        asset = uc.platform_asset(rel.get("manifest"), "amd64")
        check("能按架构挑到镜像包", asset and asset["sha256"] == "deadbeef")
    finally:
        uc._gh_json = orig_json
        uc._gh_json_raw = orig_raw


def test_release_current(db):
    group("已是最新版本")
    fake = {"tag_name": "v2.9.0", "assets": [], "body": ""}
    orig_json = uc._gh_json
    uc._gh_json = lambda url, cfg, timeout=20.0: fake
    try:
        rel = uc.fetch_release(db)
        # 测试进程里 APP_VERSION 是当前仓库版本，用它判断
        cur = uc.current_version()
        check("版本号能识别", rel.get("version") == "2.9.0")
        check("has_update 与当前版本一致", rel.get("has_update") == uc.is_newer("2.9.0", cur),
              f"当前 {cur}")
        check("缺 manifest 时给出原因", "manifest" in (rel.get("manifest_error") or "").lower())
    finally:
        uc._gh_json = orig_json


# ---------- 状态文件 ----------

def test_status_file():
    group("升级进展文件")
    ur.write_status(phase="downloading", progress=0.42, message="下载中", target_version="2.9.0")
    st = ur.read_status()
    check("phase 落盘", st.get("phase") == "downloading")
    check("进度落盘", abs(float(st.get("progress")) - 0.42) < 0.001)
    check("busy() 识别进行中", ur.busy())
    ur.append_log("测试日志一行")
    st = ur.read_status()
    check("日志追加成功", any("测试日志一行" in x for x in (st.get("log") or [])))
    ur.write_status(phase="idle", progress=0.0)
    check("空闲时 busy() 为假", not ur.busy())
    check("状态里带上当前版本与 Docker 可用性",
          "current_version" in st and "docker_ready" in st)


def main():
    m.Base.metadata.create_all(bind=dbm.engine)
    db = dbm.SessionLocal()
    try:
        test_version()
        test_config(db)
        test_manifest()
        test_clone_body()
        test_tar_extract()
        test_release_parse(db)
        test_release_current(db)
        test_status_file()
        test_preflight(db)
    finally:
        db.close()
    print(f"\n结果：\033[32m{OK} 通过\033[0m / \033[31m{FAIL} 失败\033[0m")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
