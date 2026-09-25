/* Data Matrix 打印渲染专项探针（免登录）
 * 目的：定位「打印预览里看不到 DM 码」的根因。
 * 做法：打开站点 → 直接调用 WB.print.reimbursement(假数据) → 捕获打印窗口
 *      → 断言 .bc-qr-img 是否真的加载（naturalWidth）→ 模拟 print media 截图
 * 产物：docs/screenshots/print/dm-probe-*.png
 */
const path = require('path');
const fs = require('fs');
const { chromium } = require('playwright-core');

const BASE = process.argv[2] || 'http://127.0.0.1:8080';
const OUT = path.join(__dirname, '..', 'docs', 'screenshots', 'print');
fs.mkdirSync(OUT, { recursive: true });

function findChrome() {
  const c = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
  ];
  for (const p of c) if (fs.existsSync(p)) return p;
  throw new Error('找不到本机 Chrome');
}

const DATA = {
  code: 'BX20260924001', status: '待审批', title: '差旅费报销', purpose: '客户拜访与项目对接',
  department_name: '技术部', applicant_name: '张三',
  occur_start: '2026-09-01', occur_end: '2026-09-10',
  customer_name: '深圳市某某科技有限公司', project_name: '智能报销系统',
  submit_at: '2026-09-23T10:00:00', total_amount: 1234.56,
  items: [
    { category_name: '差旅费', occur_date: '2026-09-02', description: '深圳→广州 高铁二等座', amount: 600 },
    { category_name: '餐饮费', occur_date: '2026-09-03', description: '客户商务宴请', amount: 634.56 },
  ],
  invoices: [], logs: [],
};

