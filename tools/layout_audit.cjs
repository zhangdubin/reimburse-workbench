/* 排版审计：真浏览器跑遍所有页面 × 桌面/手机两个视口，自动揪出
 *   - 文档级横向溢出（出现左右滚动条）
 *   - 越出视口右边界的元素（谁把页面撑宽的）
 *   - 文字被裁掉（scrollWidth > clientWidth 且 overflow 不是 visible）
 *   - 内部横向滚动容器（表格 wrapper，有的合理有的说明列宽没排好）
 *   - 手机端点按目标过小（< 32px）
 * 同时留全页截图供人眼复核。
 *
 * 用法：
 *   NODE_PATH=/Users/rickey/.workbuddy/binaries/node/workspace/node_modules \
 *     node tools/layout_audit.cjs [baseUrl]
 * 输出：tools/layout-report.md + docs/screenshots/layout/*.png
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright-core');

const BASE = process.argv[2] || process.env.WB_BASE_URL || 'http://127.0.0.1:8801';
const USER = process.env.ADMIN_USERNAME || 'admin';
const PASS = process.env.ADMIN_PASSWORD || 'Adm1n@2026';
const OUT = path.join(__dirname, '..', 'docs', 'screenshots', 'layout');

const ROUTES = [
  ['dashboard', '统计看板'], ['reimbursements', '报销单管理'], ['invoices', '发票管理'],
  ['inbox', '发票收件箱'], ['expenses', '费用管理'], ['alerts', '异常与预警'],
  ['users', '用户管理'], ['settings', '系统参数'], ['audit', '操作审计'], ['ai_settings', 'AI设置'],
];

const VIEWPORTS = [
  { key: 'desktop', label: '桌面 1600×1000', width: 1600, height: 1000, scale: 1 },
  { key: 'mobile', label: '手机 390×844', width: 390, height: 844, scale: 2 },
];

function findChrome() {
  const caches = [
    path.join(os.homedir(), 'Library/Caches/ms-playwright'),
    path.join(os.homedir(), '.cache/ms-playwright'),
  ];
  for (const root of caches) {
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

// 在页面里跑：收集该视口下所有排版异常
const PROBE = `(() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const de = document.documentElement;
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
      ? el.className.split(/\\s+/).filter(Boolean).slice(0, 3).join('.') : '';
    if (cls) s += '.' + cls;
    return s;
  };
  const out = { overflowX: de.scrollWidth - de.clientWidth, offenders: [], clipped: [],
                scrollers: [], smallTaps: [], wrapped: [] };

  const all = [...document.querySelectorAll('body *')].filter(vis);
  const isOffender = new Set();
  for (const el of all) {
    const r = el.getBoundingClientRect();
    if (r.right > vw + 1 || r.left < -1) isOffender.add(el);
  }
  // 只留最外层：父元素已经是溢出源就不必再报子元素
  for (const el of [...isOffender]) {
    let p = el.parentElement, nested = false;
    while (p) { if (isOffender.has(p)) { nested = true; break; } p = p.parentElement; }
    if (nested) continue;
    const r = el.getBoundingClientRect();
    if (r.width < vw * 1.02) continue;          // 只报真的比视口宽的东西
    out.offenders.push({ sel: name(el), w: Math.round(r.width), right: Math.round(r.right),
                         text: (el.textContent || '').trim().slice(0, 40) });
  }

  for (const el of all) {
    const s = getComputedStyle(el);
    if (s.overflow === 'visible' || s.overflowX === 'visible') continue;
    if (el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0) {
      const kids = [...el.children].map(c => name(c)).slice(0, 3).join(', ');
      const isScroll = s.overflowX === 'auto' || s.overflowX === 'scroll';
      (isScroll ? out.scrollers : out.clipped).push(
        { sel: name(el), scroll: el.scrollWidth, client: el.clientWidth, kids });
    }
  }

  // 按钮/链接点按高度（手机端重点）
  for (const el of document.querySelectorAll('button, a, .btn, [role=button]')) {
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.height < 30) out.smallTaps.push({ sel: name(el), h: Math.round(r.height),
                                            text: (el.textContent || '').trim().slice(0, 16) });
  }

  // 工具栏是否被挤到换行（子元素出现两行 top 值）
  const tb = document.querySelector('#toolbar');
  if (tb && vis(tb)) {
    const tops = [...tb.children].filter(vis).map(c => Math.round(c.getBoundingClientRect().top));
    const uniq = [...new Set(tops)];
    if (uniq.length > 1) out.wrapped.push({ sel: '#toolbar', rows: uniq.length,
                                            h: Math.round(tb.getBoundingClientRect().height) });
  }
  return out;
})()`;

const dedupe = (arr, keyFn) => {
  const seen = new Set();
  return arr.filter(x => { const k = keyFn(x); if (seen.has(k)) return false; seen.add(k); return true; });
};

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath: findChrome(), args: ['--no-sandbox'] });
  const findings = [];

  for (const vp of VIEWPORTS) {
    const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height },
                                           deviceScaleFactor: vp.scale });
    const page = await ctx.newPage();
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
    await page.fill('#lg-user', USER);
    await page.fill('#lg-pass', PASS);
    await page.click('#lg-submit');
    await page.waitForSelector('#app', { state: 'visible', timeout: 15000 });

    for (const [hash, label] of ROUTES) {
      // 同文档改 hash 只触发 hashchange，SPA 路由应当自己换视图；
      // 万一路由没响应（或停留在上一页），读一次标题确认，不对就强制重载。
      await page.goto(BASE + '/#/' + hash, { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(1600);
      const title = (await page.textContent('#page-title').catch(() => '')) || '';
      if (!title.includes(label)) {
        await page.reload({ waitUntil: 'domcontentloaded' });
        await page.waitForTimeout(2200);
      }
      let r;
      try { r = await page.evaluate(PROBE); } catch (e) { r = { error: e.message }; }
      r.offenders = dedupe(r.offenders || [], x => x.sel + x.w).slice(0, 8);
      r.clipped = dedupe(r.clipped || [], x => x.sel).slice(0, 10);
      r.scrollers = dedupe(r.scrollers || [], x => x.sel).slice(0, 10);
      r.smallTaps = dedupe(r.smallTaps || [], x => x.sel + x.h).slice(0, 10);
      r.route = hash; r.label = label; r.vp = vp.key;
      findings.push(r);
      await page.screenshot({ path: path.join(OUT, `${vp.key}-${hash}.png`), fullPage: true });
      const flag = r.overflowX > 1 ? ' ⟵ 横向溢出' : '';
      console.log(`${vp.key.padEnd(7)} ${label.padEnd(7)} 溢出=${String(r.overflowX).padStart(4)}px` +
                  ` 越界元素=${r.offenders.length} 裁切=${r.clipped.length}` +
                  ` 内滚=${r.scrollers.length} 小按钮=${r.smallTaps.length}${flag}`);
    }
    await ctx.close();
  }
  await browser.close();

  // 写报告
  const L = ['# 排版审计报告', '', `目标：${BASE}`, ''];
  for (const vp of VIEWPORTS) {
    L.push(`## ${vp.label}`, '');
    for (const f of findings.filter(x => x.vp === vp.key)) {
      const issues = [];
      if (f.overflowX > 1) issues.push(`**文档横向溢出 ${f.overflowX}px**`);
      if (f.offenders.length) issues.push(`越出右边界 ${f.offenders.length} 处`);
      if (f.clipped.length) issues.push(`文字被裁 ${f.clipped.length} 处`);
      if (f.smallTaps.length) issues.push(`点按目标 <30px ${f.smallTaps.length} 个`);
      if (f.wrapped.length) issues.push('工具栏换行');
      L.push(`### ${f.label}${issues.length ? ' — ' + issues.join('，') : ' — 无明显问题'}`, '');
      const tbl = (rows, head) => {
        if (!rows.length) return;
        L.push(`| ${head.join(' | ')} |`, `| ${head.map(() => '---').join(' | ')} |`);
        rows.forEach(r => L.push(`| ${r.join(' | ')} |`));
        L.push('');
      };
      if (f.offenders.length)
        tbl(f.offenders.map(o => [`\`${o.sel}\``, o.w + 'px', o.right + 'px', o.text]),
            ['元素', '宽度', '右边界', '文本']);
      if (f.clipped.length)
        tbl(f.clipped.map(c => [`\`${c.sel}\``, c.scroll + '/' + c.client, '`' + c.kids + '`']),
            ['元素', '内容宽/可见宽', '子元素']);
      if (f.scrollers.length)
        tbl(f.scrollers.map(c => [`\`${c.sel}\``, c.scroll + '/' + c.client, '`' + c.kids + '`']),
            ['内部横向滚动', '内容宽/可见宽', '子元素']);
      if (f.smallTaps.length)
        tbl(f.smallTaps.map(s => [`\`${s.sel}\``, s.h + 'px', s.text]), ['按钮', '高度', '文本']);
      if (f.wrapped.length)
        tbl(f.wrapped.map(w => [`\`${w.sel}\``, w.rows, w.h + 'px']), ['容器', '行数', '高度']);
    }
  }
  fs.writeFileSync(path.join(__dirname, 'layout-report.md'), L.join('\n'), 'utf8');
  console.log('\n报告：tools/layout-report.md\n截图：docs/screenshots/layout/');
})().catch(e => { console.error('审计失败：', e.message); process.exit(1); });
