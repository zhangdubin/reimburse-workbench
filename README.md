# 销售费用报销管理工作台

面向销售团队的**费用报销全流程管理后台**：账号与四角色权限、按金额分级审批、发票台账与影像留存、邮箱自动收票与电子发票识别、费用标准与预算执行监控、内置费用合规预警引擎，以及可对接市面主流大模型的 **AI 智能助手**（只读分析 + 识别兜底 + 审批建议）。

Docker 一键部署，前端页面与后端 API 同容器提供，数据落 PostgreSQL，容器以**非 root** 身份运行。

界面预览（`docs/screenshots/`，由 `tools/render-check.config.js` 在真实 Chromium 中逐页核验渲染后自动截图）：

| 统计看板 | 发票管理 | 发票收件箱 · 手工入账 |
| --- | --- | --- |
| ![统计看板](docs/screenshots/p01-统计看板.png) | ![发票管理](docs/screenshots/p03-发票管理.png) | ![手工入账](docs/screenshots/c5-手工入账识别预览.png) |

| AI 智能助手 · 对话与动作确认 | AI 设置 · 多厂商模型管理 |
| --- | --- |
| ![AI助手](docs/screenshots/c4-AI助手对话.png) | ![AI设置](docs/screenshots/p09-AI设置.png) |

---

## 功能模块

| 模块 | 能力 |
| --- | --- |
| **登录与权限** | 本地账号 + PBKDF2 加盐哈希口令；服务端会话表（token 只存哈希、支持过期与强制下线）；四角色权限矩阵与**行级数据隔离**（申请人只见自己的单、审批人只见本部门、财务/管理员全量）；新增账号强制改密 |
| **统计看板** | 报销总额 / 已付款 / 待审批 / 本月费用 / 发票金额 / 平均单笔 6 项 KPI；费用走势、大类构成、类型占比、部门月度堆叠、部门/人员/客户/项目排名、单据状态分布（**全部按当前用户可见范围统计**） |
| **报销单管理** | 多条件检索；明细行编辑；**新建时申请人默认选中当前登录用户**（部门随之带出，下拉仍可改为他人代建）；状态流转 `草稿 → 待审批 → 已通过 → 已付款`，支持驳回、撤回、撤销付款；**按金额分级审批**（超阈值自动加签上级，阈值后台可配）；完整流转日志（含审批级数）；批量删除与一键清空；CSV 导出；**A4 打印**（费用明细、合计大写、关联发票、流转记录、签字栏） |
| **发票管理** | 发票台账（号码/代码/类型/税额/购销双方）；单张与批量查验；**重复报销检测**；关联/改关联报销单；**影像上传留存**（PDF/图片，鉴权下载、路径穿越防御）；批量删除、清空散票、清空全部；CSV 导出；**打印清单**（勾选打印或整页打印，含合计大写）与**单张打印** |
| **发票收件箱** | **IMAP 直连收票**（企业邮箱/163/QQ，支持 SSL、文件夹、主题关键词、发件人白名单、仅未读、时间窗）；**外部投递通道**（Agent Mail / 邮件网关 / 脚本，Message-ID 幂等去重）；**自动识别**（OFD/XML/XLSX 元数据解析、PDF 字体 CMap 解码、**本地 OCR 识别拍照件与扫描件**、可选 AI 大模型兜底增强，返回置信度与逐字段来源）；收件记录可追溯；**手工入账**（识别结果逐字段填入可编辑表单，可改可补后再入账） |
| **AI 智能** | **多厂商模型接入**（DeepSeek / 通义千问 / Kimi / 智谱 / 豆包 / OpenAI / Azure / Ollama / 硅基流动等 OpenAI 兼容接口，多配置分场景启用，Key 加密存储）；**智能助手浮窗**（流式对话、当前页面上下文感知、待确认动作卡片——写操作必须人工点「执行」）；**AI 兜底识别**（本地识别全空时由视觉大模型补字段）；**报销单分析 / 审批建议**（只读，给出风险点与建议，不代替人拍板）；调用用量与耗时统计 |
| **费用管理** | 费用类型与报销标准（是否需票、单笔/单日/月度限额）；预算编制与执行监控（年·月 × 部门/项目/费用类型，实时执行率与超支）；跨单据费用明细台账；客户/项目/员工/部门基础数据维护；各表均支持批量删除 |
| **异常与预警** | 超限额明细、缺发票明细、问题发票、大额报销单、超期未审批 5 类风险扫描，阈值可调；支持从预警跳转到对应报销单 |
| **系统管理** | 用户与角色管理（含重置密码、停用、审批级别）；系统参数（审批阈值等，管理员/财务可读、仅管理员可写）；**操作审计日志**（所有写操作与登录事件留痕，可筛选、批量删除、按天数清理） |

---

## 快速开始（Docker，推荐）

```bash
cd reimburse-workbench
cp .env.example .env
# 至少改这三项：DB_PASSWORD、MAIL_SECRET、ADMIN_PASSWORD
docker compose up -d --build
```

浏览器打开 **http://localhost:8080**（端口由 `.env` 的 `APP_PORT` 控制）。

首次启动会自动：① 执行 Alembic 迁移到最新表结构；② 补齐系统参数；③ 创建初始管理员。**不会**灌任何演示数据（`SEED_DEMO` 默认 `0`）。

拿到初始密码的两种方式：

```bash
# 方式一：在 .env 里显式指定 ADMIN_PASSWORD
# 方式二：留空，让容器随机生成并打印
docker compose logs app | grep -A2 '初始管理员'
```

用初始账号登录后，系统**强制要求修改初始密码**（改完所有会话失效，需重新登录）。

> **`MAIL_SECRET` 必须显式配置**，否则邮箱授权码无法安全保存（生产环境直接拒绝保存）。
> 生成方式：`openssl rand -hex 32`。**更换该值会导致已存的邮箱密码全部解不开**，需要重新填写。

查看状态与日志：

```bash
docker compose ps
docker compose logs -f app
curl http://localhost:8080/api/health      # {"status":"ok","database":"up","schema_revision":"..."}
```

### 一键部署到其他生产环境

上面那套是「手改 .env 再 up」的做法。要往别的机器上交一份能直接用的部署，用安装脚本——它会自动生成随机口令与主密钥、自检环境、选镜像、等健康检查通过，最后把访问地址与初始口令打印出来：

```bash
# 把安装包整个目录拷到目标机后（或直接在本仓库里）
cd reimburse-workbench
./deploy/install.sh
```

| 场景 | 命令 |
| --- | --- |
| 换端口 | `./install.sh --port 8081` |
| 同机再起一套（容器名/卷名自动隔离，互不干扰） | `./install.sh --name stg --port 8081` |
| 配置与备份放别处，代码留在包目录 | `./install.sh --dir /opt/reimburse` |
| 目标机没有外网 | `./install.sh --offline`（配合离线安装包） |
| 强制在目标机重建镜像 | `./install.sh --build` |

做离线安装包（含镜像 tar.gz，`docker load` 即用）：

```bash
./deploy/make-package.sh --tag 2.6.0      # 产物在 dist/reimburse-workbench-2.6.0/
./deploy/make-package.sh --no-image       # 只出源码包（目标机现场构建，几百 KB）
```

脚本里有几条刻意做成「宁可停下也不硬来」的保护，都是踩过坑之后加的：

- **`.env` 幂等**：已存在就原样沿用，只补缺失项。主密钥绝不重新生成——换了它，库里已加密的邮箱授权码就永远解不开。
- **容器名冲突预检**：同名容器若属于另一套实例（project 或配置目录对不上），直接报错退出并给出两条可照做的出路。**不会**把正在跑的生产容器接管过去。
- **已装过的实例可以反复重跑**：识别到自己项目的容器在跑就按「就地更新」处理，不会因为端口被自己占着而误判成冲突。
- **`ENV=prod` + `SEED_DEMO=0` + `DOCS_ENABLED=0`**：演示数据（自带固定口令的账号）在安装脚本里被强制关掉，接口文档一并关闭。
- **`MAIL_SECRET` 长度自检**：不足 32 字符直接拒绝安装——短密钥等于没有加密。

> 完整的安装、升级、备份恢复、忘记口令、故障排查与上线安全清单，见 [`docs/使用说明.md`](docs/使用说明.md)。

### 镜像构建说明

Dockerfile 默认使用 `python:3.11-slim`，pip 源默认走清华镜像（内网/国内构建快很多）。

```ini
# .env
PIP_INDEX=https://pypi.org/simple          # 海外环境
```

