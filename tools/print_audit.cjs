/* 打印功能核验：
 *   1) WB.print 模块已加载；
 *   2) 报销单详情「打印」按钮 → 弹出打印窗口，截图 A4 版式；
 *   3) 发票台账「打印清单」按钮（勾选 2 张）→ 截图；
 *   4) 单张发票「打印」按钮 → 截图。
 * 产物：docs/screenshots/print/*.png + 控制台断言结果。
 */
const path = require('path');
const fs = require('fs');
const { chromium } = require('playwright-core');

const BASE = process.argv[2] || 'http://127.0.0.1:8801';
const OUT = path.join(__dirname, '..', 'docs', 'screenshots', 'print');
fs.mkdirSync(OUT, { recursive: true });

function findChrome() {
  const candidates = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
  ];
  for (const c of candidates) if (fs.existsSync(c)) return c;
  throw new Error('找不到本机 Chrome');
}

(async () => {
  const browser = await chromium.launch({
    executablePath: findChrome(),
    headless: true,
    args: ['--no-sandbox', '--disable-features=Translate'],
  });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const results = [];
  const check = (name, ok, extra) => {
    results.push(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? '  —— ' + extra : ''}`);
  };

  await page.goto(BASE + '/#/reimbursements', { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => window.location.reload());
  await page.waitForTimeout(500);

  // 登录
  await page.fill('#lg-user', 'admin');
  await page.fill('#lg-pass', 'Adm1n@2026');
  await page.click('button[type=submit]');
  await page.waitForTimeout(2500);

  // 1) print 模块
  const hasPrint = await page.evaluate(() => !!(window.WB && WB.print && WB.print.reimbursement && WB.print.invoices && WB.print.invoice));
  check('WB.print 模块加载', hasPrint);

  // 2) 报销单详情打印
  await page.waitForSelector('table.tbl tbody tr', { timeout: 8000 });
  await page.click('table.tbl tbody tr');
  await page.waitForTimeout(1200);
  const hasBtn = await page.evaluate(() => !!Array.from(document.querySelectorAll('.modal-mask button')).find((b) => b.textContent.trim() === '打印'));
  check('报销单详情有「打印」按钮', hasBtn);

  const [detailPop] = await Promise.all([
    ctx.waitForEvent('page', { timeout: 8000 }).catch(() => null),
    (async () => {
      for (const b of await page.$$('.modal-mask button')) {
        if ((await b.textContent()).trim() === '打印') return b.click();
      }
    })(),
  ]);
  if (detailPop) {
    await detailPop.waitForTimeout(900);
    await detailPop.emulateMedia({ media: 'print' });
    await detailPop.screenshot({ path: path.join(OUT, 'print-reimbursement.png'), fullPage: true });
    const t = await detailPop.title();
    check('报销单打印窗口弹出', /BX/.test(t), `标题=${t}`);
    // 抓正文断言关键区块
    const bodyTxt = await detailPop.evaluate(() => document.body.innerText);
    check('打印页含「费用报销单」抬头', bodyTxt.includes('费用报销单'));
    check('打印页含合计大写', /大写：/.test(bodyTxt));
    check('打印页含签字栏', bodyTxt.includes('财务审核') && bodyTxt.includes('审批人'));
    await detailPop.close();
  } else check('报销单打印窗口弹出', false, '没有捕获到新窗口');

  await page.keyboard.press('Escape').catch(() => {});
  await page.evaluate(() => { const m = document.querySelector('.modal-mask'); if (m) m.remove(); });

  // 3) 发票清单打印（勾选 2 张）
  await page.goto(BASE + '/#/invoices', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2200);
  await page.waitForSelector('.ck-row', { timeout: 8000 });
  const boxes = await page.$$('.ck-row');
  if (boxes[0]) await boxes[0].check();
  if (boxes[1]) await boxes[1].check();
  const [invPop] = await Promise.all([
    ctx.waitForEvent('page', { timeout: 8000 }).catch(() => null),
    page.click('#btn-print'),
  ]);
  if (invPop) {
    await invPop.waitForTimeout(900);
    await invPop.emulateMedia({ media: 'print' });
    await invPop.screenshot({ path: path.join(OUT, 'print-invoices.png'), fullPage: true });
    const bodyTxt = await invPop.evaluate(() => document.body.innerText);
    check('发票清单打印窗口弹出', bodyTxt.includes('发票清单'));
    check('清单含勾选说明', bodyTxt.includes('勾选 2 张'));
    await invPop.close();
  } else check('发票清单打印窗口弹出', false, '没有捕获到新窗口');

  // 4) 单张发票打印
  await page.waitForSelector('button[data-act="print"]', { timeout: 8000 });
  const [onePop] = await Promise.all([
    ctx.waitForEvent('page', { timeout: 8000 }).catch(() => null),
    page.click('button[data-act="print"]'),
  ]);
  if (onePop) {
    await onePop.waitForTimeout(900);
    await onePop.emulateMedia({ media: 'print' });
    await onePop.screenshot({ path: path.join(OUT, 'print-invoice-one.png'), fullPage: true });
    check('单张发票打印窗口弹出', true);
    await onePop.close();
  } else check('单张发票打印窗口弹出', false, '没有捕获到新窗口');

  console.log('\n' + results.join('\n'));
  const fail = results.filter((r) => r.startsWith('FAIL')).length;
  console.log(`\n结果：${results.length - fail}/${results.length} 通过`);
  await browser.close();
  process.exit(fail ? 1 : 0);
})();
