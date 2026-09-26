# 发版与手动推送教程

> 当 AI 助理（WorkBuddy）发版失败/不可用、或者你想自己接管这条链路时，按这份文档一步步来。
> 读完一遍就能独立把 `reimburse-workbench` 新版推到 GitHub + 跑通在线升级。

---

## 0. 这条链路总览

```
本地仓库改动 → 本地验证 → 打 release 产物 → 推到 GitHub
   ↑                                              ↓
   ↑                                       生产从 GH Release 检测
   ↑                                       下载 → 校验 → 切容器
```

源码托管：`https://github.com/zhangdubin/reimburse-workbench`（公开仓）
在线升级从 GitHub Releases 拉 `manifest.json` + `<tag>-app.tar.gz` + `<tag>-linux-<arch>.tar.gz`。
生产端一键升级链路见 `docs/使用说明.md`「在线升级」一节。

---

## 1. 前置：GitHub Token 与钥匙串

### 1.1 推荐用**细粒度 PAT**（Fine-grained Personal Access Token）

只给一个仓库、最小权限。**不要用 Classic Token**（权限太宽）。

打开 `https://github.com/settings/personal-access-tokens/new`，按下面设：

| 字段 | 值 |
| --- | --- |
| Token name | `reimburse-workbench-deploy`（或自己起名） |
| Expiration | `90 days`（或更短，过期后重发） |
| Resource owner | `zhangdubin`（你自己的账号） |
| Repository access | Only select repositories → **`zhangdubin/reimburse-workbench`** |
| Permissions → Repository permissions | `Contents` → **Read and write**（其余保持 No access） |

点 **Generate token**。GitHub 只显示这一次的完整 token，**立即复制保存**——一关页面就没了。

### 1.2 存到 macOS 钥匙串（避免明文出现在历史里）

打开终端（zsh）：

```zsh
echo "粘贴 github_pat_ 开头的 token，然后回车（输入时不回显）："
read -rs GH_PAT
echo "读到 ${#GH_PAT} 个字符"
if [ ${#GH_PAT} -lt 40 ]; then
  echo "[-] 长度不对，没读到内容，未保存，请重来一次"
else
  printf "protocol=https\nhost=github.com\n\npassword=%s\nusername=zhangdubin\n" "$GH_PAT" \
    | git credential-osxkeychain store
  echo "[+] 已写入钥匙串（${#GH_PAT} 字符）"
fi
unset GH_PAT
```

**关键陷阱**：zsh 下不要用 `read -p "提示"`！`-p` 在 zsh 是"读 coprocess"，会直接报 `zsh:read:1: -p: no coprocess`、变量恒空、`store` 又照存不误，钥匙串里留下空密码条目，症状是"看起来存好了，一用全是 401 Bad credentials"。

**验证读出来了**（不是空）：

```zsh
printf "protocol=https\nhost=github.com\n\n" \
  | git credential-osxkeychain get 2>/dev/null \
  | sed -n 's/^password=//p' | wc -c
# ≥90（含换行）才算存上
```

> ⚠️ `git config credential.helper` 必须是 `osxkeychain`，否则上面的 store/get 都打不到钥匙串。

---

## 2. 本地仓库初始化（首次拉代码）

```bash
cd <你想放代码的地方>           # 比如 ~/work/reimburse-workbench
git clone https://github.com/zhangdubin/reimburse-workbench.git
cd reimburse-workbench
git config credential.helper osxkeychain      # 仓库级启用钥匙串
git checkout main
```

钥匙串里已经有刚才存的 token，git 会自动从钥匙串里取，无需再输入。

---

## 3. 改代码 + 本地验证

正常开发流程。每次改动后跑一次本地冒烟（避免推到 GitHub 后才发现基础功能挂了）：

```bash
# 起 dev 实例（SQLite，端口 8792）
tools/dev_server.sh start

# 跑相关 smoke（本项目自带 tools/*.py，覆盖安全/识别/AI/扫码/备份等）
DATABASE_URL='sqlite:////tmp/wb_prod_test.db' \
UPLOAD_DIR=/tmp/wb-uploads-8792 \
backend/.venv/bin/python tools/backup_smoke.py
```

---