换基础镜像：`docker compose build --build-arg PYTHON_IMAGE=python:3.12-slim`

如果 Docker 守护进程直连外网很慢，可以让构建走内网代理：

```bash
docker compose build \
  --build-arg HTTP_PROXY=http://10.10.10.252:1086 \
  --build-arg HTTPS_PROXY=http://10.10.10.252:1086 \
  --build-arg NO_PROXY=localhost,127.0.0.1,db
```

（代理只影响构建阶段；容器运行时不带这些变量。）

**不依赖 apt**：`python:3.11-slim` 自带 `/usr/share/zoneinfo`，时区靠 `ENV TZ=Asia/Shanghai` 生效，所以 Dockerfile 里**不跑 apt-get**。这既省了构建时间，也绕开了一个真实踩过的坑：部分受限/沙箱化的构建环境里，apt 的 `docker-clean` 钩子（`APT::Update::Post-Invoke` 里的 `rm`）会返回非 0，导致整条 `apt-get update` 以 exit 100 失败。

同理，**非 root 用户不调用 `useradd`/`adduser`**（Debian slim 不保证有 `passwd` 包），而是直接用数字 UID/GID `10001:10001` —— Docker 允许这么做，`chown` 也不要求该用户存在。

pip 安装带 `--progress-bar off`：进度条会另起线程，线程受限的环境下会抛 `RuntimeError: can't start new thread` 让安装整体失败。这个开关请勿删。

**libdmtx 按架构预编译进镜像**（Data Matrix 打印与扫码要用）。`backend/vendor/libdmtx/lib/` 下按架构分目录，放的是 Debian trixie 的 `libdmtx 0.7.7-1.2+b1`（`x86_64-linux-gnu` 与 `aarch64-linux-gnu` 各一份，同源同版本）。构建时按 `platform.machine()` 挑本机那份装到 `/usr/local/lib`（Debian 的 `ld.so.conf.d/libc.conf` 在所有架构上都收录该目录），装完 `ldconfig -p` 反查，并在装好 pylibdmtx 之后**真正 dlopen + encode 一次**做自检——两处任一不过就直接构建失败。

这是 2.9.3 补的教训：早期版本把路径写死成 `x86_64-linux-gnu`，在 ARM（aarch64）机器上现场构建时「看着装成功了」（COPY 不校验架构、文件也 `ls` 得到），但 `import pylibdmtx` 会抛 `Unable to find dmtx shared library`；而 `print_qr` 是启动期导入的，结果**整个服务起不来**（其实只有打印条码这一个功能是坏的）。以后要加新架构：从 <https://deb.debian.org/debian/pool/main/libd/libdmtx/> 取 `libdmtx0t64_<版本>_<架构>.deb`（运行时 `.so`）与 `libdmtx-dev_<版本>_<架构>.deb`（`.a` / `.h` / `pkgconfig`），`ar x` 解开后放进 `backend/vendor/libdmtx/lib/<架构>-linux-gnu/` 即可——缺目录时构建会直接失败并把这个补法打印出来。

---

## 登录与角色权限

四个角色，权限矩阵如下（后端 `security.py` 是唯一权威，前端只做界面可见性）：

| 能力 | 申请人 | 审批人 | 财务 | 管理员 |
| --- | :-: | :-: | :-: | :-: |
| 建单 / 编辑自己的草稿 | ✅ | ✅ | ✅ | ✅ |
| 查看报销单范围 | 仅本人 | 本部门 | 全部 | 全部 |
| 审批 / 驳回 | ❌ | ✅（本部门） | ❌ | ✅ |
| 付款 / 撤销付款 | ❌ | ❌ | ✅ | ✅ |
| 发票登记 / 查验 / 关联 | ❌ | ❌ | ✅ | ✅ |
| 发票影像上传 | 只读 | 只读 | ✅ | ✅ |
| 发票收件箱（收票 / 识别 / 入账） | ❌ | ❌ | ✅ | ✅ |
| 收票邮箱账号增删改 | ❌ | ❌ | ❌ | ✅ |
| 费用类型 / 预算维护 | ❌ | ❌ | ✅ | ✅ |
| 部门 / 员工 / 客户 / 项目维护 | ❌ | ❌ | ❌ | ✅ |
| 系统参数（读） | ❌ | ❌ | ✅ | ✅ |
| 系统参数（写）/ 用户管理 / 审计日志 | ❌ | ❌ | ❌ | ✅ |
| 统计与预警 | ❌ | ✅ | ✅ | ✅ |

数据隔离在 SQL 层用 `scope_conds()` 落地，不是把全量数据取回来再过滤——列表、详情、统计、导出、预警**全部**套同一套可见范围条件。

登录接口对「用户名不存在」与「密码错误」返回同一句提示，避免账号枚举。

### 分级审批

阈值存在 `setting` 表，管理员可在「系统参数」里改：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `approval_level2_threshold` | 20000 | 报销单金额达到该值时，需要二级审批 |
| `approval_overdue_days` | 7 | 待审批超过该天数进入预警 |
| `large_amount_threshold` | 20000 | 大额报销单预警阈值 |
| `auto_approve_level1` | 0 | 预留：一级自动通过 |

提交时按金额算出 `required_level`，审批人逐级累加 `approved_level`，越级审批会被拒。审批人在账号上带 `approval_level`（1 级 / 2 级），只有级别足够的人才能审到对应层级，且**必须是申请人所在部门**。

---

## 发票收件箱

两条收票通道，共用一个识别与入账管线。

### 通道一：IMAP 直连（推荐）

在「发票收件箱 → 收票邮箱」新增账号（仅管理员），填 IMAP 服务器、端口、邮箱账号与**授权码**：

| 字段 | 说明 |
| --- | --- |
| IMAP 服务器 / 端口 / SSL | 腾讯企业邮 `imap.exmail.qq.com:993`；163 `imap.163.com:993`；QQ `imap.qq.com:993` |
| 收件文件夹 | 默认 `INBOX` |
| 只看最近 N 天 | 与「只收未读」取交集，避免首次接入就把整个邮箱拉一遍 |
| 主题关键词 | 如 `发票,电子发票`，空 = 不过滤 |
| 发件人白名单 | 如 `noreply@fapiao.com,*@tax.cn`，支持 `*` 通配，空 = 不限制 |
| 只收未读 / 收完标记已读 | 控制幂等，避免重复收取 |

点「测试连接」验证连通性，点「立即收票」或工具条的「立即收取全部邮箱」拉取。邮件**附件**是白名单类型（PDF / OFD / XML / XLSX / 图片）时才会进入识别与入账。

> 密码用 `MAIL_SECRET` 派生的密钥做可逆加密（AES 级强度靠 HMAC-SHA256 密钥流实现，纯标准库），
> 落库为 `v1$<nonce>$<cipher>$<mac>`，接口与前端**都不回显明文**，编辑时留空表示不修改。

### 通道二：外部投递

给 Agent Mail、邮件网关或自建脚本用：

```bash
curl -X POST http://<host>:8080/api/inbox/ingest \
  -H "X-Ingest-Token: $INBOX_INGEST_TOKEN" \
  -H "X-Message-Id: <上游邮件的 Message-ID>" \
  -F "subject=【电子发票】xxx 开票通知" \
  -F "sender=noreply@fapiao.com" \
  -F "date=2026-09-15 10:30:00" \
  -F "files=@发票.ofd"
```

`X-Message-Id` 用于幂等去重——上游重试同一封邮件不会重复入账。也可以用登录态的 `Authorization: Bearer <token>` 代替 `X-Ingest-Token`。留空 `INBOX_INGEST_TOKEN` 则关闭该通道。

### 自动识别

识别在**服务端本地**完成，数据不出内网，零外部服务依赖：

| 来源 | 手段 | 抽取字段 |
| --- | --- | --- |
| OFD / XML / XLSX | 标准库 `zipfile` + `ElementTree` 解析电子发票元数据 | 号码、代码、开票日期、价税合计、税额、税率、购销双方名称与税号 |
| PDF（文字版） | 解析字体 `ToUnicode` CMap 还原字形码，再按当前字体逐段解码内容流 | 与 OFD 同级：号码、日期、金额、税额、税率、购销双方名称与税号 |
| 图片 / 扫描件 PDF | **本地 OCR**（RapidOCR，服务端离线跑，数据不出内网）：图片直读；扫描版 PDF 先用 PDFium 栅格化再 OCR，自动纠正 EXIF 方向 | 与文字版同级（标签行解析优先，正则补缺；「购实方→购买方」这类标签错字会自动纠正归属） |
| AI 大模型（可选） | 本地识别**关键字段全空**时，把文件交给已配置的视觉大模型兜底补字段，逐字段标注来源（如 `pdf+ai`） | 同上 |
| 文件名 / 邮件主题 / 正文 | 正则 | 号码、金额、税额、日期、销售方、购买方 |
| 兜底 | 号码仍缺失时以「待识别-xxxxxx」占位入账并标注需人工复核 | — |

