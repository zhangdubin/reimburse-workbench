/* 弹窗排版审计：真浏览器逐个打开各视图的「新建/编辑」弹窗，在
 * 手机视口下体检：
 *   - 弹窗自身是否高于/宽于视口（全屏弹窗应恰好一屏，底部按钮常驻可见）
 *   - 底部操作按钮有没有被挤到视口外（「按钮看不到」的元凶）
 *   - 弹窗内部的横向溢出与文字裁切
 *   - 点按目标是否过小（< 30px）
 *   - 表单两列是否没塌成一列
 * 用法：
 *   NODE_PATH=/Users/rickey/.workbuddy/binaries/node/workspace/node_modules \
 *     node tools/modal_audit.cjs [baseUrl] [outDir]
 * 输出：控制台摘要 + docs/screenshots/layout/modal-*.png
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright-core');

const BASE = process.argv[2] || process.env.WB_BASE_URL || 'http://127.0.0.1:8801';
const USER = process.env.ADMIN_USERNAME || 'admin';
const PASS = process.env.ADMIN_PASSWORD || 'Adm1n@2026';
// 输出目录：第一个不以 -- 开头的额外参数；--w/--h 是视口参数，别当成路径
const OUT = process.argv.slice(3).find(a => !a.startsWith('--'))
  || path.join(__dirname, '..', 'docs', 'screenshots', 'layout');

// [路由, 触发选择器, 说明, 前置动作]。选择器点击前先滚动到视图内；
// prepare 里可以切子页签等前置动作。
const CASES = [
  ['reimbursements', '#btn-new', '新建报销单'],
  ['reimbursements', 'tr[data-id]', '报销单详情'],
  ['invoices', '#btn-new', '登记发票'],
  ['users', '#btn-new', '新建用户'],
  ['sso', '#sso-new', '新增身份源'],
  ['ai_settings', '#pv-add', '新增模型服务商'],
  ['inbox', '#ma-new', '新增收票邮箱', async (page) => {
    await page.evaluate(() => document.querySelector('.tab[data-tab="accounts"]')?.click());
    await page.waitForTimeout(900);
  }],
  ['expenses', '#cat-new', '新增费用类型'],
  ['expenses', '#bd-new', '新增预算', async (page) => {
    await page.evaluate(() => document.querySelector('.tab[data-tab="budget"]')?.click());
    await page.waitForTimeout(900);
  }],
];

const PROBE = `(() => {
  const modal = document.querySelector('.modal');
  if (!modal) return { error: '弹窗未打开' };
  const vw = window.innerWidth, vh = window.innerHeight;
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
  const mr = modal.getBoundingClientRect();
  const out = { box: { w: Math.round(mr.width), h: Math.round(mr.height),
                      top: Math.round(mr.top), bottom: Math.round(mr.bottom) },
                hidden: [], overflow: [], clipped: [], smallTaps: [], multiCol: [] };

  // 底部操作按钮：不在视口内、或被别的元素（标签栏/遮罩）压住，都算「看不到」
  for (const el of modal.querySelectorAll('.modal-foot button, .modal-foot .btn')) {
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const topEl = document.elementFromPoint(cx, cy);
    // 被弹窗以外的元素盖住（比如底部标签栏压在弹窗上）即是「看不到」
    const covered = topEl && topEl !== el && !el.contains(topEl) && !topEl.closest('.modal');
    if (r.bottom > vh + 1 || r.top < -1 || r.right > vw + 1 || r.left < -1 || covered)
      out.hidden.push({ sel: name(el), text: (el.textContent || '').trim().slice(0, 12),
                        rect: [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)],
                        coveredBy: covered ? (topEl.tagName + '.' + (topEl.className || '').toString().slice(0, 20)) : null });
  }
  // 横向溢出：子元素比弹窗还宽
  const all = [...modal.querySelectorAll('*')].filter(vis);
  for (const el of all) {
    const r = el.getBoundingClientRect();
    if (r.width > mr.width + 2 || r.right > mr.right + 2) {
      const s = getComputedStyle(el);
      const scrollable = s.overflowX === 'auto' || s.overflowX === 'scroll';
      if (!scrollable)
        out.overflow.push({ sel: name(el), w: Math.round(r.width), right: Math.round(r.right) });
    }
  }
  // 文字裁切
  for (const el of all) {
    const s = getComputedStyle(el);
    if (s.overflowX === 'visible') continue;
    if (el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0) {
      const scrollable = s.overflowX === 'auto' || s.overflowX === 'scroll';
      if (!scrollable)
        out.clipped.push({ sel: name(el), scroll: el.scrollWidth, client: el.clientWidth,
                           text: (el.textContent || '').trim().slice(0, 20) });
    }
  }
  // 点按目标
  for (const el of modal.querySelectorAll('button, a, .btn, input[type=checkbox], select')) {
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    // 原生勾选框/单选是浏览器控件，放到 30px 会很难看；22px + label 连带区域
    // 已经够手指点了，单独用低一档的阈值。其余按钮维持 30px 触控下限。
    const native = el.tagName === 'INPUT' &&
      (el.type === 'checkbox' || el.type === 'radio');
    const min = native ? 22 : 30;
    if (r.height < min || r.width < min)
      out.smallTaps.push({ sel: name(el), h: Math.round(r.height), w: Math.round(r.width),
                           text: (el.textContent || '').trim().slice(0, 12) });
  }
  // 表单列数：理应塌成一列
  for (const grid of modal.querySelectorAll('.form-grid, .sso-fgrid, .rm-row')) {
    if (!vis(grid)) continue;
    const cols = getComputedStyle(grid).gridTemplateColumns.split(' ').length;
    const rows = new Set([...grid.children].filter(vis).map(c => Math.round(c.getBoundingClientRect().top)));
    if (cols > 1 && rows.size > 1)
      out.multiCol.push({ sel: name(grid), cols, rows: rows.size });
  }
  const body = modal.querySelector('.modal-body');
  if (body) {
    out.body = { h: Math.round(body.getBoundingClientRect().height),
                 scrollH: body.scrollHeight, clientH: body.clientHeight };
  }
  return out;
})()`;

const dedupe = (arr, keyFn, max = 6) => {
  const seen = new Set();
  return arr.filter(x => { const k = keyFn(x); if (seen.has(k)) return false; seen.add(k); return true; }).slice(0, max);
};

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
  // 视口可用 --w/--h 覆盖：小屏（iPhone SE 375、老安卓 360）更容易挤出问题
  const wArg = (process.argv.find(a => a.startsWith('--w=')) || '').split('=')[1];
  const hArg = (process.argv.find(a => a.startsWith('--h=')) || '').split('=')[1];
  const ctx = await browser.newContext({ viewport: { width: Number(wArg) || 390, height: Number(hArg) || 844 }, deviceScaleFactor: 2,
                                         isMobile: true, hasTouch: true });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e).slice(0, 120)));
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.fill('#lg-user', USER);
  await page.fill('#lg-pass', PASS);
  await page.click('#lg-submit');
  await page.waitForSelector('#app', { state: 'visible', timeout: 15000 });

  const rows = [];
  for (const [route, sel, label, prepare] of CASES) {
    await page.goto(BASE + '/#/' + route, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1500);
    if (prepare) await prepare(page);
    // 列表或子页签是异步渲染的，等触发器真正出现（不等的话「找不到触发器」
    // 会把等待时间不足误报成版式问题）
    await page.waitForSelector(sel, { state: 'attached', timeout: 8000 }).catch(() => {});
    let r = { error: '未打开' };
    try {
      // 工具栏按钮可能被 mobile.js 折叠进筛选面板（display:none），
      // playwright 的可见性检查会卡死——直接在页面里触发 click。
      const opened = await page.evaluate((s) => {
        const el = document.querySelector(s);
        if (!el) return false;
        el.click();
        return true;
      }, sel);
      if (!opened) r = { error: '找不到触发器 ' + sel };
      else {
        await page.waitForTimeout(1400);
        r = await page.evaluate(PROBE);
      }
    } catch (e) { r = { error: e.message.slice(0, 120) }; }
    if (r.error) { console.log(`✗ ${label.padEnd(12)} ${r.error}`); rows.push({ label, ...r }); continue; }
    r.hidden = dedupe(r.hidden || [], x => x.sel).slice(0, 8);
    r.overflow = dedupe(r.overflow || [], x => x.sel);
    r.clipped = dedupe(r.clipped || [], x => x.sel);
    r.smallTaps = dedupe(r.smallTaps || [], x => x.sel);
    r.multiCol = dedupe(r.multiCol || [], x => x.sel);
    const flags = [];
    if (r.hidden.length) flags.push(`按钮被挤出视口 ${r.hidden.length}`);
    if (r.overflow.length) flags.push(`横向溢出 ${r.overflow.length}`);
    if (r.clipped.length) flags.push(`文字裁切 ${r.clipped.length}`);
    if (r.smallTaps.length) flags.push(`点按过小 ${r.smallTaps.length}`);
    if (r.multiCol.length) flags.push(`仍多列 ${r.multiCol.length}`);
    if (r.box.bottom > 845) flags.push(`弹窗底 ${r.box.bottom} 超出 844`);
    console.log(`${flags.length ? '!' : '·'} ${label.padEnd(12)} ${r.box.w}x${r.box.h}` +
                (r.body ? ` body=${r.body.clientH}/${r.body.scrollH}` : '') +
                (flags.length ? '  ' + flags.join(' / ') : '  OK'));
    if (r.hidden.length) r.hidden.forEach(h => console.log(`    看不到:${h.sel} "${h.text}" ${h.rect.join(',')}`));
    if (r.overflow.length) r.overflow.slice(0, 3).forEach(o => console.log(`    溢出:${o.sel} ${o.w}px right=${o.right}`));
    if (r.smallTaps.length) r.smallTaps.slice(0, 3).forEach(s => console.log(`    小按钮:${s.sel} ${s.w}x${s.h} "${s.text}"`));
    rows.push({ label, ...r });
    await page.screenshot({ path: path.join(OUT, `modal-${label}.png`) });
    // 强制清掉弹窗遮罩，避免上一轮没关干净挡住下一轮
    await page.evaluate(() => {
      document.querySelectorAll('.modal-mask').forEach(m => m.remove());
      document.body.classList.remove('m-nav-open');
    });
    await page.waitForTimeout(250);
  }
  await browser.close();
  fs.writeFileSync(path.join(OUT, 'modal-audit.json'), JSON.stringify(rows, null, 2));
  if (errors.length) console.log('\n控制台报错：\n' + errors.slice(0, 8).join('\n'));
})();
