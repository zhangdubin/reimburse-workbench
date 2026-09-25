/**
 * 前端 UI 冒烟测试：用本机 Chrome (CDP) 真实渲染工作台，
 * 逐个视图断言 DOM 渲染结果、采集控制台报错并截图。
 *
 * 生产化改造后前端需要登录，本脚本会先用给定账号登录，
 * 再按**该角色应有的可见视图集合**逐一校验（角色越权即是缺陷）。
 *
 * 用法：
 *   node tools/verify_ui.mjs [baseUrl] [outDir] [user] [pass] [role]
 *   例：
 *   node tools/verify_ui.mjs http://127.0.0.1:8793 /tmp/wb-ui admin Adm1n@2026 管理员
 *   node tools/verify_ui.mjs http://127.0.0.1:8793 /tmp/wb-ui-app applicant1 Passw0rd@1 申请人
 *
 * 依赖：本机 Google Chrome、Node 18+（内置 WebSocket）
 */

import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync, rmSync, existsSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

const BASE = process.argv[2] || 'http://127.0.0.1:8791';
const OUT = process.argv[3] || '/tmp/wb-ui';
const USER = process.argv[4] || process.env.WB_UI_USER || 'admin';
const PASSWORD = process.argv[5] || process.env.WB_UI_PASS || 'Adm1n@2026';
const ROLE = process.argv[6] || process.env.WB_UI_ROLE || '管理员';

// 上传测试用样本图（1x1 PNG），不存在就现造一张
const SAMPLE = process.env.WB_UI_SAMPLE || '/tmp/wb-ui-sample.png';
const PNG_1PX =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==';
if (!existsSync(SAMPLE)) writeFileSync(SAMPLE, Buffer.from(PNG_1PX, 'base64'));

// 手工入账验收用的两张样本：一张字段齐全的数电票（XML 形式，Node 直接写得出来），
// 一张识别不出任何内容的拍照件。用来分别验证「识别成功」与「需要人工补录」两条路径。
const INVOICE_SAMPLE = process.env.WB_UI_INVOICE || '/tmp/wb-ui-invoice.xml';
const PHOTO_SAMPLE = process.env.WB_UI_PHOTO || '/tmp/wb-ui-photo.jpg';
if (!existsSync(INVOICE_SAMPLE)) {
  writeFileSync(
    INVOICE_SAMPLE,
    `<?xml version="1.0" encoding="UTF-8"?>
<Invoice>
  <InvoiceNumber>24417000000077889911</InvoiceNumber>
  <IssueTime>2026-09-12</IssueTime>
  <SellerName>深圳市云图科技有限公司</SellerName>
  <SellerIdNum>91440300MA5F1234XA</SellerIdNum>
  <BuyerName>深圳市智联云创科技有限公司</BuyerName>
  <BuyerIdNum>91440300MA5G5678XB</BuyerIdNum>
  <TotalAmtWithTax>2380.00</TotalAmtWithTax>
  <TotalTax>273.81</TotalTax>
  <TaxRate>13</TaxRate>
</Invoice>`,
    'utf8'
  );
}
if (!existsSync(PHOTO_SAMPLE)) {
  writeFileSync(
    PHOTO_SAMPLE,
    Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe0]), Buffer.alloc(2048), Buffer.from([0xff, 0xd9])])
  );
}

const PORT = 9333 + (process.pid % 200);
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PROFILE = `/tmp/wb-chrome-${process.pid}`;

const VIEW_TITLES = {
  dashboard: '统计看板',
  reimbursements: '报销单管理',
  invoices: '发票管理',
  scan: '扫码核验',
  inbox: '发票收件箱',
  expenses: '费用管理',
  alerts: '异常与预警',
  users: '用户管理',
  settings: '系统参数',
  ai_settings: 'AI 设置',
  audit: '操作审计',
};

/* 各角色应看到的导航入口——与后端权限矩阵一一对应。
   注意 ai_settings 的权限位是 admin.ai，只有管理员（'*'）持有，
   财务虽然有 settings.view 但看不到 AI 设置——别把它混进「系统参数」。
   scan（扫码核验）对所有登录用户开放，数据范围由后端按角色裁剪。 */
const ROLE_VIEWS = {
  管理员: ['dashboard', 'reimbursements', 'invoices', 'scan', 'inbox', 'expenses', 'alerts', 'users', 'settings', 'ai_settings', 'audit'],
  财务: ['dashboard', 'reimbursements', 'invoices', 'scan', 'inbox', 'expenses', 'alerts', 'settings'],
  审批人: ['dashboard', 'reimbursements', 'invoices', 'scan', 'expenses', 'alerts'],
  申请人: ['dashboard', 'reimbursements', 'invoices', 'scan', 'expenses'],
};

const EXPECT = ROLE_VIEWS[ROLE] || ROLE_VIEWS['申请人'];

/* 只读体检模式：只登录、翻页、看控制台，绝不调用任何写接口。
   生产环境巡检用，避免在真实库里留下测试数据。
   用法：node tools/verify_ui.mjs <base> <out> <user> <pass> <role> --readonly */
const READONLY = process.argv.includes('--readonly') || process.env.WB_UI_READONLY === '1';

/* 手机视口模式：375x812（iPhone 13/14 逻辑分辨率）。
   断点 820px 取的是「平板竖屏以下都算手机」，375 是最严格的下限场景——
   它能过，更宽的手机只会更宽松。 */
const MOBILE = process.argv.includes('--mobile') || process.env.WB_UI_MOBILE === '1';
const VIEW_W = MOBILE ? 375 : 1680;
const VIEW_H = MOBILE ? 812 : 1050;

mkdirSync(OUT, { recursive: true });

/* ---------------- CDP 客户端 ---------------- */
class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.reqs = new Map();
    this.logs = [];
    this.errors = [];
    this.dialogs = [];
    this.closed = null;
    // Chrome/渲染进程中途崩掉时，WebSocket 直接断开，之后再发什么都没回应，
    // 表现就是「脚本卡在某一节不动」。这里把断线立刻反映成明确错误。
    ws.addEventListener('close', () => {
      this.closed = 'WebSocket closed';
      for (const [, p] of this.pending) p.reject(new Error('Chrome 连接已断开（浏览器可能已崩溃）'));
      this.pending.clear();
    });
    ws.addEventListener('error', () => {
      this.closed = this.closed || 'WebSocket error';
    });
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
        return;
      }
      if (msg.method === 'Runtime.consoleAPICalled') {
        const text = (msg.params.args || []).map((a) => a.value ?? a.description ?? '').join(' ');
        if (msg.params.type === 'error') this.errors.push(text);
        this.logs.push(`[${msg.params.type}] ${text}`);
      }
      if (msg.method === 'Runtime.exceptionThrown') {
        const d = msg.params.exceptionDetails;
        this.errors.push(d.exception?.description || d.text || 'unknown exception');
      }
      if (msg.method === 'Log.entryAdded' && msg.params.entry.level === 'error') {
        this.errors.push(msg.params.entry.text);
      }
      // 401/403 单独记下 URL：浏览器只说「Failed to load resource: 401」，
      // 光凭这句话查不出是哪个接口在越权，得把地址带上。
      // 再加上发起方（initiator 的调用栈首帧），直接指到是哪个 js 文件哪一行发的请求——
      // 排查「登出后还在打带鉴权的接口」这类问题时，没有它就只能靠猜。
      if (msg.method === 'Network.requestWillBeSent') {
        const p = msg.params;
        this.reqs.set(p.requestId, { url: p.request?.url || '', initiator: p.initiator });
      }
      if (msg.method === 'Network.responseReceived') {
        const st = msg.params.response?.status;
        if (st === 401 || st === 403) {
          const u = (msg.params.response?.url || '').replace(/^https?:\/\/[^/]+/, '');
          const req = this.reqs.get(msg.params.requestId);
          const frame = req?.initiator?.stack?.callFrames?.[0];
          const from = frame
            ? `${String(frame.url).replace(/^https?:\/\/[^/]+/, '')}:${frame.lineNumber + 1}`
            : req?.initiator?.type || '';
          this.errors.push(`HTTP ${st} ${msg.params.type || ''} ${u}${from ? ` ← 发起于 ${from}` : ''}`);
        }
      }
      // 原生 alert/confirm 会**卡住渲染主线程**，Runtime.evaluate 一律等到超时，
      // 现象是「跑到某一节突然 CDP timeout」，很难查。这里一律自动确认并记一笔，
      // 让它以一条明确的失败暴露出来，而不是把整个验收挂死。
      if (msg.method === 'Page.javascriptDialogOpening') {
        this.dialogs.push(`${msg.params.type}: ${msg.params.message}`);
        this.errors.push(`弹窗阻塞：${msg.params.type} ${msg.params.message}`);
        this.ws.send(JSON.stringify({
          id: ++this.id, method: 'Page.handleJavaScriptDialog',
          params: { accept: true },
        }));
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      if (this.closed) {
        reject(new Error(`Chrome 连接已断开，无法执行 ${method}：${this.closed}`));
        return;
      }
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP timeout: ${method}`));
        }
      }, 30000);
    });
  }
  async eval(expression) {
    const r = await this.send('Runtime.evaluate', {
      expression: `(() => { try { return JSON.stringify(${expression}); } catch (e) { return JSON.stringify({__err: String(e)}); } })()`,
      awaitPromise: true,
      returnByValue: true,
    });
    const v = r.result?.value;
    return v === undefined ? undefined : JSON.parse(v);
  }
  async waitFor(expression, { timeout = 15000, label = expression } = {}) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      if (await this.eval(expression)) return true;
      await sleep(180);
    }
    throw new Error(`等待超时: ${label}`);
  }
}

/* ---------------- 启动 Chrome ---------------- */
async function launch() {
  rmSync(PROFILE, { recursive: true, force: true });

  // 运行环境可能注入了 HTTP_PROXY，会影响 Chrome 访问回环地址与 CDP 端口
  const env = { ...process.env };
  for (const k of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']) {
    delete env[k];
  }
  env.NO_PROXY = '127.0.0.1,localhost';

  const proc = spawn(
    CHROME,
    [
      '--headless=new',
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${PROFILE}`,
      '--no-sandbox',
      '--disable-dev-shm-usage',
      '--in-process-gpu',
      '--disable-gpu',
      '--no-proxy-server',
      '--remote-allow-origins=*',
      '--no-first-run',
      '--no-default-browser-check',
      '--hide-scrollbars',
      '--window-size=1680,1050',
      'about:blank',
    ],
    { stdio: 'ignore', detached: false, env }
  );

  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await r.json();
      const page = list.find((t) => t.type === 'page');
      if (page?.webSocketDebuggerUrl) return { proc, wsUrl: page.webSocketDebuggerUrl };
    } catch (e) {}
    await sleep(300);
  }
  proc.kill();
  throw new Error('Chrome 启动失败');
}