> **PDF 这一层值得说明**：从电子税务局下载的数电票 PDF，正文用的是嵌入子集字体
> （Identity-H）编码，内容流里写的是字形码而不是 Unicode。只扫 `(...)` 字面量的话，
> 一张票往往**只能识别出号码和日期**，金额与购销方全部丢失——这正是「手工录入发票
> 信息识别不全」的主因。现在的做法是先解析每个字体的 `ToUnicode` CMap 建立
> 字形码 → 文字 的映射，再跟着 `Tf` 跟踪当前字体逐段解码 `Tj` / `TJ`；中文标签
> 和数值都能还原，且按字符类型决定片段间是否补空格（中文相接直接拼，避免
> 「价税合计」被拆成「价 税 合 计」而匹配不上）。

> **识别层级合并规则**：行级「标签：值」解析 > 段级正则 > OCR 行解析 > AI 兜底，
> 精确来源永远优先于模糊来源，模糊来源只补缺、不覆盖。每个字段都带 `field_sources`
> 来源标注，界面逐字段显示它来自哪一层（票面解析 / OCR / AI 增强），方便人工判断可信度。

本地 OCR 是**可选依赖**（RapidOCR + PDFium，约 300MB）：构建时 `INSTALL_OCR=1` 安装，
`0` 则镜像小很多、图片类发票退回人工补录，识别链路自动降级、不会报错。

每张票返回 `confidence`（0–1）与 `source`（识别来源），界面按置信度显示进度条。

#### 手工入账：识别只是打草稿，人工说了算

「发票收件箱 → 手工入账」是三步走：**选文件 → 识别结果逐字段填进可编辑表单 → 核对补录后入账**。

- 表单字段与发票台账**一一对应**（号码、代码、类型、日期、价税合计、税率、税额、
  购销双方名称与税号），识别到什么就填什么，识别不到的标红「待补录」，人一眼能看出缺哪几项；
- 所有字段**都能改**。人工填过的值一律覆盖识别结果——包括**把字段清空**，
  不会被识别结果又填回去（后端用「未传 = 沿用识别 / 传空 = 人工确认为空」区分，
  所以模型识别错的金额、日期在入账前来得及纠正）；
- 数电票（全电发票）没有发票代码，该字段标「可留空」而非「待补录」，
  不把「本来就没有」误报成「识别失败」；
- 改金额或税率会自动带出税额，规则与台账登记发票一致；
- 号码留空则以「待识别-」占位入账——**收票最忌讳的是识别失败等于票据消失**，
  宁可留一张待补全的记录，也不让票丢了。

---

## AI 智能（可选，对接市面主流大模型）

模型 Key **不写在配置文件里**，由管理员在「AI 设置」页配置，落库前用 `MAIL_SECRET`
派生密钥加密，接口与前端只回显掩码（`**********3456`，只留末 4 位辨认、不暴露长度）。

### 模型接入

- 内置预设：DeepSeek、通义千问、Kimi、智谱、豆包、OpenAI、Azure OpenAI、Ollama、硅基流动；
  任意 **OpenAI 兼容**接口选「自定义」填 Base URL 即可；
- 可建多套配置，按场景分别启用：助手对话 / 识别增强 / 分析评估各用各的模型
  （例如对话用便宜的，识别增强挂视觉模型）；
- 每套配置可单独覆盖温度、超时、最大 tokens、鉴权方式（Bearer / 自定义 Header）、
  附加 Header 与 Query（适配企业网关）；
- 「测试连接」直接向后端发一次真实补全验证连通性，失败给出具体原因（域名不通 / 401 / 模型名错）。

### 智能助手（右下角浮窗）

- **流式对话**，逐字上屏；自动带上**当前页面上下文**（在报销单页问「这张单什么情况」
  不用再描述是哪张）；
- 助手能解析动作意图（建草稿 / 关联发票 / 提交 / 审批…），但**写操作一律先弹
  「待确认操作」卡片**——参数摆出来，人点「执行」才走，且**执行走的是你本人的权限**，
  助手没有任何越权通道；点「忽略」即作废；
- 未配置模型时浮窗显示引导，点「去配置」直达 AI 设置页。

### 助手能操作整个系统（v2.5.0）

助手不只是「会起草报销单」——它有一份**跟着角色走的全系统能力目录**：

- **只读工具 15 个**：报销单/发票/收件箱/主数据/预算/异常预警/统计/用户/系统参数……
- **写动作 36 个**：报销单（建/改/提交/撤回/审批/驳回/删除）、发票（登记/修改/分类/关联/查验/删除）、
  主数据（客户/项目/部门/员工/费用类型的增删改）、预算、账号、系统参数、邮箱账号。

关键行为：

| 行为 | 说明 |
| --- | --- |
| **先查再答** | 问「有哪些异常发票」，助手会真的去调只读工具（`/api/ai/chat` 服务端执行并把结果回灌给模型，最多 4 轮），不再凭空编 |
| **名字 → id** | 说「XTS 客户」「张斌」这种业务名，后端自动解析成 id 并回显在参数里；解析不到会说明 |
| **缺参数先追问** | 例如只说「建一张报销单」没给明细，卡片**不会**弹出来，助手会先问你要什么 |
| **可见 ≠ 可执行** | 目录按角色裁剪（管理员的动作集严格大于财务，财务大于申请人）；即便动作可见，`prepare_write` 仍会逐条校验角色与状态 |
| **写操作必须过确认卡片** | 参数摆在卡片上，人点「执行」才走；**执行走的是点按钮这个人的权限**，助手没有任何越权通道 |

`GET /api/ai/actions` 返回当前登录者可用的完整动作 + 工具目录（含标签、必填项、危险标记），
前端的执行器与它一一对应——`ai_test.py` 会把「后端有、前端没实现」的动作直接判为失败，
避免出现「卡片点下去没反应」。

### 打印与排版（v2.6.0）

- **报销单打印**：详情弹窗 →「打印」，出 A4 正式单据（费用明细、合计**人民币大写**、
  关联发票清单、流转记录、申请人/部门负责人/财务审核/审批人四栏签字区）。
- **发票打印**：台账「打印清单」按勾选优先（没勾就打当前筛选整页）；每行另有「打印」出单张。
  清单合计同样附大写金额。打印走独立窗口（Blob URL），弹窗被拦时自动降级为隐藏 iframe，
  版式与页面主题完全隔离，直接 `window.print()`。
- **排版修缮**：小屏看板 KPI 改两列（不再一条长竖条滚到底）；手机卡片里的省略号截断
  改为自然换行（触屏没有悬停提示，截断等于丢信息）；发票台账操作列**钉在可视区右缘**
  （列多出现内滚时按钮不再被裁掉）；页签溢出改为隐滚动条的横滑；按钮触控高度统一上调。

### 只读分析（权限边界）

模型本身仍只有**只读 + 建议**能力：它自己碰不到数据库，也拿不到绕过权限的路径。
所有写操作都必须经由上面那套「动作 → 确认卡片 → 既有业务接口」的链路，
所以越权与否取决于**点按钮的人**，而不是模型听不听话。

| 能力 | 入口 | 说明 |
| --- | --- | --- |
| AI 兜底识别 | 手工入账 / 收票勾选「AI 增强识别」 | 本地识别关键字段全空时才调模型，省 token |
| 报销单分析 | 报销单详情 →「AI 分析」 | 费用构成、标准比对、异常点，纯文本结论 |
| 审批建议 | 报销单详情（审批人可见） | 风险点清单 + 建议，**不代替人拍板** |
| 记账草稿 | 发票管理 →「AI 记账草稿」 | 勾选发票批量生成报销单草稿，仍需人工确认提交 |

服务端访问模型可走代理（`AI_PROXY`），适配内网不出网环境；`AI_VERIFY_SSL=0`
仅限自签证书的内网网关。每次调用记录模型、耗时与 token 用量，AI 设置页可查。

---

### Jev AI 决策加速器（v2.7.10+，可选；协议于 v2.9.1 修正）