## 4. 升版号（必做，否则升级链路不会触发）

四处要同步改（缺一个生产会卡在「已是最新」）：

```bash
# 1) 后端版本号
sed -i '' 's/APP_VERSION = "X.Y.Z"/APP_VERSION = "A.B.C"/' backend/app/main.py

# 2) compose 默认镜像 tag
sed -i '' 's/${IMAGE_TAG:-X.Y.Z}/${IMAGE_TAG:-A.B.C}/' docker-compose.yml

# 3) .env.example 默认 tag
sed -i '' 's/IMAGE_TAG=X.Y.Z/IMAGE_TAG=A.B.C/' .env.example

# 4) deploy/install.sh 默认 tag
sed -i '' 's/IMAGE_TAG="X.Y.Z"/IMAGE_TAG="A.B.C"/' deploy/install.sh

# 5) 前端缓存破坏：所有 ?v=X.Y.Z 一律升（不升用户拿到旧 JS）
sed -i '' 's/?v=X.Y.Z/?v=A.B.C/g' frontend/index.html

# 文档里散落的版本引用（如 docs/使用说明.md）
grep -n 'X.Y.Z' docs/使用说明.md && sed -i '' 's/X.Y.Z/A.B.C/g' docs/使用说明.md
```

`schema_changed` 字段决定要不要跑迁移——表结构变了就 `true`，否则 `false`（2.9.x 都是 false）。

---

## 5. 本地构建镜像 + 验证

```bash
docker build -t reimburse-workbench:A.B.C .
```

镜像构建成功会有「Image successfully tagged」字样，耗时约 25 秒（命中缓存）/ 5-10 分钟（冷启动）。

---

## 6. 推送代码 + tag

```bash
git add -A
git commit -m "feat: <一句话说改了什么>"
git push origin main                              # 推分支
git tag vA.B.C HEAD -m "vA.B.C · <一句话>"
git push origin vA.B.C                            # 推 tag
```

tag 推到 GitHub 后，GitHub 才会在 tag 页面看到。

---

## 7. 打 release 产物

```bash
./deploy/make-package.sh --tag A.B.C --release
```

产物在 `dist/release-A.B.C/`：

```
manifest.json                                    ← 升级时用它校验 sha256
reimburse-workbench-A.B.C-app.tar.gz            ← 骨架包（compose / install.sh / deploy / docs）
reimburse-workbench-A.B.C-linux-amd64.tar.gz    ← 镜像包（docker load 即用）
```

⚠️ **必须用 `dist/release-A.B.C/` 里的那份镜像 tar 上传到 Release**。离线安装包里 `dist/reimburse-workbench-A.B.C/images/` 里的 tar 是**另一次 docker save** 导出，gzip 头带时间戳，同一镜像两次导出字节不同、sha 不同——上传那份会让所有升级校验挂掉。

---

## 8. 上传 Release 资产

小文件（manifest + 骨架包，几秒）：

```bash
TOKEN=$(printf "protocol=https\nhost=github.com\n\n" \
  | git credential-osxkeychain get 2>/dev/null \
  | sed -n 's/^password=//p')

RID=<刚建好的 release id>
cd dist/release-A.B.C

for F in manifest.json reimburse-workbench-A.B.C-app.tar.gz; do
  curl -s -o /dev/null -w "up $F -> HTTP:%{http_code}\n" \
    -X POST -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/octet-stream" \
    --data-binary @"$F" \
    "https://uploads.github.com/repos/zhangdubin/reimburse-workbench/releases/$RID/assets?name=$F"
done
```

大文件（镜像包 364MB，约 25 分钟直连上传）：

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @reimburse-workbench-A.B.C-linux-amd64.tar.gz \
  "https://uploads.github.com/repos/zhangdubin/reimburse-workbench/releases/$RID/assets?name=reimburse-workbench-A.B.C-linux-amd64.tar.gz" \
  -o /tmp/upload.log -w "upload: HTTP:%{http_code} speed:%{speed_upload}B/s time:%{time_total}s\n"
```

如果上传**中断**，要清理残尸再传：

```bash
# 列资产看 state=starter 的就是中断的
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://api.github.com/repos/zhangdubin/reimburse-workbench/releases/$RID/assets" \
  | python3 -c "import json,sys; [print(a['id'], a['name'], a['state']) for a in json.load(sys.stdin)]"