(async () => {
  const browser = await chromium.launch({
    executablePath: findChrome(), headless: true,
    args: ['--no-sandbox', '--disable-features=Translate'],
  });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const netlog = [];
  page.on('requestfailed', (r) => netlog.push(`REQFAIL ${r.url()} :: ${r.failure() && r.failure().errorText}`));
  page.on('response', (r) => {
    if (/\/api\/print\/dm/.test(r.url())) netlog.push(`DM-RESP ${r.status()} ${r.url().slice(0, 140)}`);
  });

  const out = [];
  const ok = (n, c, x) => out.push(`${c ? 'PASS' : 'FAIL'}  ${n}${x ? '  —— ' + x : ''}`);

  await page.goto(BASE + '/', { waitUntil: 'load' });
  await page.waitForTimeout(1500);

  const hasPrint = await page.evaluate(() => !!(window.WB && WB.print && WB.print.reimbursement));
  ok('WB.print 模块（免登录可见）', hasPrint);
  if (!hasPrint) {
    console.log(out.join('\n'));
    await browser.close();
    process.exit(1);
  }

  const [pop] = await Promise.all([
    ctx.waitForEvent('page', { timeout: 10000 }).catch(() => null),
    page.evaluate((d) => {
      try { WB.print.reimbursement(d); return 'ok'; } catch (e) { return 'ERR: ' + e.message; }
    }, DATA).then((r) => console.log('调用结果:', r)),
  ]);

  if (!pop) {
    ok('打印窗口弹出', false, '未捕获到新窗口（可能走了 iframe 降级）');
    const iframeInfo = await page.evaluate(() => {
      const f = document.getElementById('_wb_pf');
      if (!f) return { exists: false };
      const doc = f.contentDocument;
      const img = doc && doc.querySelector('.bc-qr-img');
      return { exists: true, hasImg: !!img, src: img && img.getAttribute('src'), nw: img && img.naturalWidth };
    });
    console.log('iframe 降级信息:', JSON.stringify(iframeInfo));
  } else {
    const popUrl = pop.url();
    ok('打印窗口弹出', true, `URL 前缀=${popUrl.slice(0, 40)}`);
    console.log('窗口 URL 类型:', popUrl.startsWith('blob:') ? 'blob:' : popUrl.slice(0, 30));

    await pop.waitForTimeout(2500);

    // 关键断言：DM 图真的加载了吗
    const dm = await pop.evaluate(() => {
      const img = document.querySelector('.bc-qr-img');
      if (!img) return { found: false };
      return {
        found: true,
        attrSrc: img.getAttribute('src'),
        currentSrc: img.currentSrc,
        complete: img.complete,
        naturalWidth: img.naturalWidth,
        naturalHeight: img.naturalHeight,
        clientW: img.clientWidth,
        clientH: img.clientHeight,
      };
    });
    console.log('DM img 状态:', JSON.stringify(dm, null, 2));
    ok('打印页存在 .bc-qr-img', dm.found);
    ok('DM 图已加载（naturalWidth>0）', dm.found && dm.naturalWidth > 0,
      dm.found ? `naturalWidth=${dm.naturalWidth} complete=${dm.complete}` : '');

    // 屏幕态：bc-corner 默认 display:none
    const screenDisp = await pop.evaluate(() => {
      const c = document.querySelector('.bc-corner');
      return c ? getComputedStyle(c).display : 'no-node';
    });
    console.log('屏幕态 .bc-corner display =', screenDisp);

    // 打印态
    await pop.emulateMedia({ media: 'print' });
    await pop.waitForTimeout(600);
    const printDisp = await pop.evaluate(() => {
      const c = document.querySelector('.bc-corner');
      const img = document.querySelector('.bc-qr-img');
      const r = c && c.getBoundingClientRect();
      return {
        display: c ? getComputedStyle(c).display : 'no-node',
        rect: r ? { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) } : null,
        imgRect: img ? (() => { const q = img.getBoundingClientRect(); return { x: Math.round(q.x), y: Math.round(q.y), w: Math.round(q.width), h: Math.round(q.height) }; })() : null,
        docH: document.documentElement.scrollHeight,
        cornerText: c ? c.innerText.replace(/\s+/g, '') : null,   // v2.7.13：应只有码图、无文字
        cornerImgCount: c ? c.querySelectorAll('img').length : 0,
        cornerChildTags: c ? Array.from(c.querySelectorAll('*')).map((e) => e.tagName).join(',') : '',
      };
    });
    console.log('打印态信息:', JSON.stringify(printDisp, null, 2));

    await pop.screenshot({ path: path.join(OUT, 'dm-probe-reimbursement.png'), fullPage: true });
    ok('打印态 DM 卡片可见', printDisp.display !== 'none');
    ok('DM 图有实际尺寸', !!(printDisp.imgRect && printDisp.imgRect.w > 0 && printDisp.imgRect.h > 0),
      printDisp.imgRect ? `${printDisp.imgRect.w}x${printDisp.imgRect.h} @ (${printDisp.imgRect.x},${printDisp.imgRect.y})` : '');
    ok('DM 图在页面可视范围内（未越界）',
      !!(printDisp.imgRect && printDisp.imgRect.y + printDisp.imgRect.h <= printDisp.docH + 5),
      printDisp.imgRect ? `img底部=${printDisp.imgRect.y + printDisp.imgRect.h} 文档高=${printDisp.docH}` : '');
    ok('左下角只有 DM 码图、无任何文字（v2.7.13）',
      printDisp.cornerText === '' && printDisp.cornerImgCount === 1,
      `文本=${JSON.stringify(printDisp.cornerText)} 图数=${printDisp.cornerImgCount} 子标签=${printDisp.cornerChildTags}`);

    await pop.close();
  }

  console.log('\n--- 网络日志 ---');
  console.log(netlog.length ? netlog.join('\n') : '(无 DM 请求记录)');
  console.log('\n' + out.join('\n'));
  const fail = out.filter((r) => r.startsWith('FAIL')).length;
  console.log(`\n结果：${out.length - fail}/${out.length} 通过`);
  await browser.close();
  process.exit(fail ? 1 : 0);
})();