[Jev](https://vercel.com/kb/guide/typesafe-jev-and-ai-sdk)（TypeSafe AI System One）是
**结构化决策模型**（不写文本），比大模型快 200× 便宜 400×（$0.042 / 1M 输入 token），
单次约 0.4s。三类题型：
- **boolean** — 真值概率 0~1（如「是否重复报销」）
- **choice** — 从具名集合里选一个（如「发票类别」「审批路由」）
- **score** — 按有序量表打分（如「异常度 1-5」）

通过 [Vercel AI Gateway](https://vercel.com/docs/ai-gateway) 接入。
⚠️ **前置条件：Vercel 账号需要绑定信用卡**，否则调用一律返回
`403 customer_verification_required`（免费额度也需绑卡后放行）。

**新增 4 个 AI 工具**（位于 `backend/app/ai_tools.py` 末尾）：
| 工具 | 用途 |
| --- | --- |
| `jev_classify_category` | 发票类别快速分类（choice + 8 类） |
| `jev_score_anomaly` | 报销异常打分（score 1-5） |
| `jev_route_approval` | 审批路由建议（choice + 财务/部门/总经理） |
| `jev_check_duplicate` | 重复报销检测（boolean 0-1 概率） |

**三态返回**，调用方据此决定是否采信：

| `source` | 含义 | 处理 |
| --- | --- | --- |
| `jev` | 拿到模型结论 | 直接用 |
| `unavailable` | 没配 key | 退回大模型/规则 |
| `error` | 配了但调用失败 | 退回大模型/规则，**不要采信**空结论 |

**协议要点**（`backend/app/jev_client.py` 模块头部有完整说明，改之前务必先读）：

```
POST https://ai-gateway.vercel.sh/v1/evaluate
Authorization: Bearer vck_…
{
  "model": "typesafe-ai/jev",
  "state": "<要判定的材料>",
  "questions": {                        # 对象（键 = 题 id），不是数组
    "q": {"type": "boolean", "instructions": "…"},
    "q": {"type": "choice",  "instructions": "…", "criteria": {"选项key": "判据"}},
    "q": {"type": "score",   "instructions": "…", "criteria": ["低", "中", "高"]}
  }
}
→ {"answers": {"q": {"probability": 0.93} | {"choice": "…"} | {"score": 3}}}
```

五个易错点（v2.7.9 上线时全部踩过，端点错到恒 404）：端点是 `/v1/evaluate` 而非
`/v1/ai/jev`；`questions` 是对象；问题文本字段是 `instructions`；`choice` 的 `criteria`
是**对象**而 `score` 的 `criteria` 是**数组**；`confidence` 不在 `answers` 里，在
`providerMetadata.typesafe.confidence`。

**配置**：管理员在「系统参数 → AI 设置 → Jev 决策网关」卡片里维护（存 `setting` 表，
5 秒内生效，无需重启）；`.env` 里的同名变量作为 fallback：
```
JEV_API_KEY=vck_xxxxxxxxxxxxxxxxxxxx        # env fallback（也可在界面里填）
JEV_BASE_URL=https://ai-gateway.vercel.sh   # 网关根地址，自动补 /v1/evaluate
JEV_TIMEOUT_SEC=8                           # 单次调用超时（秒）
JEV_PROXY=                                  # 走代理留空（如 http://proxy.local:8080）
JEV_VERIFY_SSL=1                            # 0=自签证书内网网关时跳过校验
```

**排障**：卡片上的「连通性测试」发一次最小 boolean 决策，失败时直接给出
HTTP 状态码、服务端原文、规范化端点、耗时和可读建议（如「需要绑卡」「key 失效」
「可能是 HTTP_PROXY 拦了」）。早期版本把异常全吞掉、只回一句「调用失败」，
排查只能靠猜——v2.9.1 起失败必须留痕。

**实现细节**（`backend/app/jev_client.py`）：
- stdlib `urllib.request`，与 `ai.py` 大模型客户端同风格，**零新依赖**
- 同步阻塞调用（FastAPI 线程池隔离，不影响 async）
- 配置读取有 5s 缓存；`_cfg()` 即使没拿到 db session 也会自己开一个读库，
  避免调用链漏传 session 时**静默退化成「未配置」**
- 离线单测 `backend/jev_test.py`（72 项，用本地假网关钉住协议，不联网）

---

## 手机版（移动端适配，v2.4.0）

同一套代码、同一套接口与权限模型：窗口 ≤820px 自动切换为移动端外壳，手机浏览器直接访问 `http://<部署机>:8080` 即可，无需安装 App。

### 换了什么外壳（业务逻辑一行没动）

- **导航**：侧栏收进抽屉（左上角汉堡按钮）；高频入口固定在底部标签栏（报销 / 看板 / 发票 / 收件箱，其余入口收进「全部」，按角色权限渲染）。
- **表格 → 卡片**：`frontend/js/mobile.js` 从 `<thead>` 读列名自动回填成「左标签 + 右值」的卡片；新增视图自动受益。结构对不上的表保持横向滚动，宁可少变也不猜。
- **工具条**：筛选控件折叠进「筛选」面板，搜索框与主操作按钮留在外面；拖回桌面宽度自动还原。
- **弹窗 → 全屏**、输入框 16px（防 iOS 聚焦缩放）、触控目标 ≥ 38px、刘海屏安全区适配（`viewport-fit=cover` + `env(safe-area-inset-*)`）。
- **没改的**：权限过滤、识别回填、审批流、数据可见范围与桌面完全同源——手机上看到的数据与桌面登录同一账号一模一样。

### 拍照上传

登记发票、发票影像、收件箱手工入账三处上传入口在手机上多一个「拍照」按钮，直接唤起后置摄像头；拍到的照片经 `DataTransfer` 塞回原有文件控件，走与桌面**完全相同**的识别链路（规则解析 → OCR → AI 兜底），不维护第二套上传代码。

### 加到主屏幕（PWA）

`manifest.json` 与图标（192/512/maskable/apple-touch-icon）已就位；iOS Safari「添加到主屏幕」、Android Chrome「安装应用」后以独立窗口打开、无地址栏。内网多为 http，刻意不引入 Service Worker 离线缓存——离线打开一个报销工作台只会看到过期数据，没有收益。

图标由脚本生成，改主色或应用名后重跑即可：

```bash
backend/.venv/bin/python tools/make_icons.py
```

### 手机版验收

```bash
# --mobile：375x812 视口跑完整断言 + 手机外壳专项
# （抽屉、底部标签栏、表格卡片化、筛选折叠、全屏弹窗、拍照入口、断点来回切换回归）
node tools/verify_ui.mjs http://127.0.0.1:8793 /tmp/wb-ui-mobile admin 'Adm1n@2026' 管理员 --mobile
```

最近一次（v2.9.0）：管理员 175/0、财务 151/0、审批人 105/0、申请人 100/0；同实例桌面回归 149/0，互不影响。

---

## 批量删除与数据清理

四个模块支持**勾选批量删除**：发票台账、报销单、费用管理（费用类型/预算/基础数据）、审计日志。危险操作走统一确认框——**必须原样输入确认词**才可提交：

| 操作 | 确认词 | 谁能做 |
| --- | --- | --- |
| 批量删除（各模块） | `批量删除` / `删除发票` / `删除报销单` / `删除审计日志` | 按模块写权限 |
| 清空全部发票 | `清空全部发票` | 财务 / 管理员 |
| 清空待关联散票 | `清空散票` | 财务 / 管理员 |
| 清空全部报销单 | `清空全部报销单` | 仅管理员 |
| 清空收件记录 | `清空收件记录` | 仅管理员 |
| 清空审计日志 | `清空审计日志` | 仅管理员 |

几个刻意的设计：

- **删报销单不删发票**：发票是资产凭证，批量删单时自动解绑，发票本身保留。
- **删发票会清影像文件**：磁盘上的附件一并删除，返回 `files_removed` 计数。
- **能力不足的记录会跳过并给原因**：批量删除返回 `skipped: [{id, code, reason}]`，界面把原因汇总弹出来，而不是整批失败。
- **被业务数据引用的主数据拒绝删除**：如部门下还有员工、费用类型已被明细引用。

### 一键清空业务数据（命令行）

```bash
# 清空业务数据，保留账号、主数据与审批参数
docker compose exec app python -m app.seed --purge

# 连主数据一起清空（账号保留，employee_id 绑定会解开）
docker compose exec app python -m app.seed --purge-all

# 清空后重建演示数据
docker compose exec app python -m app.seed --reset
```

破坏性操作默认要求交互确认，脚本化部署加 `--yes`。

---

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `APP_PORT` | 8080 | 宿主机端口 |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` | workbench / workbench / workbench123 | PostgreSQL 凭据，**上线务必改密码** |
| `DATABASE_URL` | 由上面拼出 | 也可直接给完整连接串；本地开发默认 SQLite |
| `AUTO_MIGRATE` | 1 | 启动时自动 `alembic upgrade head` |
| `SEED_DEMO` | **0** | 是否灌演示数据；生产保持 0 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `ADMIN_NAME` | admin / （空=随机） / 系统管理员 | 初始管理员，仅在库中无管理员时创建 |
| `MAIL_SECRET` | （空） | 邮箱凭据加密主密钥，**生产必填** |
| `INBOX_INGEST_TOKEN` | （空） | 外部收票通道令牌，空=关闭 |
| `DOCS_ENABLED` | 0 | 是否暴露 `/docs`、`/redoc` 交互式文档 |
| `CORS_ORIGINS` | （空） | 跨域白名单，逗号分隔；前后端同容器留空 |
| `UPLOAD_DIR` | `/app/data/uploads` | 发票影像落盘目录（已挂 `reimburse-uploads` 卷） |
| `UPLOAD_MAX_MB` | 10 | 单个影像大小上限 |
| `INSTALL_OCR` | 1 | **构建期**是否安装本地 OCR（RapidOCR+PDFium，约 300MB）；0 = 图片类发票退回人工补录 |
| `OCR_ENGINE` | auto | 运行时 OCR 后端：auto / rapidocr / tesseract / off |
| `OCR_MAX_PDF_PAGES` / `OCR_PDF_SCALE` | 5 / 2.0 | 扫描版 PDF 最多处理页数 / 栅格化倍率 |
| `AI_PROXY` | （空） | 服务端访问模型接口走的代理（如 `http://10.10.10.252:1086`）；模型 Key 在网页「AI 设置」里配，不在这里 |
| `AI_VERIFY_SSL` | 1 | 模型接口 HTTPS 证书校验；仅自签证书的内网网关设 0 |
| `AI_TIMEOUT_SEC` / `AI_MAX_TOKENS` | 60 / 2048 | 模型调用超时与单次最大输出（每套配置可单独覆盖） |
| `SECCOMP_MODE` | unconfined | 老 Docker 引擎兼容，见下文 |
| `PIP_INDEX` | 清华镜像 | 构建期 pip 源 |
| `TZ` | Asia/Shanghai | 时区 |

---

## 部署到局域网

1. 修改 `.env`：

   ```ini
   APP_PORT=8080
   DB_PASSWORD=<换成强密码>
   MAIL_SECRET=<openssl rand -hex 32>
   ADMIN_PASSWORD=<初始密码>
   SEED_DEMO=0                 # 生产必须为 0
   DOCS_ENABLED=0              # 生产建议关闭接口文档
   ```

2. 启动后团队通过 `http://<服务器IP>:8080/` 访问，例如 `http://10.10.10.5:8080/`。

3. 如需从其他机器直连数据库排查问题，放开 `docker-compose.yml` 中 `db` 服务的 `ports` 段（默认只在内网 Docker 网络暴露）。

4. **生产建议**：在前面挂一层 Nginx/网关做 HTTPS 终止与访问控制。应用自身已开启 `X-Frame-Options`、`X-Content-Type-Options: nosniff`、`Referrer-Policy`，全局异常兜底不吐堆栈，`/api/health` 不暴露连接串。

> 容器内服务监听 `0.0.0.0:8000`，宿主机端口由 `APP_PORT` 映射，无需改动应用配置。

---

## 本地开发（不使用 Docker）

后端默认使用 SQLite，无需准备数据库：

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 启动（同时托管 frontend 静态页面，自动执行迁移与管理员初始化）
DATABASE_URL=sqlite:///./data/workbench.db \
ADMIN_USERNAME=admin ADMIN_PASSWORD='Adm1n@2026' \
DOCS_ENABLED=1 \
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

打开 http://localhost:8000 。数据库文件位于 `backend/data/workbench.db`。

也可以直接用封装好的脚本（起停/看日志/重置库）：

```bash
tools/dev_server.sh start            # 默认端口 8791
tools/dev_server.sh status
tools/dev_server.sh logs
tools/dev_server.sh stop
tools/dev_server.sh reset            # 删库重来（下次启动自动迁移并建管理员）
PORT=8792 DB_FILE=/tmp/wb.db tools/dev_server.sh start
```

切换到 PostgreSQL 只需设置环境变量：

```bash
export DATABASE_URL="postgresql+psycopg://user:pass@127.0.0.1:5432/workbench"
```

### 数据库迁移

Alembic 管理表结构，迁移脚本在 `backend/migrations/versions/`：

```bash
cd backend
DATABASE_URL=sqlite:///./data/workbench.db .venv/bin/alembic upgrade head
DATABASE_URL=sqlite:///./data/workbench.db .venv/bin/alembic downgrade -1
# 改完 models.py 后生成新迁移
DATABASE_URL=sqlite:///./data/workbench.db .venv/bin/alembic revision --autogenerate -m "描述"
```

`env.py` 已开启 `render_as_batch`，SQLite 的 `ALTER TABLE` 限制由 Alembic 自动绕开，升降级在两个库上都验证过。

### 从 v1 老库升级（重要）

v1 是用 `Base.metadata.create_all` 建的库，**没有 `alembic_version` 表**。直接跑 `alembic upgrade head` 会因为「表已存在」整条失败。

为此启动时加了一步自动识别与补齐，默认开启，无需人工介入：

1. 检测到「有业务表但没有 `alembic_version`」→ 先把库标记成初始版本 `cc5660459504`；
2. `5f96e014a6eb` 这条迁移负责把 v1 与 v2 的差额**幂等**补上：建 `app_user` / `user_session` / `audit_log` / `invoice_attachment` / `setting` 五张表，加 `reimbursement.approver_id|required_level|approved_level`、`approval_log.level`、`invoice.created_by` 五个字段及相关索引。已存在的表和字段会跳过，所以从零建的新库跑它等于空操作。

```bash
# 升级前务必备份（Postgres 示例）
docker compose exec -T db pg_dump -U workbench -d workbench > backup_$(date +%F_%H%M).sql

# 直接启动即可，迁移在容器启动时自动执行
docker compose up -d --build && docker compose logs -f app | head -30
# 应看到：[migrate] 检测到 v1 旧库 …  [migrate] 数据库结构已升级到最新版本
```

补结构之后，管理员账号会由 `bootstrap()` 自动创建（`ADMIN_USERNAME` / `ADMIN_PASSWORD` 没配就随机生成并打印在启动日志里）。

旧库里的 600+ 条演示单据不会被迁移自动清掉，看够了再清：

```bash
docker compose exec app python -m app.seed --purge --yes
```

`AUTO_STAMP_LEGACY=0` 可关闭自动打标——只有在你有自定义迁移流水线时才需要。

### 演示数据

```bash
.venv/bin/python -m app.seed --reset --months 12 --orders 620 --yes
```

`--months` 控制数据覆盖的月份跨度，`--orders` 控制报销单数量。演示数据**只应在试用/演示环境**使用。

---

## 目录结构

```
reimburse-workbench/
├── Dockerfile                    # 镜像构建（前后端同镜像，非 root 运行）
├── docker-compose.yml            # app + PostgreSQL 编排，含影像数据卷
├── .env.example                  # 环境变量样例
├── backend/
│   ├── requirements.txt
│   ├── alembic.ini               # Alembic 配置（sqlalchemy.url 由 env.py 注入）
│   ├── migrations/               # 迁移脚本（含 render_as_batch，SQLite 兼容）
│   ├── smoke_test.py             # 功能冒烟（80 项，自带数据、带登录态）
│   ├── security_test.py          # 权限与安全验收（94 项）
│   ├── inbox_test.py             # 收票/识别/批量删除验收（78 项）
│   ├── recognize_test.py         # 识别引擎单测（49 项，纯离线）
│   ├── ocr_test.py               # 本地 OCR 单测（75 项，纯离线）
│   ├── ai_test.py                # AI 链路验收（109 项，需 mock_llm.py 起 8899）
│   ├── ui_fixture.py             # UI 验收用最小数据集（含 4 个角色账号）
│   └── app/
│       ├── main.py               # 入口：迁移、初始化、中间件、静态托管、CSV 导出
│       ├── security.py           # 口令哈希、会话、角色守卫、数据可见范围
│       ├── approvals.py          # 分级审批规则与系统参数
│       ├── audit.py              # 审计中间件（写操作统一留痕）
│       ├── mailbox.py            # IMAP 收票（标准库 imaplib/email）
│       ├── recognize.py          # 发票识别引擎（OFD/XML/XLSX/PDF/文本）
│       ├── crypt.py              # 邮箱凭据可逆加密（标准库 HMAC 密钥流）
│       ├── filestore.py          # 影像落盘（随机名、后缀白名单、路径穿越防御）
│       ├── migrate.py            # Alembic 封装（启动自动升级）
│       ├── bootstrap.py          # 初始管理员与默认参数补齐
│       ├── database.py / models.py / schemas.py / serializers.py
│       ├── seed.py               # 演示数据生成 / 清库子命令
│       └── routers/
│           ├── auth.py           # 登录 / 登出 / 改密 / 当前用户
│           ├── admin.py          # 用户、系统参数、审计日志
│           ├── master.py         # 部门/员工/费用类型/客户/项目/预算/明细台账
│           ├── reimbursements.py # 报销单、状态流转、分级审批、批量删除
│           ├── invoices.py       # 发票台账、查验、重复检测、批量删除
│           ├── attachments.py    # 发票影像上传/下载/删除
│           ├── inbox.py          # 收票邮箱、收件记录、识别、导入、外部投递
│           └── stats.py          # 看板聚合与预警（全部套可见范围）
├── frontend/                     # 原生 JS 单页应用，无构建步骤
│   ├── index.html
│   ├── manifest.json             # PWA 清单（加到主屏幕/安装应用）
│   ├── img/                      # PWA 图标（由 tools/make_icons.py 生成）
│   ├── css/app.css
│   ├── css/mobile.css            # 手机版样式（≤820px：抽屉/卡片化/全屏弹窗/安全区）
│   ├── js/{util,auth,api,charts,app,ai}.js
│   ├── js/mobile.js              # 手机版外壳（抽屉、底部标签栏、表格卡片化、拍照桥接）
│   ├── js/views/{dashboard,reimbursements,invoices,inbox,expenses,alerts,users,settings,audit}.js
│   └── vendor/echarts.min.js     # 本地化 ECharts，内网离线可用
├── tools/
│   ├── verify_ui.mjs             # 浏览器 UI 验收（按角色，含登录与权限渲染；--mobile 跑手机视口）
│   ├── make_icons.py             # 生成 PWA 图标（改主色/应用名后重跑）
│   └── dev_server.sh             # 本地起停/日志/重置
└── deploy/backup.sh              # 数据库备份脚本
```

---

## 数据模型

```
AppUser ─┬─< UserSession            (会话：只存 token 哈希)
         └─< AuditLog               (操作审计)

Department ─┬─< Employee >── AppUser.employee_id
            └─< Budget >─┬─ Project >── Customer
ExpenseCategory ─────────┘
      │
      ├─< ReimbursementItem >── Reimbursement ─┬─< ApprovalLog (含审批级数)
      │                          │             └── required_level / approved_level
      └─< Invoice >──────────────┘
              │
              ├─< InvoiceAttachment (影像：磁盘随机名 + 元数据)
              └── MailMessage        (收件记录，Message-ID 唯一)

MailAccount ─< MailMessage ─< Invoice   (IMAP 账号与收票溯源)
AiProvider ─< AiUsage                   (模型配置与调用用量；Key 加密存储)
Setting                                  (系统参数 key-value)
```

| 表 | 说明 |
| --- | --- |
| `app_user` | 账号：用户名唯一、PBKDF2 口令哈希、角色、审批级别、是否须改密 |
| `user_session` | 会话：token 哈希（不存明文）、IP/UA、过期时间、是否已吊销 |
| `audit_log` | 操作审计：操作人、动作、对象、方法/路径、状态码、IP、详情 |
| `reimbursement` | 报销单主表，含金额、状态、审批级别、各节点时间戳 |
| `reimbursement_item` | 费用明细行，金额合计即报销单金额 |
| `approval_log` | 全量流转日志（含审批级数） |
| `invoice` | 发票台账，可关联报销单与明细行，含识别来源与置信度 |
| `invoice_attachment` | 发票影像元数据（磁盘名为随机 hex，原文件名仅作展示） |
| `expense_category` / `budget` / `project` / `customer` / `department` / `employee` | 主数据与预算 |
| `mail_account` | 收票邮箱配置（密码可逆加密存储） |
| `mail_message` | 收件记录，Message-ID 唯一用于幂等去重 |
| `ai_provider` | 大模型配置：厂商预设 / 自定义 Base URL、模型名、场景开关；API Key 加密存储、只回显掩码 |
| `ai_usage` | 模型调用流水：场景、模型、耗时、token 用量（失败也记，便于排查） |
| `setting` | 系统参数（审批阈值、预警天数等） |

金额字段统一使用 `NUMERIC(14,2)`；对外 JSON 一律以数值返回，避免精度转换歧义。

---

## API 一览

所有接口（除 `/api/health` 与 `/api/auth/login`）都需要 `Authorization: Bearer <token>` 或登录 Cookie。

| 分组 | 方法与路径 | 说明 |
| --- | --- | --- |
| 认证 | `POST /api/auth/login` `POST /api/auth/logout` `GET /api/auth/me` `POST /api/auth/password` | 登录 / 登出 / 当前用户 / 改密（改后全端下线） |
| 账号与参数 | `GET/POST/PUT/DELETE /api/users[/{id}]`、`POST /api/users/{id}/reset-password`、`GET/PUT /api/settings[/{key}]` | 用户管理（仅管理员）、系统参数（读：财务+管理员，写：仅管理员） |
| 审计 | `GET /api/audit-logs`、`POST /api/audit-logs/batch-delete`、`POST /api/audit-logs/purge` | 查询 / 批量删除 / 按天数清理（仅管理员） |
| 报销单 | `GET /api/reimbursements`、`GET /api/reimbursements/{id}`、`GET /api/reimbursements/{id}/logs` | 列表（含 `pending_level` 筛选）/ 详情 / 流转日志 |
| 报销单写入 | `POST/PUT/DELETE /api/reimbursements[/{id}]`、`POST /api/reimbursements/batch-delete`、`POST /api/reimbursements/purge-all` | 建/改/删、批量删除、清空全部 |
| 状态流转 | `POST /api/reimbursements/{id}/{submit\|withdraw\|approve\|reject\|pay\|unpay}` | 提交/撤回/通过/驳回/付款/撤销付款（分级校验） |
| 发票 | `GET /api/invoices` `/summary` `/monthly`、`POST/PUT/DELETE /api/invoices[/{id}]` | 台账检索、汇总与重复检测、月度趋势、增改删 |
| 发票操作 | `POST /api/invoices/{id}/check` `/batch-check` `/link`、`POST /api/invoices/batch-delete` `/purge-all` | 查验、关联、批量删除、清空（`?only_unlinked=true` 清散票） |
| 发票影像 | `GET/POST /api/invoices/{id}/attachments`、`GET /api/attachments/{id}/raw`、`DELETE /api/attachments/{id}` | 列表 / 上传 / 鉴权下载 / 删除 |
| 收件箱 | `GET/POST/PUT/DELETE /api/mail-accounts[/{id}]`、`POST /api/mail-accounts/{id}/test` `/sync`、`POST /api/inbox/sync-all` | 邮箱账号（写仅管理员）、测试连接、立即收票、全量收票 |
| 收件箱 | `GET /api/inbox/messages`、`POST /api/inbox/messages/batch-delete` `/purge` | 收件记录查询 / 批量删除 / 清空（清空仅管理员） |
| 识别 | `POST /api/inbox/recognize`（只识别不入库）、`POST /api/inbox/import`（识别并入账） | 手工上传通道 |
| 外部投递 | `POST /api/inbox/ingest` | `X-Ingest-Token` 或 Bearer 鉴权，Message-ID 幂等 |
| AI | `GET /api/ai/status`、`GET/POST/PUT/DELETE /api/ai/providers[/{id}]`、`POST /api/ai/providers/{id}/test` | 状态（含 OCR 引擎探测）、模型配置 CRUD（写仅管理员）、连通性测试 |
| AI 能力 | `POST /api/ai/chat`（SSE 流式）、`POST /api/ai/bookkeeping` `/analyze` `/approval-advice` | 助手对话、记账草稿、报销单分析、审批建议（全部只读或需人工确认） |
| 主数据 | `GET/POST/PUT/DELETE /api/{departments\|employees\|categories\|customers\|projects\|budgets}[/{id}]`、`POST /api/{...}/batch-delete` | CRUD（带引用保护）+ 批量删除 |
| 台账 | `GET /api/items` | 跨单据费用明细台账（套可见范围） |
| 统计 | `GET /api/stats/{overview\|trend\|by-category\|by-group\|by-department\|by-employee\|by-customer\|by-project\|by-month-department\|category-trend\|budget-execution\|alerts}` | 看板聚合与预警（全部套可见范围） |
| 导出 | `GET /api/export/{reimbursements\|invoices\|items}.csv` | 台账导出（含 BOM，Excel 直接打开；同样套可见范围） |

`/api/health` 只返回 `status` / `version` / `database`（up/down）/ `schema_revision`，**不暴露连接串**。

需要交互式文档时设 `DOCS_ENABLED=1`，然后访问 `http://<host>:8080/docs`（生产建议关闭）。

---

## 运维

**备份**

```bash
./deploy/backup.sh                     # 导出到 backups/reimburse_<时间戳>.sql.gz
```

建议加入 crontab，例如每天 02:00 备份：

```cron
0 2 * * * cd /path/to/reimburse-workbench && ./deploy/backup.sh >> logs/backup.log 2>&1
```

备份时别忘了影像卷：`docker run --rm -v reimburse-uploads:/data -v $PWD/backups:/out alpine tar czf /out/uploads_$(date +%Y%m%d).tar.gz -C /data .`

**恢复**

```bash
gunzip -c backups/reimburse_20260918_020000.sql.gz \
  | docker compose exec -T db psql -U workbench -d workbench
```

**清空业务数据 / 重建演示数据**

```bash
docker compose exec app python -m app.seed --purge          # 保留账号与主数据
docker compose exec app python -m app.seed --purge-all      # 连主数据一起清
docker compose exec app python -m app.seed --reset --yes    # 清空后重建演示数据
```

**升级**

```bash
git pull
docker compose up -d --build        # 启动时自动跑 Alembic 迁移
```

**忘记管理员密码**

```bash
docker compose exec app python -m app.bootstrap --list-admin              # 看有哪些管理员
docker compose exec app python -m app.bootstrap --reset-admin             # 重置为随机密码并打印
docker compose exec app python -m app.bootstrap --reset-admin --user admin --password 'NewPass@2026'
```

重置后该账号的**所有在途会话会被吊销**，且下次登录仍会要求改密。该工具刻意只能重置管理员，避免变成「任意改别人密码」的后门。

---

## 测试

后端六套测试 + 两套真实浏览器验收，全部自带数据、不依赖演示数据：

```bash
cd reimburse-workbench

# 1) 识别引擎单测（纯离线，不需要服务）
backend/.venv/bin/python backend/recognize_test.py                  # 49 项

# 2) OCR 单测（纯离线；能力现场合成，无 RapidOCR / tesseract / 中文字体的机器自动降级跳过对应项）
backend/.venv/bin/python backend/ocr_test.py                        # 62 项（+3 取决于环境）

# 3) 功能冒烟（需要先起一个服务）
WB_BASE_URL=http://127.0.0.1:8792 WB_PASSWORD='Adm1n@2026' \
  backend/.venv/bin/python backend/smoke_test.py                    # 80 项

# 4) 权限与安全验收
WB_BASE_URL=http://127.0.0.1:8792 WB_PASSWORD='Adm1n@2026' \
  backend/.venv/bin/python backend/security_test.py                 # 94 项

# 5) 收票 / 识别 / 批量删除验收（需要 INBOX_INGEST_TOKEN）
WB_BASE_URL=http://127.0.0.1:8792 WB_PASSWORD='Adm1n@2026' \
  WB_INGEST_TOKEN=dev-ingest-token \
  backend/.venv/bin/python backend/inbox_test.py                    # 78 项

# 6) AI 链路验收（需要服务 + Mock 大模型；不花真 token）
backend/.venv/bin/python backend/mock_llm.py 8899 &                 # 起在 8899
WB_BASE_URL=http://127.0.0.1:8801 WB_PASSWORD='Adm1n@2026' \
  backend/.venv/bin/python backend/ai_test.py                       # 109 项
# 应用跑在容器里、mock 跑在宿主时（上例的 127.0.0.1 在容器里指的是容器自己）：
#   mock 绑到宿主内网地址，再让 ai_test 把同一个地址告诉应用
#   mock_llm.py 8899 0.0.0.0 &    →    WB_MOCK_LLM=http://<宿主IP>:8899/v1
# 注意 mock 与 ai_test 要在**同一个终端会话**里跑完，否则 mock 会随会话一起被回收。

# 7) 扫码核验接口（v2.8.0；临时库、不依赖服务与凭据）
backend/.venv/bin/python backend/scan_test.py                       # 18 项

# 8) 在线升级（v2.9.0；版本比较 / 配置白名单 / 前置检查 / 克隆重建字段 / manifest 挑包）
backend/.venv/bin/python backend/upgrade_test.py                    # 56 项

# 9) Jev 决策网关（v2.9.1；本地假网关钉住 HTTP 协议，不联网不花 token）
backend/.venv/bin/python backend/jev_test.py                        # 72 项

# 10) 浏览器 UI 验收（需本机 Chrome；按角色跑，断言导航与按钮的可见性）
node tools/verify_ui.mjs http://127.0.0.1:8793 /tmp/wb-ui-admin admin 'Adm1n@2026' 管理员
node tools/verify_ui.mjs http://127.0.0.1:8793 /tmp/wb-ui-app  applicant1 'Passw0rd@1' 申请人
node tools/verify_ui.mjs http://127.0.0.1:8793 /tmp/wb-ui-apr  approver1  'Passw0rd@1' 审批人
node tools/verify_ui.mjs http://127.0.0.1:8793 /tmp/wb-ui-fin  finance1   'Passw0rd@1' 财务

# 11) 前端渲染核验（真实 Chromium 逐页截图 + 交互断言，见 tools/render-check.config.js）
NODE_PATH=<node_modules> node ~/.workbuddy/skills/frontend-render-verify/scripts/render_check.js \
  tools/render-check.config.js
```

UI 验收需要一份最小的真实数据集（含 4 个角色账号与各状态单据）：

```bash
WB_BASE_URL=http://127.0.0.1:8793 WB_PASSWORD='Adm1n@2026' \
  WB_INGEST_TOKEN=ui-fixture-ingest-token \
  backend/.venv/bin/python backend/ui_fixture.py
```

最近一次结果（v2.9.3）：这一版只动了三处——Dockerfile 里 libdmtx 的安装方式、
`print_qr` 的导入方式、`/api/health` 多一个 `datamatrix` 字段，所以只重跑了相关套件：
冒烟 80/0、安全 94/0、扫码接口 18/18、在线升级 56/0（含「从 manifest 里按架构挑镜像包」），
外加 DM 打印渲染探针 `tools/dm_probe.cjs` 8/8（免登录真调 `WB.print`，断言 DM 图
`naturalWidth>0`）。其余套件（收件箱 / 识别 / OCR / AI / Jev / UI）本次未改动，沿用
v2.9.2 的结论：收件箱 78/0、识别 49/0、OCR 62/0（3 项因镜像不带 tesseract / 无中文字体
显式跳过）、AI 链路 109/0、Jev 网关 72/0；
渲染核验 10 个页面 0 控制台错误（图表 9/9 画出、控件样式一致、弹窗与 AI 浮窗交互正常）；
另有打印专项核验 `tools/print_audit.cjs`（报销单/清单/单张三种弹窗 9 项断言）与
排版审计 `tools/layout_audit.cjs`（10 页面 × 桌面/手机双视口，横向溢出 0、文字裁切 0）。
此外 v2.9.3 的 libdmtx 改动做了**跨架构对照实验**（都在 QEMU 模拟的 arm64 上跑）：
同一段旧写法会「装成功」但下一层 `import pylibdmtx` 时抛
`Unable to find dmtx shared library`（与线上故障逐行一致）；换成新写法后该步打印
`libdmtx 离线安装完成（aarch64 → aarch64-linux-gnu）`、`ldconfig -p` 反查通过、构建继续往下走。
arm64 镜像的 OCR 依赖在模拟环境下安装过慢（一小时仍未装完），**没有**在本地完整构建；
目标机原生构建含 OCR 约 5-15 分钟即可完成。

UI 验收按角色跑（桌面视口）：管理员 149/0、财务 125/0、审批人 81/0、申请人 76/0；
手机视口（--mobile）：管理员 175/0、财务 151/0、审批人 105/0、申请人 100/0。
每个角色的用例数不同是因为导航与按钮可见性按权限过滤——角色越权即是缺陷。
控制台断言只覆盖「会话进行中」：登出那一下会有几条 401（`POST /api/auth/logout` 已把 token
在服务端吊销，而在途请求还带着它回来），脚本把它们单独打出来但不计失败。
环境里若配了可兜底的识别增强模型，拍照件的「逐项标待补录」会不适用，
该条会显式记为 **未验证**（不计入通过），不会静默混进绿色结果。
渲染核验同理：实例没配模型时，「AI 对话/流式/关闭」三条记为未验证，
只验「浮窗退化成配置引导」这一态，不会把「没配模型」误报成前端故障。

UI 验收会真实驱动 Chrome 渲染每个视图，断言 DOM 与图表渲染结果、遍历交互流程（登录与错误密码、账号菜单、详情弹窗、子页签、重复检测、收件箱三页签、**登记发票上传识别回填**、**手工入账的识别表单可改可补**、**批量删除的危险确认框校验**、影像上传与预览）、校验四个角色的导航与按钮可见性、采集控制台报错并输出截图。

> 上传类的验收走 CDP 的 `DOM.setFileInputFiles` 真实投喂文件（`tools/verify_ui.mjs` 会自己生成 XML 数电票与拍照件两张样本），
> 因此识别回填、待补录标记这些行为是**在浏览器里真跑出来的**，不是断言源码写没写。

后端三套测试在 SQLite（本地 dev）与 PostgreSQL（Docker 容器）下均全绿。

### 生产环境只读体检

上线后想确认「页面都能打开、没有 JS 报错」，又不希望巡检本身往真实库里写数据时，加 `--readonly`：

```bash
node tools/verify_ui.mjs http://127.0.0.1:8080 /tmp/wb-prod admin '你的管理员密码' 管理员 --readonly
```

该模式只做四件事：登录 → 检查导航与权限是否吻合 → 按角色逐个点开每个入口并断言渲染正常（空库时校验空状态文案）→ 汇总控制台报错，并输出截图。**不会调用任何写接口**，所以可在生产库上安全执行。首次登录若弹出强制改密框，脚本只从 DOM 上摘掉它，不会真的改密码。

最近一次生产巡检：19/0（9 个入口全部渲染正常、无控制台报错）。

---

## Docker 引擎兼容性（重要）

本项目的 `docker-compose.yml` 里给 `app` 服务加了 `security_opt: seccomp=unconfined`，这不是随手加的，是为了绕开**老版本 Docker 引擎的一个致命坑**：

**现象**：容器能启动、种子数据能灌进去，但随后：

- 所有需要访问数据库的接口返回 **500**；
- 容器**反复重启**（`docker inspect` 看 `ExitCode=139`，即 SIGSEGV）；
- 构建镜像时 `pip install` 报 `RuntimeError: can't start new thread`。

**根因**：Docker Engine **低于 20.10.10** 的默认 seccomp 白名单里没有 `clone3` 这个系统调用，而且返回的是 `EPERM` 而不是 `ENOSYS`。Debian bookworm/trixie（glibc ≥ 2.34）的 `pthread_create` 会优先走 `clone3`，收到 EPERM 后**不会回退**到老的 `clone(2)`，于是线程一律创建失败：

- FastAPI 的同步端点（`Depends(get_db)`）靠 anyio 线程池执行 → 全部 500；
- uvicorn 默认的 uvloop 在被拒绝的线程环境下会直接段错误 → 容器重启循环；
- pip 的进度条要起刷新线程 → 构建失败。

一句话验证（在容器里跑，第一条就报错说明中招了）：

```bash
docker run --rm python:3.11-slim python -c "import threading; threading.Thread(target=lambda: None).start(); print('ok')"
```

再看引擎版本：

```bash
docker version --format '{{.Server.Version}}'      # < 20.10.10 就是嫌疑对象
```

**三种解法**，按推荐程度排序：

1. **升级 Docker Engine / Docker Desktop 到 20.10.10 以上**（推荐，治本）。升级后把 `docker-compose.yml` 里的 `security_opt` 两行删掉，恢复默认沙箱。
2. **保留本项目的默认配置**：`seccomp=unconfined`。内网单租户的内部工具场景下可以接受，但它确实放宽了容器的系统调用沙箱。
3. **换成 Alpine 基础镜像**：`docker compose build --build-arg PYTHON_IMAGE=python:3.11-alpine`。musl 的 `pthread_create` 不用 `clone3`，因此完全不受影响，也不牺牲沙箱；代价是部分依赖需要 musl 版 wheel，构建时间可能变长。

`SECCOMP_MODE` 可以在 `.env` 里调整（`unconfined` / `default`，或指向自定义 profile 的路径）。

---

## 安全加固清单

| 项 | 做法 |
| --- | --- |
| 口令存储 | PBKDF2-HMAC-SHA256（`hashlib.pbkdf2_hmac`），带随机盐与 `compare_digest` 防时序；零外部依赖 |
| 口令强度 | 至少 8 位且含字母/数字/符号中的两类，前后端双重校验 |
| 会话 | token 只存 SHA-256 哈希；支持过期、强制下线；改密后吊销该用户全部会话；停用账号立即失效 |
| 账号枚举 | 用户名不存在与密码错误返回同一句提示；登录成功/失败都记审计 |
| 数据隔离 | SQL 层 `scope_conds()`，列表/详情/统计/导出/预警全链路生效 |
| 越权防护 | 角色守卫写在后端依赖里，前端隐藏按钮只是体验优化 |
| 影像安全 | 磁盘名随机 hex + 后缀白名单；落盘后二次校验路径在 `UPLOAD_DIR` 内；下载带 `nosniff` |
| 邮箱凭据 | `MAIL_SECRET` 派生的密钥流可逆加密；接口与前端均不回显明文 |
| 外发通道 | 外部投递通道需 `X-Ingest-Token` 或登录态，且支持关闭 |
| 响应头 | `X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy` |
| 跨域 | 默认不放行任何跨域来源，需显式配置 `CORS_ORIGINS` |
| 错误处理 | 全局异常兜底返回统一文案，不吐堆栈；`/api/health` 不暴露连接串 |
| 危险操作 | 清空类接口要求后端二次校验确认词，不能只靠前端弹窗 |
| 运行身份 | 容器以 UID/GID `10001:10001` 非 root 运行 |

---

## 已知边界与后续可扩展

- **发票查验为内置规则校验**：目前校验号码长度、金额、日期、销方信息完整性并检测重复号码。真实场景需对接国家税务总局全国增值税发票查验平台或第三方查验服务，接口形状（`POST /api/invoices/{id}/check`）已预留，替换实现即可。
- **OCR 只认清晰的印刷体**：拍照件、截图、扫描版 PDF 已由本地 RapidOCR 离线识别（`INSTALL_OCR=1` 时内置），印刷质量的票基本能拿到全部字段；但**严重倾斜、强反光、手写体**仍可能识别不出——这类票照旧走「待补录」人工兜底，识别层永远不猜。识别是纯本地推理（约 300MB 依赖），不引入外部服务，不破坏「内网离线可用」的约定；不装 OCR 的镜像自动降级为图片类不识别。
- **AI 依赖可达的模型端点**：AI 能力（助手 / 兜底识别 / 分析 / 审批建议）需要网络可达你配置的模型服务（公网 API 或内网网关均可，支持代理）。没配模型时相关入口自动隐藏或降级为本地识别，其余功能完全不受影响；助手永远**只读 + 建议**，写操作必须人确认。
- **不支持逐单据自定义审批人**：审批人按「部门 + 审批级别」规则确定，不做单点指派。需要灵活审批链时可在 `reimbursements.py` 的 `approve` 中接入审批人表。
- **收票为手动触发或外部调度**：不带内置定时轮询（避免容器内起后台线程）。需要定时收票时用宿主机 cron 调 `POST /api/inbox/sync-all`，或让邮件网关推 `POST /api/inbox/ingest`。
- **前端为原生 JS**：零构建步骤，便于内网直接改。若规模继续扩大，可平滑迁移到 Vite + Vue/React。
- **单 worker**：容器固定 `--workers 1`（启动期要跑迁移与初始化，多 worker 会并发冲突）。需要横向扩展时把初始化拆成独立一次性任务。