/* ---------------- 断言工具 ---------------- */
let PASS = 0;
let FAIL = 0;
const failures = [];
const skips = [];

/* 记一条「本轮没验到」的断言。不计入 PASS，汇总里点名——
   绿了但没测到，比红了更危险。 */
function skip(label, why) {
  skips.push(`${label}（${why}）`);
  console.log(`  \x1b[33mSKIP\x1b[0m ${label}（${why}）`);
}

function assert(label, cond, detail = '') {
  if (cond) {
    PASS++;
    console.log(`  \x1b[32mPASS\x1b[0m ${label}${detail ? ' | ' + detail : ''}`);
  } else {
    FAIL++;
    failures.push(label);
    console.log(`  \x1b[31mFAIL\x1b[0m ${label}${detail ? ' | ' + detail : ''}`);
  }
}

/* ---------------- 主流程 ---------------- */
let proc = null;
let ws = null;
try {
  const launched = await launch();
  proc = launched.proc;
  ws = new WebSocket(launched.wsUrl);
  await new Promise((res, rej) => {
    ws.addEventListener('open', res);
    ws.addEventListener('error', rej);
  });
} catch (e) {
  console.error('\x1b[31m无法启动浏览器:\x1b[0m', e.message);
  console.error('请确认已安装 Google Chrome，且未被安全策略限制。');
  if (proc) proc.kill('SIGKILL');
  process.exit(1);
}
const cdp = new CDP(ws);

await cdp.send('Runtime.enable');
await cdp.send('Log.enable');
await cdp.send('Page.enable');
await cdp.send('Network.enable');
await cdp.send('Emulation.setDeviceMetricsOverride', {
  width: VIEW_W, height: VIEW_H, deviceScaleFactor: MOBILE ? 2 : 1, mobile: MOBILE,
});

console.log(`\n\x1b[1m角色：${ROLE}（${USER}） · 目标：${BASE}\x1b[0m`);