# 删除
AID=<资产 id>
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  "https://api.github.com/repos/zhangdubin/reimburse-workbench/releases/assets/$AID" \
  -w "delete: HTTP:%{http_code}\n"
# 然后重新上传
```

---

## 9. 用 curl 建 Release（如果不想走 web UI）

新建 release 时 `target_commitish` 必须是**分支名或 commit SHA**，不能是 tag 本身：

```bash
TOKEN=$(printf "protocol=https\nhost=github.com\n\n" \
  | git credential-osxkeychain get 2>/dev/null \
  | sed -n 's/^password=//p')

cat > /tmp/release.json <<EOF
{
  "tag_name": "vA.B.C",
  "name": "vA.B.C",
  "target_commitish": "main",
  "draft": false,
  "prerelease": false,
  "body": "## A.B.C\n\n一句话说明……"
}
EOF

curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d @/tmp/release.json \
  "https://api.github.com/repos/zhangdubin/reimburse-workbench/releases" \
  -o /tmp/release_resp.json -w "create: HTTP:%{http_code}\n"

# 取 release id
python3 -c "import json; d=json.load(open('/tmp/release_resp.json')); print(d['id'])" \
  > /tmp/release_id.txt
```

---

## 10. 端到端升级（生产端）

生产端（你跑着的 `reimburse-app`）无须手动操作：
- 管理端登录 → 右上角用户菜单 → **系统更新**
- 「检测新版本」会发现 vA.B.C → 一键升级
- 升级会：备份 → 下载资产 → sha256 校验 → 切容器 → 健康检查
- 失败自动回滚到上一个健康版本

要手动确认生产也跑起来了：

```bash
curl -s http://localhost:8080/api/health | python3 -m json.tool | grep version
# 期望: "version": "A.B.C"
```

---

## 11. 应急：手动同步生产（升级链坏了时）

最坏情况——一键升级卡在中间相位、生产版本号没变。这种时候手动升级：

```bash
# 在本机：导出最新镜像成 tar
docker save reimburse-workbench:A.B.C | gzip > rw-A.B.C.tar.gz

# 把 tar 拷到生产服务器（rsync / scp / 网盘都行）
rsync rw-A.B.C.tar.gz 生产服务器:/tmp/

# 在生产服务器：导入镜像 + 改 compose 的 image tag + 重启
ssh 生产服务器 'docker load -i /tmp/rw-A.B.C.tar.gz && \
  cd /path/to/reimburse-workbench && \
  sed -i "s/IMAGE_TAG=.*/IMAGE_TAG=A.B.C/" .env && \
  docker compose up -d --no-deps app'

# 验
curl -s http://localhost:8080/api/health | python3 -m json.tool | grep version
```

---

## 12. 收尾

- 在 GitHub `Settings → Developer settings → Personal access tokens` 把快过期的 token 撤销或让它自然过期
- 检查仓库 Issues 有没有用户反馈新版本的 bug
- 把当天的关键操作（做了什么、踩了什么坑）写进 `~/.workbuddy/memory/<workspace>/YYYY-MM-DD.md` 下次接手不用从头猜

---

## 常见问题

**Q: `git credential-osxkeychain get` 拿到空字符串？**
A: 大概率是 token 当时没存上。重新跑第 1.2 节的 `read -rs` 流程，**先 `echo "读到 N 个字符"` 确认 ≥90 再 `store`**。

**Q: 上传大文件卡死？**
A: 走代理（10.10.10.252:1086）有时更慢；如果代理不可用直接走直连。断点续传 GitHub 不支持——只能 DELETE 残尸重传。

**Q: tag 推上去了但 GitHub 网页不显示？**
A: 用 `git ls-remote origin 'refs/tags/v*'` 看远端有没有这个 tag。没有就是 push 失败；有可能网络抖动，重推一次。

**Q: 升级后端点 502 / upstream connect failed？**
A: 大概率是测试机设了 HTTP_PROXY 拦截了回环请求。生产无此问题（生产是 docker 直连）；测试时 `export no_proxy=*` 或用 `127.0.0.1` 绕过。