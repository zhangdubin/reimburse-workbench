/* 备份页版式审计（v2.9.28）：真浏览器打开「数据备份」页，体检
 *   - 六个区块是否都渲染出来（概览 / 存储 / 策略 / 最近一次 / 列表 / 危险区）
 *   - 有没有横向溢出、文字裁切、点按目标过小
 *   - 控制台有没有报错（JS 挂了页面会半白）
 * 手机（默认 390x844）与桌面（--w=1440 --h=900）各跑一遍。
 *
 * 用法：
 *   NODE_PATH=/Users/rickey/.workbuddy/binaries/node/workspace/node_modules \
 *     node tools/backup_page_audit.cjs [baseUrl] [outDir] [--w=1440] [--h=900]
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright-core');

const BASE = process.argv[2] || process.env.WB_BASE_URL || 'http://127.0.0.1:8792';
const USER = process.env.ADMIN_USERNAME || 'admin';
const PASS = process.env.ADMIN_PASSWORD || 'Adm1n@2026';
const OUT = process.argv.slice(3).find(a => !a.startsWith('--'))
  || path.join(__dirname, '..', 'docs', 'screenshots', 'layout');

const PROBE = `(() => {
  const vis = el => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0.5 && r.height > 0.5;
  };
  const name = el => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    const cls = (el.className && typeof el.className === 'string')
      ? el.className.split(/\\s+/).filter(Boolean).slice(0, 2).join('.') : '';
    if (cls) s += '.' + cls;
    return s;
  };
  const out = { blocks: {}, overflow: [], clipped: [], smallTaps: [], missing: [] };
  const want = {
    '概览': '.bk-hero', '存储位置': '.bk-store', '策略': '.bk-policy',
    '最近一次': '.bk-last', '列表': '.bk-table', '危险区': '.bk-danger',
  };
  for (const [k, sel] of Object.entries(want)) {
    const el = document.querySelector(sel);
    out.blocks[k] = el ? (vis(el) ? 'ok' : '隐藏') : '缺失';
    if (!el || !vis(el)) out.missing.push(k);
  }
  const scope = document.querySelector('#view-root') || document.body;
  for (const el of scope.querySelectorAll('*')) {
    if (!vis(el)) continue;
    if (el.hasAttribute('title')) continue;   // 有 title：悬停能看全，不算裁切
    if (el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0) {
      const s = getComputedStyle(el);
      const scrollable = s.overflowX === 'auto' || s.overflowX === 'scroll';
      if (!scrollable) out.clipped.push({ sel: name(el), scroll: el.scrollWidth, client: el.clientWidth });
    }
  }
  const doc = document.documentElement;
  if (doc.scrollWidth > window.innerWidth + 2)
    out.overflow.push({ sel: 'document', w: doc.scrollWidth, right: window.innerWidth });
  // 触控下限只在手机视口有意义：桌面上 24px 的次要按钮是常规尺寸，
  // 硬套 30px 会把「重新检测挂载」这类按钮全报成缺陷。
  const mobile = window.innerWidth <= 820;
  for (const el of scope.querySelectorAll('button, .btn, a.btn, input, select')) {
    if (!vis(el)) continue;
    if (el.closest('.switch')) continue;   // 开关的可点区域是 slider，不是那个透明 input
    const r = el.getBoundingClientRect();
    const native = el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio');
    const min = native ? 22 : (mobile ? 30 : 24);
    if (r.height < min || r.width < min)
      out.smallTaps.push({ sel: name(el), h: Math.round(r.height), w: Math.round(r.width),
                           text: (el.textContent || el.value || '').trim().slice(0, 12) });
  }
  const copy = document.querySelector('.bk-copy');
  out.copyBtn = copy ? 'ok' : '缺失';
  out.hasBackup = !!document.querySelector('.bk-table tbody tr');
  const hostPath = document.querySelector('.bk-path.hl code, .bk-path.cmd code');
  out.hostPathText = hostPath ? hostPath.textContent.trim().slice(0, 80) : '';
  return out;
})()`;

function findChrome() {
  for (const root of [path.join(os.homedir(), 'Library/Caches/ms-playwright'),
                      path.join(os.homedir(), '.cache/ms-playwright')]) {
    if (!fs.existsSync(root)) continue;
    for (const d of fs.readdirSync(root).filter(x => /^chromium-\d+$/.test(x))) {
      for (const c of [
        path.join(root, d, 'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'),
        path.join(root, d, 'chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'),
        path.join(root, d, 'chrome-linux/chrome'),
      ]) if (fs.existsSync(c)) return c;
    }
  }
  return undefined;
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath: findChrome(), args: ['--no-sandbox'] });
  const wArg = (process.argv.find(a => a.startsWith('--w=')) || '').split('=')[1];
  const hArg = (process.argv.find(a => a.startsWith('--h=')) || '').split('=')[1];
  const W = Number(wArg) || 390, H = Number(hArg) || 844;
  const ctx = await browser.newContext({
    viewport: { width: W, height: H }, deviceScaleFactor: 2,
    isMobile: W <= 820, hasTouch: W <= 820,
  });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e).slice(0, 160)));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text().slice(0, 160)); });

  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.fill('#lg-user', USER);
  await page.fill('#lg-pass', PASS);
  await page.click('#lg-submit');
  await page.waitForSelector('#app', { state: 'visible', timeout: 15000 });
  await page.goto(BASE + '/#/backup', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1800);

  const r = await page.evaluate(PROBE);
  const tag = W <= 820 ? 'mobile' : 'desktop';
  await page.screenshot({ path: path.join(OUT, `backup-${tag}.png`), fullPage: true });

  const flags = [];
  // 没有备份包时列表区退化成空态，不算缺陷
  const missing = r.missing.filter(k => !(k === '列表' && !r.hasBackup));
  if (missing.length) flags.push('缺失区块: ' + missing.join('/'));
  if (r.clipped.length) flags.push(`文字裁切 ${r.clipped.length}`);
  if (r.overflow.length) flags.push(`横向溢出 ${r.overflow.length}`);
  if (r.smallTaps.length) flags.push(`点按过小 ${r.smallTaps.length}`);
  console.log(`[${tag} ${W}x${H}] 区块 ${JSON.stringify(r.blocks)}`);
  console.log(`         复制按钮 ${r.copyBtn}；宿主路径行 "${r.hostPathText}"`);
  console.log(flags.length ? '  ! ' + flags.join(' / ') : '  · OK');
  r.smallTaps.slice(0, 5).forEach(s => console.log(`    小按钮 ${s.sel} ${s.w}x${s.h} "${s.text}"`));
  r.clipped.slice(0, 5).forEach(s => console.log(`    裁切 ${s.sel} ${s.scroll}/${s.client}`));
  if (errors.length) console.log('控制台报错：\n' + errors.slice(0, 6).join('\n'));

  await browser.close();
  fs.writeFileSync(path.join(OUT, `backup-audit-${tag}.json`), JSON.stringify({ ...r, errors }, null, 2));
})();