try {
  /* ---------------- 登录 ---------------- */
  console.log('\n\x1b[1m== 登录页 ==\x1b[0m');
  // 先清掉可能残留的本地会话，确保从登录页开始
  await cdp.send('Page.navigate', { url: BASE + '/' });
  await sleep(600);
  await cdp.eval(`localStorage.clear(); sessionStorage.clear();`);
  await cdp.send('Page.navigate', { url: BASE + '/' });

  await cdp.waitFor(`document.querySelector('#login-root #lg-form')`, { label: '登录页渲染' });
  const lg = await cdp.eval(`({
    hasUser: !!document.querySelector('#lg-user'),
    hasPass: !!document.querySelector('#lg-pass'),
    passType: (document.querySelector('#lg-pass')||{}).type,
    appHidden: getComputedStyle(document.querySelector('#app')).display === 'none',
    status: (document.querySelector('#lg-status')||{}).textContent,
  })`);
  assert('登录页渲染用户名/密码输入', lg.hasUser && lg.hasPass, lg.status || '');
  assert('密码框类型为 password', lg.passType === 'password');
  assert('未登录时主界面隐藏', lg.appHidden === true);
  const lshot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  writeFileSync(`${OUT}/00-login.png`, Buffer.from(lshot.data, 'base64'));

  // 故意用错密码，验证错误提示
  await cdp.eval(`(() => {
    document.querySelector('#lg-user').value = ${JSON.stringify(USER)};
    document.querySelector('#lg-pass').value = 'definitely-wrong';
    document.querySelector('#lg-submit').click();
  })()`);
  await cdp.waitFor(`document.querySelector('#lg-error') && document.querySelector('#lg-error').style.display !== 'none'`, {
    timeout: 8000, label: '错误提示',
  }).catch(() => {});
  const errText = await cdp.eval(`(document.querySelector('#lg-error')||{}).textContent || ''`);
  assert('错误密码给出提示', /错误|失败|用户名/.test(errText), errText);

  // 这一步会故意打出一条 401，作为基线忽略掉，避免污染最终的控制台检查
  const errBaseline = cdp.errors.length;

  /* 阶段快照：一旦最终控制台检查挂了，用它一眼看出报错发生在哪一段
     （「登录后无报错」是个汇总断言，没有分段信息就只能靠猜）。 */
  const phases = [];
  const phase = (label) => phases.push({ label, n: cdp.errors.length });

  // 正确登录
  await cdp.eval(`(() => {
    document.querySelector('#lg-pass').value = ${JSON.stringify(PASSWORD)};
    document.querySelector('#lg-submit').click();
  })()`);
  await cdp.waitFor(`document.querySelector('#app') && getComputedStyle(document.querySelector('#app')).display !== 'none'`, {
    label: '登录后进入主界面',
  });

  // 管理员代设密码的账号首次登录会被要求改密；这里只做识别与关闭，
  // 不把它当成缺陷（该行为已在 security_test 里单独验证）。
  await sleep(900);
  const forced = await cdp.eval(`(() => {
    const h = document.querySelector('.modal-head h3');
    const t = h ? h.textContent : '';
    if (/修改初始密码/.test(t)) { document.querySelector('.modal-x').click(); return t; }
    return '';
  })()`);
  if (forced) {
    console.log(`  \x1b[33m注意\x1b[0m 触发了首次登录强制改密弹窗，已关闭：${forced}`);
    await sleep(400);
  }

  /* ---------------- 首屏 ---------------- */
  console.log('\n\x1b[1m== 启动与首屏 ==\x1b[0m');
  await cdp.waitFor(
    `document.querySelector('#page-title') && document.querySelector('#page-title').textContent !== '加载中…'`,
    { label: '首屏渲染完成' }
  );

  const boot = await cdp.eval(`({
    title: document.querySelector('#page-title').textContent,
    navKeys: [...document.querySelectorAll('.nav-item')].map(e => e.dataset.key),
    navCount: document.querySelectorAll('.nav-item').length,
    navGroups: [...document.querySelectorAll('.nav-group')].map(e => e.textContent),
    dbChip: document.querySelector('#db-chip').textContent,
    userChip: (document.querySelector('#user-chip')||{}).textContent || '',
    roleTag: (document.querySelector('#user-chip .role-tag')||{}).textContent || '',
    kpiCount: document.querySelectorAll('.kpi').length,
    charts: document.querySelectorAll('canvas').length,
    token: !!localStorage.getItem('wb.token'),
  })`);
  assert('登录后 token 已持久化', boot.token === true);
  assert('顶栏显示当前用户与角色', boot.userChip.includes('管理员') || boot.userChip.includes(ROLE) || boot.roleTag.length > 0, `${boot.userChip.trim()}`);
  assert('后端连接状态已显示', !/连接中|未连接/.test(boot.dbChip), boot.dbChip);

  const expectedNav = EXPECT.slice().sort().join(',');
  assert(
    `${ROLE} 导航入口与权限一致（${EXPECT.length} 项）`,
    boot.navKeys.slice().sort().join(',') === expectedNav,
    `实际 ${boot.navKeys.join('/')}`
  );
  // 「系统管理」分组的可见性 = 该角色是否至少有一个系统类入口
  // （管理员全有、财务只有「系统参数」，审批人/申请人一个都没有 → 分组不出现）
  const expectSysGroup = EXPECT.some((k) => ['users', 'settings', 'audit'].includes(k));
  assert(
    expectSysGroup ? '系统管理分组出现（该角色有系统类入口）' : '不出现越权导航分组',
    boot.navGroups.includes('系统管理') === expectSysGroup,
    boot.navGroups.join('/')
  );
  // 用户管理与操作审计是管理员独占入口，其他角色绝不能出现在导航里
  if (ROLE !== '管理员') {
    assert(
      '非管理员不出现用户管理/操作审计/AI设置入口',
      !boot.navKeys.includes('users') && !boot.navKeys.includes('audit') && !boot.navKeys.includes('ai_settings'),
      boot.navKeys.join('/')
    );
  }
  // 发票收件箱属于财务/管理员（inbox.view），其他角色不可见
  if (!['财务', '管理员'].includes(ROLE)) {
    assert('非财务/管理员不出现发票收件箱入口', !boot.navKeys.includes('inbox'), boot.navKeys.join('/'));
  }

  /* ---------------- 手机版外壳专项（--mobile） ----------------
     与桌面断言同源跑完首屏权限检查后，这里验「外壳换没换对」：
     抽屉、底部标签栏、卡片化、筛选折叠、全屏弹窗、拍照入口、断点回归。
     桌面模式下整段跳过——避免同一断言在两种视口下语义混乱。 */
  if (MOBILE) {
    console.log('\n\x1b[1m== 手机版外壳 ==\x1b[0m');

    const shell = await cdp.eval(`(() => {
      const vis = (sel) => { const n = document.querySelector(sel); return !!n && getComputedStyle(n).display !== 'none'; };
      const tabs = [...document.querySelectorAll('#mtabbar .mtab')].map(e => ({
        key: e.dataset.key || '', more: !!e.dataset.more, active: e.classList.contains('active'),
      }));
      return {
        navBtn: vis('.m-nav-btn'),
        tabbar: vis('#mtabbar'),
        tabs,
        overflowX: document.documentElement.scrollWidth - window.innerWidth,
        /* 首屏 hash 可能为空（默认视图不写 hash），以侧栏高亮项为准 */
        cur: (document.querySelector('#nav .nav-item.active') || {}).dataset
          ? document.querySelector('#nav .nav-item.active').dataset.key : '',
        navItems: document.querySelectorAll('#nav .nav-item').length,
      };
    })()`);
    assert('手机版 · 汉堡导航按钮显示', shell.navBtn === true);
    assert('手机版 · 底部标签栏显示', shell.tabbar === true);
    assert('手机版 · 底部页签与角色权限一致',
      shell.tabs.filter((t) => !t.more).every((t) => EXPECT.includes(t.key)) &&
      shell.tabs.filter((t) => !t.more).length === Math.min(4, EXPECT.length),
      shell.tabs.map((t) => t.key + (t.more ? '(全部)' : '')).join('/'));
    assert('手机版 · 当前页签高亮', shell.tabs.some((t) => t.active && t.key === shell.cur),
      `active=${(shell.tabs.find((t) => t.active) || {}).key} vs cur=${shell.cur}`);
    assert('手机版 · 抽屉里的导航条目数与桌面一致', shell.navItems === EXPECT.length, `${shell.navItems}/${EXPECT.length}`);
    assert('手机版 · 页面无横向溢出', shell.overflowX <= 1, `超出 ${shell.overflowX}px`);

    /* ---- 抽屉 ---- */
    await cdp.eval(`document.querySelector('#m-nav-btn').click()`);
    await sleep(500);
    const drawer = await cdp.eval(`(() => {
      const vis = (sel) => { const n = document.querySelector(sel); return !!n && getComputedStyle(n).display !== 'none'; };
      return {
        open: document.body.classList.contains('m-nav-open'),
        closeBtn: vis('#m-nav-close'),
        backdrop: (() => { const s = getComputedStyle(document.querySelector('#m-backdrop')); return s.pointerEvents === 'auto'; })(),
        firstItem: (document.querySelector('#nav .nav-item') || {}).dataset ? document.querySelector('#nav .nav-item').dataset.key : null,
      };
    })()`);
    assert('手机版 · 点汉堡展开抽屉', drawer.open === true);
    assert('手机版 · 抽屉内关闭键可见', drawer.closeBtn === true);
    assert('手机版 · 遮罩可拦截点击', drawer.backdrop === true);
    const mshot0 = await cdp.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(`${OUT}/m-drawer.png`, Buffer.from(mshot0.data, 'base64'));
    await cdp.eval(`document.querySelector('#m-backdrop').click()`);
    await sleep(450);
    assert('手机版 · 点遮罩收起抽屉', (await cdp.eval(`document.body.classList.contains('m-nav-open')`)) === false);

    /* ---- 表格卡片化 + 筛选折叠（报销单列表） ---- */
    await cdp.eval(`location.hash = '#/reimbursements'`);
    await sleep(2200);
    const cards = await cdp.eval(`(() => {
      const tbl = document.querySelector('#view-inner table.tbl');
      const row = tbl ? tbl.querySelector('tbody tr') : null;
      const tds = row ? [...row.children] : [];
      const labels = tds.map((td) => td.getAttribute('data-label'));
      /* 首个带标签的非标题格：m-head 是卡片标题（无标签栏位设计），
         m-act 是操作区，量它们的 padding 没有意义 */
      const td = tds.find((t) => t.getAttribute('data-label') && !t.classList.contains('m-head') && !t.classList.contains('m-act'));
      return {
        enhanced: !!(tbl && tbl.dataset.mCards === '1'),
        theadHidden: (() => { const s = getComputedStyle(document.querySelector('#view-inner table.tbl thead')); return !s || s.display === 'none'; })(),
        labelCount: labels.filter(Boolean).length,
        cardPadding: td ? parseFloat(getComputedStyle(td).paddingLeft) : 0,
        toggle: !!document.querySelector('#toolbar > .m-filter-toggle'),
        filtersHidden: (() => { const b = document.querySelector('#toolbar > .m-filters'); return !b || b.hidden; })(),
      };
    })()`);
    assert('手机版 · 表格已自动卡片化', cards.enhanced === true);
    assert('手机版 · 表头已隐藏（列名落到卡片上）', cards.theadHidden === true);
    assert('手机版 · 卡片单元格带列名标签', cards.labelCount >= 4, `${cards.labelCount} 个标签`);
    assert('手机版 · 卡片样式生效（标签栏位让出空间）', cards.cardPadding >= 90, `padding-left ${cards.cardPadding}px`);
    const cshot = await cdp.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(`${OUT}/m-cards.png`, Buffer.from(cshot.data, 'base64'));

    if (!cards.toggle) {
      skip('手机版 · 筛选折叠', '该视图可折叠的筛选控件不足 2 个，未生成折叠面板');
    } else {
      assert('手机版 · 筛选默认折叠', cards.filtersHidden === true);
      await cdp.eval(`document.querySelector('#toolbar > .m-filter-toggle').click()`);
      await sleep(400);
      const opened = await cdp.eval(`(() => {
        const box = document.querySelector('#toolbar > .m-filters');
        if (!box) return { shown: false, visible: 0 };
        const ctl = [...box.querySelectorAll('select, input')];
        return {
          shown: !box.hidden,
          visible: ctl.filter((e) => e.offsetParent !== null).length,
          total: ctl.length,
        };
      })()`);
      assert('手机版 · 点筛选展开面板', opened.shown === true);
      assert('手机版 · 展开后筛选控件可用（未丢失/未藏死）',
        opened.total >= 2 && opened.visible === opened.total, `${opened.visible}/${opened.total}`);
      await cdp.eval(`document.querySelector('#toolbar > .m-filter-toggle').click()`);
      await sleep(300);
    }

    /* ---- 全屏弹窗 ---- */
    await cdp.eval(`document.querySelector('#btn-new').click()`);
    await cdp.waitFor(`document.querySelector('.modal-mask .modal')`, { label: '手机端新建弹窗' });
    await sleep(600);
    const full = await cdp.eval(`(() => {
      const m = document.querySelector('.modal-mask .modal');
      if (!m) return null;
      const r = m.getBoundingClientRect();
      return {
        w: Math.round(r.width), h: Math.round(r.height),
        radius: getComputedStyle(m).borderTopLeftRadius,
        inner: [window.innerWidth, window.innerHeight],
      };
    })()`);
    assert('手机版 · 弹窗铺满全屏',
      full && Math.abs(full.w - full.inner[0]) <= 1 && Math.abs(full.h - full.inner[1]) <= 2,
      full ? `${full.w}x${full.h} vs ${full.inner.join('x')}` : '无弹窗');
    const fshot = await cdp.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(`${OUT}/m-modal-full.png`, Buffer.from(fshot.data, 'base64'));
    await cdp.eval(`(() => { const b = document.querySelector('.modal-mask [data-close]'); if (b) b.click(); return true; })()`);
    await sleep(500);

    /* ---- 拍照入口（发票登记弹窗，限有权限角色） ---- */
    if (['财务', '管理员'].includes(ROLE)) {
      await cdp.eval(`location.hash = '#/invoices'`);
      await sleep(1800);
      await cdp.eval(`document.querySelector('#btn-new').click()`);
      await cdp.waitFor(`document.querySelector('.modal-mask #v-file')`, { timeout: 8000, label: '登记发票弹窗' });
      await sleep(400);
      const cam = await cdp.eval(`(() => {
        const b = document.querySelector('.modal-mask [data-camera]');
        const vis = !!b && getComputedStyle(b).display !== 'none';
        const target = b ? document.querySelector(b.dataset.camera) : null;
        return { vis, target: !!target, label: b ? b.textContent.trim() : '' };
      })()`);
      assert('手机版 · 登记发票提供拍照入口', cam.vis === true, cam.label);
      assert('手机版 · 拍照按钮指向真实存在的文件控件', cam.target === true);
      await cdp.eval(`(() => { const b = document.querySelector('.modal-mask [data-close]'); if (b) b.click(); return true; })()`);
      await sleep(400);
    }

    /* ---- 断点回归：拖回桌面再拖回手机，外壳要能干净地来回切 ---- */
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: 1280, height: 900, deviceScaleFactor: 1, mobile: false,
    });
    await sleep(900);
    const desktop = await cdp.eval(`(() => {
      const vis = (sel) => { const n = document.querySelector(sel); return !!n && getComputedStyle(n).display !== 'none'; };
      return {
        tabbar: vis('#mtabbar'),
        navBtn: vis('.m-nav-btn'),
        toggle: !!document.querySelector('#toolbar > .m-filter-toggle'),
        filters: !!document.querySelector('#toolbar > .m-filters'),
        thead: (() => { const s = getComputedStyle(document.querySelector('#view-inner table.tbl thead')); return !s || s.display !== 'none'; })(),
        overflowX: document.documentElement.scrollWidth - window.innerWidth,
      };
    })()`);
    assert('断点回归 · 拖回桌面后底部标签栏隐藏', desktop.tabbar === false);
    assert('断点回归 · 拖回桌面后汉堡按钮隐藏', desktop.navBtn === false);
    assert('断点回归 · 折叠面板已还原（筛选控件回到工具条）',
      desktop.toggle === false && desktop.filters === false);
    assert('断点回归 · 表格恢复为普通表格', desktop.thead === true);

    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: VIEW_W, height: VIEW_H, deviceScaleFactor: 2, mobile: true,
    });
    await sleep(900);
    const back = await cdp.eval(`(() => {
      const vis = (sel) => { const n = document.querySelector(sel); return !!n && getComputedStyle(n).display !== 'none'; };
      return {
        tabbar: vis('#mtabbar'),
        toggle: !!document.querySelector('#toolbar > .m-filter-toggle'),
        enhanced: (document.querySelector('#view-inner table.tbl') || { dataset: {} }).dataset.mCards === '1',
        overflowX: document.documentElement.scrollWidth - window.innerWidth,
      };
    })()`);
    assert('断点回归 · 拖回手机后外壳恢复', back.tabbar === true && back.toggle === true && back.enhanced === true,
      `tabbar=${back.tabbar} toggle=${back.toggle} cards=${back.enhanced}`);
    assert('断点回归 · 拖回手机后仍无横向溢出', back.overflowX <= 1, `超出 ${back.overflowX}px`);
  }

  /* ---------------- 只读体检（生产巡检） ---------------- */
  if (READONLY) {
    console.log('\n\x1b[1m== 只读体检：逐页渲染与空状态 ==\x1b[0m');

    // 管理员被代设初始密码时进来会弹强制改密，这里只把弹窗节点摘掉再继续翻页，
    // 不改后端数据，所以密码不会被真的改掉。
    const forced = await cdp.eval(`!!document.querySelector('.modal-mask')`);
    console.log(`  ${forced ? '\x1b[33m注意\x1b[0m' : '  info'} 强制改密弹窗：${forced ? '出现（首次登录必须改密）' : '未出现'}`);
    await cdp.eval(`document.querySelectorAll('.modal-mask').forEach(n => n.remove())`);

    for (const [key, title] of Object.entries(VIEW_TITLES)) {
      if (!EXPECT.includes(key)) continue;
      await cdp.eval(
        `document.querySelector('.nav-item[data-key="${key}"]') && document.querySelector('.nav-item[data-key="${key}"]').click()`
      );
      await sleep(1900);
      const t = await cdp.eval(`({
        title: (document.querySelector('#page-title')||{}).textContent || '',
        inner: !!document.querySelector('#view-inner'),
        failing: [...document.querySelectorAll('#view-inner .empty')].map(e => e.textContent)
                   .filter(x => /失败|错误|error/i.test(x)),
        empties: [...document.querySelectorAll('#view-inner .empty')].map(e => e.textContent.trim().slice(0, 46)),
        spin: document.querySelectorAll('#view-inner .spin').length,
      })`);
      assert(
        `「${title}」渲染正常`,
        t.inner && t.failing.length === 0 && t.spin === 0,
        (t.title || '') + (t.empties.length ? ' | 空态：' + t.empties[0] : '')
      );
      await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true }).then((s) =>
        writeFileSync(`${OUT}/${key}.png`, Buffer.from(s.data, 'base64'))
      );
    }

    console.log('\n\x1b[1m== 控制台检查 ==\x1b[0m');
    const roErrors = cdp.errors
      .slice(errBaseline)
      .filter((e) => !/favicon|Failed to load resource.*404/i.test(e));
    assert('只读巡检无 JS 报错', roErrors.length === 0, roErrors.slice(0, 2).join(' | ').slice(0, 200));

    console.log(`\n\x1b[1m结果：${PASS} 通过 / ${FAIL} 失败\x1b[0m`);
    console.log(`截图目录：${OUT}`);
    if (FAIL) console.log('失败项：\n - ' + failures.join('\n - '));
    process.exit(FAIL ? 1 : 0);
  }

  /* ---- 账号菜单 ---- */
  console.log('\n\x1b[1m== 账号菜单 ==\x1b[0m');
  await cdp.eval(`document.querySelector('#user-chip').click()`);
  await cdp.waitFor(`document.querySelector('#user-menu')`, { label: '账号菜单' });
  const menu = await cdp.eval(`({
    items: [...document.querySelectorAll('#user-menu .um-item')].map(e => e.textContent.trim()),
    role: (document.querySelector('#user-menu .role-tag')||{}).textContent || '',
    meta: (document.querySelector('#user-menu .um-meta')||{}).textContent || '',
  })`);
  assert('账号菜单含改密与退出', menu.items.includes('修改密码') && menu.items.includes('退出登录'), menu.items.join('/'));
  assert('账号菜单显示角色', menu.role.length > 0, menu.role);
  await cdp.eval(`document.querySelector('#user-menu .um-item').click()`);
  await cdp.waitFor(`document.querySelector('.modal-head h3')`, { label: '改密弹窗' });
  const pwdModal = await cdp.eval(`document.querySelector('.modal-head h3').textContent`);
  assert('改密弹窗可打开', /密码/.test(pwdModal), pwdModal);
  await cdp.eval(`document.querySelector('.modal-x').click()`);
  await sleep(300);

  /* ---- 逐个视图 ---- */
  phase('登录后·首屏');
  const results = {};
  for (const key of EXPECT) {
    const name = VIEW_TITLES[key];
    console.log(`\n\x1b[1m== ${name} ==\x1b[0m`);
    const before = cdp.errors.length;
    await cdp.eval(`location.hash = '#/${key}'`);
    await sleep(1400);
    await cdp.waitFor(
      `document.querySelector('#page-title') && document.querySelector('#page-title').textContent === ${JSON.stringify(name)}`,
      { label: `${name} 标题` }
    );
    await sleep(900);

    const info = await cdp.eval(`({
      title: document.querySelector('#page-title').textContent,
      sub: document.querySelector('#page-sub').textContent,
      toolbarVisible: document.querySelector('#toolbar').style.display !== 'none',
      toolbarControls: document.querySelectorAll('#toolbar input, #toolbar select, #toolbar button').length,
      rows: document.querySelectorAll('#view-inner table.tbl tbody tr').length,
      tables: document.querySelectorAll('#view-inner table.tbl').length,
      charts: document.querySelectorAll('#view-inner canvas').length,
      kpis: document.querySelectorAll('#view-inner .kpi').length,
      tabs: [...document.querySelectorAll('#view-inner .tab')].map(e => e.textContent.trim().split(' ')[0]),
      emptyBlocks: document.querySelectorAll('#view-inner .empty').length,
      badges: document.querySelectorAll('#view-inner .badge').length,
      spinner: document.querySelectorAll('#view-inner .spin').length,
      failBlocks: [...document.querySelectorAll('#view-inner .empty')].map(e=>e.textContent).filter(t=>/失败/.test(t)),
      /* 扫码核验页没有列表筛选工具条（cfg.toolbar 未配置），用页面自己的取像/手输入口作为渲染判定 */
      scanStage: !!document.querySelector('#view-inner #scan-stage'),
      scanManual: !!document.querySelector('#view-inner #scan-input'),
      scanActions: document.querySelectorAll('#view-inner .scan-actions button, #view-inner .scan-actions label').length,
    })`);
    results[key] = info;

    assert(`${name} · 标题正确`, info.title === name, info.title);
    if (key === 'scan') {
      assert(`${name} · 取像入口已渲染`,
        info.scanStage && info.scanActions >= 1,
        `stage=${info.scanStage} 动作=${info.scanActions}`);
      assert(`${name} · 手动输入单号入口已渲染`,
        info.scanManual, `scan-input=${info.scanManual}`);
    } else {
      assert(`${name} · 筛选工具条已渲染`, info.toolbarVisible && info.toolbarControls >= 1, `${info.toolbarControls} 个控件`);
    }
    assert(`${name} · 无残留 loading`, info.spinner === 0);
    assert(`${name} · 无加载失败`, info.failBlocks.length === 0, info.failBlocks.join('|').slice(0, 120));
    assert(`${name} · 页面无报错`, cdp.errors.length === before, cdp.errors.slice(before).join(' | ').slice(0, 160));

    const shot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    writeFileSync(`${OUT}/${key}.png`, Buffer.from(shot.data, 'base64'));
  }

  /* ---- 角色相关的按钮可见性 ---- */
  phase('逐视图遍历后');
  console.log('\n\x1b[1m== 角色按钮可见性 ==\x1b[0m');
  await cdp.eval(`location.hash = '#/reimbursements'`);
  await sleep(1500);
  const reimbBtns = await cdp.eval(`({
    newBtn: !!document.querySelector('#btn-new'),
    rowActs: [...new Set([...document.querySelectorAll('#view-inner .row-actions button')].map(b=>b.textContent.trim()))],
  })`);
  assert('报销单 · 新建按钮存在（所有角色可建单）', reimbBtns.newBtn === true);

  /* ---- 新建报销单：申请人应默认选中登录账号绑定的员工 ---- */
  if (reimbBtns.newBtn) {
    console.log('\n\x1b[1m== 新建报销单默认申请人 ==\x1b[0m');
    const me = await cdp.eval(`JSON.parse(localStorage.getItem('wb.user') || 'null')`);
    await cdp.eval(`document.querySelector('#btn-new').click()`);
    await cdp.waitFor(`!!document.querySelector('.modal-mask #f-applicant')`, {
      timeout: 8000, label: '新建报销单弹窗',
    });
    await sleep(500);
    const form = await cdp.eval(`(() => {
      const a = document.querySelector('.modal-mask #f-applicant');
      const d = document.querySelector('.modal-mask #f-form-dept');   // 注意：与筛选栏的 #f-dept 是两个不同节点
      return {
        value: a ? a.value : null,
        label: a && a.selectedIndex >= 0 ? a.options[a.selectedIndex].textContent.trim() : '',
        opts: a ? a.options.length : 0,
        dept: d ? d.value : null,
        deptLabel: d && d.selectedIndex >= 0 ? d.options[d.selectedIndex].textContent.trim() : '',
        dupId: document.querySelectorAll('#f-form-dept').length,
      };
    })()`);
    if (me && me.employee_id) {
      assert('新建报销单 · 申请人默认选中当前登录用户',
        String(form.value) === String(me.employee_id) && form.label !== '请选择',
        `value=${form.value} label=${form.label}（登录账号 employee_id=${me.employee_id}）`);
      assert('新建报销单 · 所属部门随申请人一并带出',
        String(form.dept) === String(me.department_id || ''),
        `dept=${form.dept} ${form.deptLabel}`);
    } else {
      assert('新建报销单 · 账号未绑员工时申请人留空待选',
        form.value === '' && form.label === '请选择', `value=${form.value} label=${form.label}`);
    }
    assert('新建报销单 · 申请人下拉仍可改选他人', form.opts > 1, `${form.opts} 个选项`);
    assert('新建报销单 · 部门下拉 id 不与筛选栏重复', form.dupId === 1, `#f-form-dept 节点数=${form.dupId}`);
    const newShot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    writeFileSync(`${OUT}/reimb-new-default-applicant.png`, Buffer.from(newShot.data, 'base64'));
    // 只验默认值，不保存——避免在验收库里留下脏数据
    await cdp.eval(`(() => { const b = document.querySelector('.modal-mask [data-close]'); if (b) b.click(); return true; })()`);
    await sleep(500);
  }

  if (ROLE === '申请人') {
    assert('申请人 · 不出现在审批/付款按钮', !reimbBtns.rowActs.includes('通过') && !reimbBtns.rowActs.includes('付款'), reimbBtns.rowActs.join('/'));
  } else {
    assert(`${ROLE} · 可见审批相关按钮`, reimbBtns.rowActs.length > 0, reimbBtns.rowActs.join('/'));
  }

  await cdp.eval(`location.hash = '#/invoices'`);
  await sleep(1500);
  const invBtns = await cdp.eval(`({
    newBtn: !!document.querySelector('#btn-new'),
    checkBox: !!document.querySelector('#ck-all'),
    rowActs: [...new Set([...document.querySelectorAll('#view-inner .row-actions button')].map(b=>b.textContent.trim()))],
  })`);
  if (['财务', '管理员'].includes(ROLE)) {
    assert('发票 · 财务/管理员可见登记与查验', invBtns.newBtn === true && invBtns.rowActs.includes('查验'), invBtns.rowActs.join('/'));

    // 登记发票弹窗：必须能上传发票自动填表。财务手工录入时靠它免去逐字段手敲，
    // 识别不到的地方仍可改——「手工录入信息不全」的正解就在这一步。
    await cdp.eval(`document.querySelector('#btn-new').click()`);
    await cdp.waitFor(`!!document.querySelector('.modal-mask #v-file')`, { timeout: 8000, label: '登记发票弹窗' });
    const regUp = await cdp.eval(`({
      hasPicker: !!document.querySelector('#v-file'),
      hint: (document.querySelector('#v-recog-hint') || {}).textContent || '',
    })`);
    assert('登记发票 · 提供上传发票识别入口', regUp.hasPicker === true);
    assert('登记发票 · 说明可自动填表且仍可手改', /自动填入/.test(regUp.hint) && /手工修改/.test(regUp.hint),
      regUp.hint.trim().slice(0, 50));

    const docReg = await cdp.send('DOM.getDocument', { depth: -1 });
    const regNode = await cdp.send('DOM.querySelector', { nodeId: docReg.root.nodeId, selector: '#v-file' });
    await cdp.send('DOM.setFileInputFiles', { files: [INVOICE_SAMPLE], nodeId: regNode.nodeId });
    await cdp.waitFor(`(document.querySelector('#v-no') || {}).value`, { timeout: 20000, label: '识别结果回填' });
    const regFilled = await cdp.eval(`({
      no: document.querySelector('#v-no').value,
      amount: document.querySelector('#v-amount').value,
      seller: document.querySelector('#v-seller').value,
      sellerTax: document.querySelector('#v-selltax').value,
      buyerTax: document.querySelector('#v-buytax').value,
      date: document.querySelector('#v-date').value,
      hint: (document.querySelector('#v-recog-hint') || {}).textContent || '',
    })`);
    assert('登记发票 · 上传后自动填入发票号码', regFilled.no === '24417000000077889911', regFilled.no);
    assert('登记发票 · 自动填入金额', Number(regFilled.amount) === 2380, regFilled.amount);
    assert('登记发票 · 自动填入购销双方税号',
      regFilled.sellerTax === '91440300MA5F1234XA' && regFilled.buyerTax === '91440300MA5G5678XB',
      `${regFilled.sellerTax} / ${regFilled.buyerTax}`);
    assert('登记发票 · 自动填入销售方与开票日期',
      regFilled.seller === '深圳市云图科技有限公司' && regFilled.date === '2026-09-12',
      `${regFilled.seller} / ${regFilled.date}`);
    assert('登记发票 · 回显已填入项数', /填入/.test(regFilled.hint), regFilled.hint.trim().slice(0, 40));
    const regShot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    writeFileSync(`${OUT}/invoices-register-recognize.png`, Buffer.from(regShot.data, 'base64'));
    // 只验识别回填，不保存——避免在验收库里留下脏数据
    await cdp.eval(`(() => { const b = document.querySelector('.modal-mask [data-close]'); if (b) b.click(); return true; })()`);
    await sleep(600);
  } else {
    assert('发票 · 非财务角色无登记按钮', invBtns.newBtn === false, `new=${invBtns.newBtn}`);
    assert('发票 · 非财务角色无批量勾选', invBtns.checkBox === false);
    assert('发票 · 非财务角色仍可查看影像', invBtns.rowActs.includes('影像'), invBtns.rowActs.join('/'));
  }

  /* ---- 费用管理子页签（按角色过滤） ---- */
  console.log('\n\x1b[1m== 费用管理子页签 ==\x1b[0m');
  await cdp.eval(`location.hash = '#/expenses'`);
  await sleep(1600);
  const exTabs = await cdp.eval(`[...document.querySelectorAll('#ex-tabs .tab')].map(e=>e.textContent.trim())`);
  // 预算执行统计对「分析师」角色（审批人/财务/管理员）开放；
  // 申请人没有 stats.analyst 权限，页签必须隐藏，否则一点就是 403。
  if (['财务', '管理员', '审批人'].includes(ROLE)) {
    assert('费用管理 · 含费用预算页签', exTabs.includes('费用预算'), exTabs.join('/'));
  } else {
    assert('费用管理 · 无预算页签（无 stats.analyst 权限）', !exTabs.includes('费用预算'), exTabs.join('/'));
    assert('费用管理 · 仍可看明细台账', exTabs.includes('费用明细台账'), exTabs.join('/'));
  }
  for (let i = 0; i < exTabs.length; i++) {
    await cdp.eval(`document.querySelectorAll('#ex-tabs .tab')[${i}].click()`);
    await sleep(1400);
    const t = await cdp.eval(`({
      name: document.querySelectorAll('#ex-tabs .tab')[${i}].textContent.trim(),
      rows: document.querySelectorAll('#ex-body table.tbl tbody tr').length,
      kpis: document.querySelectorAll('#ex-body .kpi').length,
      charts: document.querySelectorAll('#ex-body canvas').length,
      checkAll: !!document.querySelector('#ex-body #ck-all'),
      batchBar: !!document.querySelector('#ex-body .batch-bar'),
      failures: [...document.querySelectorAll('#ex-body .empty')].map(e=>e.textContent).filter(t=>/失败/.test(t)),
    })`);
    assert(`子页签「${t.name}」渲染无错误`, t.failures.length === 0 && (t.rows > 0 || t.kpis > 0 || t.charts > 0), `行 ${t.rows} / KPI ${t.kpis} / 图 ${t.charts}`);
    // 写权限与批量删除必须成对出现：能改的角色勾选列与批量条一起在，不能改的一起不在
    const expWrite = (t.name === '费用类型' || t.name === '费用预算' || t.name === '基础数据') &&
      (ROLE === '管理员' || (ROLE === '财务' && t.name !== '基础数据'));
    if (expWrite) {
      assert(`「${t.name}」提供批量删除`, t.checkAll && t.batchBar, `ck=${t.checkAll} bar=${t.batchBar}`);
    } else {
      assert(`「${t.name}」无写权限则不显示勾选列`, !t.checkAll && !t.batchBar, `ck=${t.checkAll} bar=${t.batchBar}`);
    }
    const s = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    writeFileSync(`${OUT}/expenses-${i}.png`, Buffer.from(s.data, 'base64'));
  }

  /* ---- 发票收件箱（财务 / 管理员） ---- */
  if (['财务', '管理员'].includes(ROLE)) {
    console.log('\n\x1b[1m== 交互：发票收件箱 ==\x1b[0m');
    await cdp.eval(`location.hash = '#/inbox'`);
    // 注意：cdp.eval 会把表达式塞进 JSON.stringify(...)，多语句必须包成 IIFE
    await cdp.eval(`(() => {
      window.__err = null;
      window.addEventListener('unhandledrejection', (e) => {
        if (!window.__err) window.__err = String((e.reason && (e.reason.stack || e.reason.message)) || e.reason);
      });
      return true;
    })()`);
    await sleep(2200);

    const ibTabs = await cdp.eval(`[...document.querySelectorAll('#ib-tabs .tab')].map(e=>e.textContent.trim())`);
    assert('收件箱含三个页签', ibTabs.length === 3 && ibTabs.includes('收件记录') && ibTabs.includes('收票邮箱') && ibTabs.includes('手工入账'), ibTabs.join('/'));

    const ibMsgs = await cdp.eval(`({
      summary: [...document.querySelectorAll('#ib-body .inbox-summary .chip')].map(e=>e.textContent.trim()),
      hasCkAll: !!document.querySelector('#ib-body #ck-all'),
      failures: [...document.querySelectorAll('#ib-body .empty')].map(e=>e.textContent).filter(t=>/失败/.test(t)),
    })`);
    assert('收件记录页渲染无错误', ibMsgs.failures.length === 0, ibMsgs.failures.join('|'));
    assert('收件记录支持批量勾选', ibMsgs.hasCkAll === true);
    assert('收件箱汇总统计可见', ibMsgs.summary.length >= 3, ibMsgs.summary.join(' / '));
    const ibshot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    writeFileSync(`${OUT}/inbox-messages.png`, Buffer.from(ibshot.data, 'base64'));

    // 收票邮箱页：管理员有增删改，财务只读
    await cdp.eval(`[...document.querySelectorAll('#ib-tabs .tab')].find(t=>t.textContent.includes('收票邮箱')).click()`);
    await sleep(1800);
    const ibAcc = await cdp.eval(`({
      rows: document.querySelectorAll('#ib-body table.tbl tbody tr').length,
      hasNew: !!document.querySelector('#ma-new'),
      acts: [...new Set([...document.querySelectorAll('#ib-body .row-actions button')].map(b=>b.textContent.trim()))],
      failures: [...document.querySelectorAll('#ib-body .empty')].map(e=>e.textContent).filter(t=>/失败/.test(t)),
    })`);
    assert('收票邮箱页渲染无错误', ibAcc.failures.length === 0, ibAcc.failures.join('|'));
    assert('收票邮箱 · 立即收票与测试连接可用', ibAcc.acts.includes('立即收票') && ibAcc.acts.includes('测试连接'), ibAcc.acts.join('/'));
    if (ROLE === '管理员') {
      assert('收票邮箱 · 管理员可新增', ibAcc.hasNew === true);
      assert('收票邮箱 · 管理员可编辑删除', ibAcc.acts.includes('编辑') && ibAcc.acts.includes('删除'), ibAcc.acts.join('/'));
    } else {
      assert('收票邮箱 · 财务不可新增（仅管理员）', ibAcc.hasNew === false);
      assert('收票邮箱 · 财务不可编辑删除', !ibAcc.acts.includes('编辑') && !ibAcc.acts.includes('删除'), ibAcc.acts.join('/'));
    }

    // 手工入账页：能选文件，识别入口就位
    await cdp.eval(`[...document.querySelectorAll('#ib-tabs .tab')].find(t=>t.textContent.includes('手工入账')).click()`);
    await sleep(1400);
    const ibUp = await cdp.eval(`({
      picker: !!document.querySelector('#up-file'),
      syncAll: !!document.querySelector('#ib-sync-all'),
      hint: (document.querySelector('#ib-body .batch-bar')||{}).textContent || '',
    })`);
    assert('手工入账 · 文件选择器就位', ibUp.picker === true);
    assert('手工入账 · 提供「立即收取全部邮箱」', ibUp.syncAll === true);
    assert('手工入账 · 说明识别范围', /OFD|PDF/.test(ibUp.hint), ibUp.hint.trim().slice(0, 60));

    // 核心路径：上传发票 -> 识别结果必须落进「可修改、可补录」的表单。
    // 只展示不给改，识别不全时人就无从补救，这正是要钉住的行为。
    await cdp.send('DOM.enable');
    const docUp = await cdp.send('DOM.getDocument', { depth: -1 });
    const fileNode = await cdp.send('DOM.querySelector', { nodeId: docUp.root.nodeId, selector: '#up-file' });
    await cdp.send('DOM.setFileInputFiles', { files: [INVOICE_SAMPLE], nodeId: fileNode.nodeId });
    await cdp.waitFor(`!!document.querySelector('#up-result [data-rk="invoice_no"]')`, {
      timeout: 20000,
      label: '识别结果表单',
    });
    await sleep(400);
    const recForm = await cdp.eval(`({
      fields: [...document.querySelectorAll('#up-result [data-rk]')].map(e => e.dataset.rk),
      editable: [...document.querySelectorAll('#up-result [data-rk]')].every(e => e.tagName === 'INPUT' && !e.disabled && !e.readOnly),
      values: Object.fromEntries([...document.querySelectorAll('#up-result [data-rk]')].map(e => [e.dataset.rk, e.value])),
      missing: document.querySelectorAll('#up-result .recog-item.miss').length,
      hasImport: !!document.querySelector('#up-import'),
    })`);
    const WANT_FIELDS = [
      'invoice_no', 'invoice_code', 'invoice_type', 'invoice_date', 'amount', 'tax_rate',
      'tax_amount', 'seller_name', 'seller_tax_no', 'buyer_name', 'buyer_tax_no',
    ];
    assert('手工入账 · 字段与台账发票一一对应（含购销双方税号）',
      WANT_FIELDS.every((k) => recForm.fields.includes(k)), recForm.fields.join('/'));
    assert('手工入账 · 识别结果可编辑', recForm.editable === true);
    const NEED_FILLED = ['invoice_no', 'invoice_date', 'amount', 'tax_rate', 'tax_amount',
      'seller_name', 'seller_tax_no', 'buyer_name', 'buyer_tax_no'];
    assert('手工入账 · 数电票除发票代码外全部识别出来',
      NEED_FILLED.every((k) => recForm.values[k]), JSON.stringify(recForm.values));
    assert('手工入账 · 识别完整时不提示待补录', recForm.missing === 0, `缺失 ${recForm.missing} 项`);
    assert('手工入账 · 提供确认入账按钮', recForm.hasImport === true);
    const upShot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    writeFileSync(`${OUT}/inbox-upload.png`, Buffer.from(upShot.data, 'base64'));

    // 识别不出来的拍照件：必须逐项标出「待补录」，让人一眼知道哪里要补。
    // 前提提醒：若环境里配了可兜底的视觉大模型（验收用的 Mock 也算），拍照件会被 AI
    // 补全字段，「待补录」就不会出现。那不是缺陷，但也不能当通过——记成「未验证」，
    // 免得绿着绿着把这条真正重要的兜底能力漏掉了。
    const docUp2 = await cdp.send('DOM.getDocument', { depth: -1 });
    const fileNode2 = await cdp.send('DOM.querySelector', { nodeId: docUp2.root.nodeId, selector: '#up-file' });
    await cdp.send('DOM.setFileInputFiles', { files: [PHOTO_SAMPLE], nodeId: fileNode2.nodeId });
    const rendered = await cdp
      .waitFor(`document.querySelectorAll('#up-result [data-rk]').length > 0`, {
        timeout: 25000,
        label: '拍照件识别结果',
      })
      .then(() => true)
      .catch(() => false);
    assert('手工入账 · 拍照件也能给出可编辑的结果表单', rendered === true);
    await sleep(1200);
    const recMiss = await cdp.eval(`({
      miss: document.querySelectorAll('#up-result .recog-item.miss').length,
      total: document.querySelectorAll('#up-result .recog-item:not(.opt) [data-rk]').length,
      note: (document.querySelector('#up-result .recog-note') || {}).textContent || '',
      editable: [...document.querySelectorAll('#up-result [data-rk]')].every(e => e.tagName === 'INPUT' && !e.disabled),
    })`);
    if (recMiss.miss > 0) {
      assert('手工入账 · 识别不全时逐项标出待补录（可选字段不误报）', recMiss.miss === recMiss.total,
        `${recMiss.miss}/${recMiss.total}`);
    } else {
      skip('手工入账 · 识别不全时逐项标出待补录',
        '本次未出现待补录标记：环境里配了可兜底识别增强的模型，该断言不适用（要验这条请先停掉 AI 识别增强）');
    }
    assert('手工入账 · 识别失败时依然可手工填写', recMiss.editable === true);
    assert('手工入账 · 明确告知可修改补录', /修改/.test(recMiss.note) && /补录/.test(recMiss.note),
      recMiss.note.trim().slice(0, 50));
    const missShot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    writeFileSync(`${OUT}/inbox-upload-missing.png`, Buffer.from(missShot.data, 'base64'));

    const ibErr = await cdp.eval(`window.__err`);
    assert('收件箱无未捕获异常', ibErr === null, String(ibErr).slice(0, 200));
  }

  /* ---- 批量删除（勾选 -> 批量条出现 -> 危险确认弹窗） ---- */
  console.log('\n\x1b[1m== 交互：批量删除 ==\x1b[0m');
  await cdp.eval(`location.hash = '#/reimbursements'`);
  await sleep(1800);
  const rbBefore = await cdp.eval(`({
    hasCkAll: !!document.querySelector('#view-inner #ck-all'),
    barVisible: (() => { const b = document.querySelector('#rb-bar'); return !!b && getComputedStyle(b).display !== 'none'; })(),
    rowCount: document.querySelectorAll('#view-inner table.tbl tbody tr[data-id]').length,
  })`);
  assert('报销单列表支持勾选', rbBefore.hasCkAll === true, `行 ${rbBefore.rowCount}`);
  assert('未勾选时批量条隐藏', rbBefore.barVisible === false);

  if (rbBefore.rowCount > 0) {
    await cdp.eval(`(() => {
      const c = document.querySelector('#view-inner table.tbl tbody tr[data-id] .ck-row');
      c.checked = true;
      c.dispatchEvent(new Event('change', { bubbles: true }));
    })()`);
    await sleep(500);
    const rbAfter = await cdp.eval(`({
      barVisible: (() => { const b = document.querySelector('#rb-bar'); return !!b && getComputedStyle(b).display !== 'none'; })(),
      picked: (document.querySelector('#rb-picked')||{}).textContent,
      rowPicked: document.querySelectorAll('#view-inner table.tbl tbody tr.picked').length,
      hasPurge: !!document.querySelector('#rb-purge'),
    })`);
    assert('勾选后批量条出现并计数', rbAfter.barVisible === true && rbAfter.picked === '1', `picked=${rbAfter.picked}`);
    assert('已勾选行高亮', rbAfter.rowPicked === 1, `${rbAfter.rowPicked} 行`);
    assert('清空入口仅管理员可见', rbAfter.hasPurge === (ROLE === '管理员'), `hasPurge=${rbAfter.hasPurge}`);

    // 点批量删除 -> 必须弹出要求输入确认词的危险确认框，且确认按钮初始禁用
    await cdp.eval(`document.querySelector('#rb-del').click()`);
    await cdp.waitFor(`document.querySelector('.modal-mask .modal')`, { label: '危险确认弹窗' });
    await sleep(500);
    const danger = await cdp.eval(`({
      title: (document.querySelector('.modal-head h3')||{}).textContent || '',
      hasInput: !!document.querySelector('#dc-input'),
      keyword: (document.querySelector('.danger-key')||{}).textContent || '',
      okDisabled: (document.querySelector('#dc-ok')||{}).disabled,
    })`);
    assert('批量删除弹出危险确认框', /危险操作/.test(danger.title), danger.title);
    assert('确认框要求输入确认词', danger.hasInput === true && danger.keyword.length > 0, `keyword=${danger.keyword}`);
    assert('未输入确认词时确认按钮禁用', danger.okDisabled === true);

    // 输入错误确认词，按钮应保持禁用
    await cdp.eval(`(() => {
      const i = document.querySelector('#dc-input');
      i.value = '随便乱打';
      i.dispatchEvent(new Event('input', { bubbles: true }));
    })()`);
    await sleep(300);
    assert('输入错误确认词仍禁用', (await cdp.eval(`document.querySelector('#dc-ok').disabled`)) === true);

    // 输入正确确认词后按钮可用，然后取消（不做真实删除）
    await cdp.eval(`(() => {
      const i = document.querySelector('#dc-input');
      i.value = document.querySelector('.danger-key').textContent.trim();
      i.dispatchEvent(new Event('input', { bubbles: true }));
    })()`);
    await sleep(300);
    assert('输入正确确认词后按钮可用', (await cdp.eval(`document.querySelector('#dc-ok').disabled`)) === false);
    const dshot2 = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    writeFileSync(`${OUT}/batch-danger.png`, Buffer.from(dshot2.data, 'base64'));
    await cdp.eval(`document.querySelector('.modal-mask .modal [data-close]').click()`);
    await sleep(400);
    // 取消后取消勾选，避免影响后续断言
    await cdp.eval(`(() => {
      const c = document.querySelector('#view-inner table.tbl tbody tr[data-id] .ck-row');
      if (c) { c.checked = false; c.dispatchEvent(new Event('change', { bubbles: true })); }
    })()`);
    await sleep(300);
  }

  // 发票台账的批量条（财务/管理员才有勾选列）
  await cdp.eval(`location.hash = '#/invoices'`);
  await sleep(1800);
  const invBatch = await cdp.eval(`({
    hasCkAll: !!document.querySelector('#iv-body #ck-all'),
    hasBar: !!document.querySelector('#iv-body #iv-batch-bar'),
    hasDel: !!document.querySelector('#iv-del-selected'),
    hasPurgeUnlinked: !!document.querySelector('#iv-purge-unlinked'),
    hasPurgeAll: !!document.querySelector('#iv-purge-all'),
  })`);
  if (['财务', '管理员'].includes(ROLE)) {
    assert('发票台账 · 批量工具条就位', invBatch.hasCkAll && invBatch.hasBar && invBatch.hasDel, JSON.stringify(invBatch));
    assert('发票台账 · 提供清空待关联散票', invBatch.hasPurgeUnlinked === true);
    assert('发票台账 · 清空全部仅管理员可见', invBatch.hasPurgeAll === (ROLE === '管理员'), `hasPurgeAll=${invBatch.hasPurgeAll}`);
  } else {
    assert('发票台账 · 非财务角色无批量勾选', invBatch.hasCkAll === false && invBatch.hasBar === false);
  }

  /* ---- 发票重复检测 ---- */
  console.log('\n\x1b[1m== 交互：重复报销检测 ==\x1b[0m');
  await cdp.eval(`location.hash = '#/invoices'`);
  await sleep(1600);
  await cdp.eval(`document.querySelectorAll('#iv-tabs .tab')[1].click()`);
  await sleep(1400);
  const dup = await cdp.eval(`({
    rows: document.querySelectorAll('#iv-body table.tbl tbody tr').length,
    empty: document.querySelector('#iv-body .empty') ? document.querySelector('#iv-body .empty').textContent : '',
  })`);
  assert('重复检测页签渲染', dup.rows >= 0 && !/失败/.test(dup.empty), dup.empty || `${dup.rows} 组`);
  const dshot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  writeFileSync(`${OUT}/invoices-dup.png`, Buffer.from(dshot.data, 'base64'));

  /* ---- 报销单详情弹窗 ---- */
  console.log('\n\x1b[1m== 交互：报销单详情弹窗 ==\x1b[0m');
  await cdp.eval(`location.hash = '#/reimbursements'`);
  await sleep(1600);
  const firstRow = await cdp.eval(`!!document.querySelector('#view-inner table.tbl tbody tr.clickable')`);
  if (firstRow) {
    await cdp.eval(`document.querySelector('#view-inner table.tbl tbody tr.clickable').click()`);
    await cdp.waitFor(`document.querySelector('.modal-mask .modal')`, { label: '详情弹窗' });
    await sleep(700);
    const modal = await cdp.eval(`({
      title: document.querySelector('.modal-head h3').textContent,
      descItems: document.querySelectorAll('.desc-item').length,
      innerTables: document.querySelectorAll('.modal .tbl').length,
      footerBtns: [...document.querySelectorAll('.modal-foot button')].map(b => b.textContent.trim()),
    })`);
    assert('详情弹窗已打开', !!modal.title, modal.title);
    assert('基本信息字段完整', modal.descItems >= 8, `${modal.descItems} 个字段`);
    assert('明细表格已渲染', modal.innerTables >= 1, `${modal.innerTables} 张`);
    if (ROLE === '申请人') {
      assert('申请人 · 详情弹窗无审批/付款按钮', !modal.footerBtns.includes('审批通过') && !modal.footerBtns.includes('确认付款'), modal.footerBtns.join('/'));
    }
    const mshot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    writeFileSync(`${OUT}/modal-detail.png`, Buffer.from(mshot.data, 'base64'));
    await cdp.eval(`document.querySelector('.modal-x').click()`);
    await sleep(400);
  } else {
    assert('报销单列表至少有一行可点开', false, '无数据行');
  }

  /* ---- 管理视图交互（仅管理员） ---- */
  if (ROLE === '管理员') {
    console.log('\n\x1b[1m== 交互：用户管理与参数 ==\x1b[0m');
    await cdp.eval(`location.hash = '#/users'`);
    await sleep(1600);
    const users = await cdp.eval(`({
      rows: document.querySelectorAll('#view-inner table.tbl tbody tr').length,
      hasNew: !!document.querySelector('#btn-new'),
      roleTags: document.querySelectorAll('#view-inner .role-tag').length,
    })`);
    assert('用户管理表格渲染', users.rows >= 1, `${users.rows} 行`);
    assert('用户管理显示角色标签', users.roleTags >= 1, `${users.roleTags} 个`);
    assert('用户管理有新增入口', users.hasNew === true);

    await cdp.eval(`location.hash = '#/settings'`);
    await sleep(1500);
    const sets = await cdp.eval(`({
      rows: document.querySelectorAll('#set-body .set-row').length,
      groups: document.querySelectorAll('#set-body .set-group').length,
      editable: [...document.querySelectorAll('#set-body input')].some(i => !i.disabled),
      saveBtn: !!document.querySelector('#set-save'),
    })`);
    assert('系统参数分组行渲染', sets.rows >= 3, `${sets.rows} 项 / ${sets.groups} 组`);
    assert('管理员可编辑参数', sets.editable && sets.saveBtn, `editable=${sets.editable} save=${sets.saveBtn}`);

    await cdp.eval(`location.hash = '#/audit'`);
    await sleep(1600);
    const audit = await cdp.eval(`({
      rows: document.querySelectorAll('#view-inner table.tbl tbody tr').length,
      actions: (document.querySelector('#f-action')||{}).options ? document.querySelector('#f-action').options.length : 0,
      hasCkAll: !!document.querySelector('#view-inner #ck-all'),
      hasPurgeAll: !!document.querySelector('#btn-purge-all'),
      hasPurgeOld: !!document.querySelector('#btn-purge-old'),
      entityOptions: (document.querySelector('#f-entity')||{}).options ? document.querySelector('#f-entity').options.length : 0,
    })`);
    assert('审计日志表格渲染', audit.rows >= 1, `${audit.rows} 行`);
    assert('审计动作筛选项已加载', audit.actions >= 5, `${audit.actions} 项`);
    assert('审计日志支持批量勾选', audit.hasCkAll === true);
    assert('审计日志提供清理入口', audit.hasPurgeAll && audit.hasPurgeOld, `all=${audit.hasPurgeAll} old=${audit.hasPurgeOld}`);
    assert('审计对象筛选含收件箱实体', audit.entityOptions >= 12, `${audit.entityOptions} 项`);
    const ashot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    writeFileSync(`${OUT}/audit.png`, Buffer.from(ashot.data, 'base64'));
  }

  /* ---- 发票影像：真实上传 + 列表回显（CDP 直接投递文件） ---- */
  if (['财务', '管理员'].includes(ROLE)) {
    console.log('\n\x1b[1m== 交互：发票影像上传 ==\x1b[0m');
    await cdp.eval(`location.hash = '#/invoices'`);
    await sleep(1600);
    // 页签选择在会话内保留，前面「重复检测」段把它切走了，这里显式切回台账
    await cdp.eval(`(() => {
      const t = document.querySelector('#iv-tabs .tab[data-tab="ledger"]');
      if (t && !t.classList.contains('active')) t.click();
      return true;
    })()`);
    await sleep(1800);
    // 选一张「没有附件」的发票来验上传（收件箱导入的票自带 .ofd 附件，
    // 挑到它会让「上传后行数 +1」的断言失准）
    const picked = await cdp.eval(`(() => {
      const tr = [...document.querySelectorAll('#iv-body table.tbl tbody tr[data-id]')]
        .find(r => r.textContent.includes('24417000000000101'));
      if (!tr) return 'no-row';
      const b = [...tr.querySelectorAll('button[data-act]')].find(x => x.textContent.trim() === '影像');
      if (!b) return 'no-button';
      b.click();
      return 'ok';
    })()`);
    assert('找到一张无附件的发票并打开影像弹窗', picked === 'ok', picked);
    await cdp.waitFor(`document.querySelector('#att-list')`, { label: '影像弹窗' });
    await sleep(1200);
    const attModal = await cdp.eval(`({
      title: document.querySelector('.modal-head h3').textContent,
      hasPicker: !!document.querySelector('#att-file'),
      accept: (document.querySelector('#att-file')||{}).accept || '',
      empty: !!document.querySelector('#att-list .empty'),
    })`);
    assert('影像弹窗包含上传控件', attModal.hasPicker === true, attModal.accept);
    assert('影像弹窗限定了可上传类型', /pdf/.test(attModal.accept) && /png/.test(attModal.accept), attModal.accept);
    assert('未上传前影像列表为空', attModal.empty === true);

    await cdp.send('DOM.enable');
    const doc = await cdp.send('DOM.getDocument', { depth: -1 });
    const found = await cdp.send('DOM.querySelector', { nodeId: doc.root.nodeId, selector: '#att-file' });
    await cdp.send('DOM.setFileInputFiles', { files: [SAMPLE] , nodeId: found.nodeId });

    // 上传成功后会重新拉列表
    await cdp.waitFor(
      `document.querySelectorAll('#att-list table.tbl tbody tr').length > 0`,
      { timeout: 15000, label: '影像列表出现记录' }
    );
    // 图片要在列表渲染后被逐个取回 blob（受保护接口），存在异步窗口，
    // 这里等它落地再断言，避免和渲染抢跑
    await cdp
      .waitFor(`!!(document.querySelector('#att-list [data-view]')||{}).dataset?.blob`, {
        timeout: 12000,
        label: '图片取回 blob',
      })
      .catch(() => {});
    const attRows = await cdp.eval(`({
      rows: document.querySelectorAll('#att-list table.tbl tbody tr').length,
      name: (document.querySelector('#att-list table.tbl tbody tr td')||{}).textContent || '',
      acts: [...document.querySelectorAll('#att-list .row-actions button')].map(b=>b.textContent.trim()),
      hasBlobPreview: !!document.querySelector('#att-list [data-view]') && !!document.querySelector('#att-list [data-view]').dataset.blob,
    })`);
    assert('影像上传后列表出现记录', attRows.rows >= 1, `${attRows.rows} 条 / ${attRows.name.trim()}`);
    assert('影像提供预览与下载', attRows.acts.includes('预览') && attRows.acts.includes('下载'), attRows.acts.join('/'));
    assert('图片已取回 blob 供预览（受保护接口需鉴权）', attRows.hasBlobPreview === true);
    const atshot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    writeFileSync(`${OUT}/invoice-attachments.png`, Buffer.from(atshot.data, 'base64'));

    // 删除刚上传的影像，保持验收数据干净
    await cdp.eval(
      `[...document.querySelectorAll('#att-list .row-actions button')].find(b => b.textContent.trim()==='删除').click()`
    );
    await cdp.waitFor(`document.querySelector('.modal-foot [data-ok]')`, { label: '删除确认' });
    await cdp.eval(`document.querySelector('.modal-foot [data-ok]').click()`);
    await sleep(1200);
    const afterDel = await cdp.eval(`document.querySelectorAll('#att-list table.tbl tbody tr').length`);
    assert('影像可删除', afterDel === 0, `${afterDel} 条`);
    await cdp.eval(`document.querySelector('.modal-x').click()`);
    await sleep(400);
  }

  /* ---- 登录态持久化：刷新后仍在线 ---- */
  phase('交互流程后');
  console.log('\n\x1b[1m== 会话持久化 ==\x1b[0m');
  await cdp.send('Page.navigate', { url: BASE + '/' });
  await cdp.waitFor(`document.querySelector('#app') && getComputedStyle(document.querySelector('#app')).display !== 'none'`, {
    label: '刷新后仍保持登录',
  });
  await cdp.waitFor(
    `document.querySelector('#page-title') && document.querySelector('#page-title').textContent !== '加载中…'`,
    { label: '刷新后视图渲染完成' }
  );
  const afterReload = await cdp.eval(`({
    loginVisible: !!document.querySelector('#login-root') && getComputedStyle(document.querySelector('#login-root')).display !== 'none',
    title: document.querySelector('#page-title').textContent,
  })`);
  assert('刷新后免登录直接进入工作台', afterReload.loginVisible === false, afterReload.title);

  /* ---- 登出：token 清除且回到登录页 ---- */
  phase('刷新持久化后');
  phase('登出前');
  console.log('\n\x1b[1m== 登出 ==\x1b[0m');
  await cdp.eval(`document.querySelector('#user-chip').click()`);
  await cdp.waitFor(`document.querySelector('#user-menu')`, { label: '账号菜单' });
  await cdp.eval(
    `[...document.querySelectorAll('#user-menu .um-item')].find(e => e.textContent.includes('退出登录')).click()`
  );
  // 退出前会弹一次二次确认
  await cdp.waitFor(`document.querySelector('.modal-foot [data-ok]')`, { label: '退出确认弹窗' });
  await cdp.eval(`document.querySelector('.modal-foot [data-ok]').click()`);
  await cdp.waitFor(
    `document.querySelector('#login-root') && getComputedStyle(document.querySelector('#login-root')).display !== 'none'`,
    { timeout: 15000, label: '回到登录页' }
  );
  const afterLogout = await cdp.eval(`!!localStorage.getItem('wb.token')`);
  assert('登出后本地 token 已清除', afterLogout === false);

  /* ---------------- 全局报错汇总 ---------------- */
  phase('登出后');
  console.log('\n\x1b[1m== 控制台检查 ==\x1b[0m');
  const logoutMark = (phases.find((p) => p.label === '登出前') || { n: cdp.errors.length }).n;
  // 判据只覆盖「会话进行中」。登出那一下必然有一批 401：POST /api/auth/logout 会在
  // 服务端吊销 token，而此刻可能还有几个请求在路上（角标、AI 动作目录），它们带着
  // 刚被吊销的 token 回来只能是 401。这是预期行为，不是缺陷，所以单独报告不断言。
  const inSession = cdp.errors.slice(errBaseline, logoutMark);
  const realErrors = inSession.filter((e) => !/favicon|Failed to load resource.*404/i.test(e));
  const postLogoutErrors = cdp.errors.slice(logoutMark);
  assert('登录后控制台无 JS 报错', realErrors.length === 0, realErrors.slice(0, 3).join(' | ').slice(0, 240));
  if (realErrors.length) {
    // 分段计数：定位报错发生在哪一段，省得下次又靠猜
    const marks = phases.map((p) => `${p.label}=${p.n - errBaseline}`);
    console.log('  阶段错误累计（含基线之后全部）：' + marks.join(' → '));
    console.log('  报错明细：\n   - ' + realErrors.join('\n   - '));
  }
  if (postLogoutErrors.length) {
    console.log(`  \x1b[33m·\x1b[0m 登出瞬间另有 ${postLogoutErrors.length} 条 401（token 已被服务端吊销的在途请求，预期内，不计失败）`);
    postLogoutErrors.slice(0, 3).forEach((e) => console.log(`     ${e}`));
  }

  console.log(`\n\x1b[1m结果：${PASS} 通过 / ${FAIL} 失败${skips.length ? ` / ${skips.length} 未验证` : ''}\x1b[0m`);
  console.log(`截图目录：${OUT}`);
  if (FAIL) console.log('失败项：\n - ' + failures.join('\n - '));
  if (skips.length) console.log('未验证（环境不适用，不计入通过）：\n - ' + skips.join('\n - '));
} catch (e) {
  console.error('\n\x1b[31m测试中断:\x1b[0m', e.message);
  if (cdp.dialogs.length) {
    console.error('被弹窗卡住的渲染线程：\n - ' + cdp.dialogs.join('\n - '));
  }
  console.error('已捕获的控制台错误：\n' + cdp.errors.slice(0, 10).join('\n'));
  if (skips.length) console.error('中断前未验证：\n - ' + skips.join('\n - '));
  FAIL++;
} finally {
  try { ws.close(); } catch (e) {}
  proc.kill('SIGKILL');
  await sleep(600);
  try { rmSync(PROFILE, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); } catch (e) {}
  process.exit(FAIL ? 1 : 0);
}
