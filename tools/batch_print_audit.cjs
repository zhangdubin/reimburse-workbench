#!/usr/bin/env node
/**
 * 报销单批量打印审计（v2.9.37）
 *
 *   node tools/batch_print_audit.cjs [base] [outDir]
 *
 * 走完整链路：造 3 张报销单 → 列表页全选 → 点「批量打印」→ 弹窗里确认
 * → 去掉一张再确认 → 接住打印窗口 → 落盘 → file:// 打开检查：
 *
 *   1. 弹窗列出了勾选的单据（单号/申请人/金额/状态），可二次去勾选
 *   2. 打印文档里每张单各占一段，单据之间有 n-1 个分页符（不多打空白页）
 *   3. 每张单页尾都有自己的 Data Matrix 核验码（不是只有最后一张有）
 *   4. 去掉一张后，那份单的编号不再出现在打印文档里
 *   5. 全流程无 JS 报错
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright-core');

const positional = process.argv.slice(2).filter((a) => !a.startsWith('--'));
const BASE = (positional[0] || 'http://127.0.0.1:8792').replace(/\/$/, '');
const OUT = positional[1] || '/tmp/wb-bp';
fs.mkdirSync(OUT, { recursive: true });

function findChrome() {
  for (const root of [
    path.join(os.homedir(), 'Library/Caches/ms-playwright'),
    path.join(os.homedir(), '.cache/ms-playwright'),
  ]) {
    if (!fs.existsSync(root)) continue;
    for (const d of fs.readdirSync(root).filter((x) => /^chromium-\d+$/.test(x))) {
      for (const c of [
        path.join(root, d, 'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'),
        path.join(root, d, 'chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'),
        path.join(root, d, 'chrome-linux/chrome'),
      ]) if (fs.existsSync(c)) return c;
    }
  }
  throw new Error('未找到 Chrome for Testing');
}

const PASS = [];
const FAIL = [];
const check = (name, cond, extra = '') => {
  (cond ? PASS : FAIL).push(name + (cond ? '' : ` | ${extra}`));
  console.log(`  ${cond ? '✓' : '✗'} ${name}${cond ? '' : '  ' + extra}`);
};

async function prepare() {
  const PW = 'Adm1n@2026';
  const post = (p, b, h = {}) => fetch(BASE + p, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...h }, body: JSON.stringify(b),
  });
  let info = await (await post('/api/auth/login', { username: 'admin', password: PW })).json();
  if (info.user && info.user.must_change_password) {
    await post('/api/auth/password', { old_password: PW, new_password: PW }, { Authorization: `Bearer ${info.token}` });
    info = await (await post('/api/auth/login', { username: 'admin', password: PW })).json();
  }
  const H = { Authorization: `Bearer ${info.token}` };
  const j = async (m, p, b) => (await fetch(BASE + p, {
    method: m, headers: b ? { ...H, 'Content-Type': 'application/json' } : H,
    body: b ? JSON.stringify(b) : undefined,
  })).json();

  let emps = await j('GET', '/api/employees');
  if (!emps.length) emps = [await j('POST', '/api/employees', { name: '批量打印审计员', employee_no: 'BPA001' })];
  const tag = 'BPA' + Math.random().toString(36).slice(2, 6).toUpperCase();
  const codes = [];
  for (let i = 0; i < 3; i++) {
    const rb = await j('POST', '/api/reimbursements', {
      title: `批量打印样本-${i + 1}-${tag}`,
      applicant_id: emps[0].id,
      items: [{
        occur_date: '2026-10-22', amount: 100 * (i + 1) + 20,
        description: `样本明细 ${i + 1}`,
      }],
    });
    codes.push(rb.code);
  }
  return { tag, codes };
}

(async () => {
  const data = await prepare();
  console.log(`\n== 数据就绪：${data.codes.join(' / ')} ==\n`);

  const browser = await chromium.launch({ executablePath: findChrome(), args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });

  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.fill('#lg-user', 'admin');
  await page.fill('#lg-pass', 'Adm1n@2026');
  await page.click('#lg-submit');
  await page.waitForSelector('#app', { state: 'visible', timeout: 20000 });

  await page.goto(`${BASE}/#/reimbursements`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1200);
  await page.fill('#f-q', data.tag);
  await page.waitForTimeout(1200);
  const rows = await page.locator('tr[data-id]').count();
  check('列表筛出 3 张样本单', rows === 3, String(rows));

  await page.click('#ck-all');
  await page.waitForTimeout(300);
  check('批量栏出现批量打印按钮', await page.locator('#rb-print').isVisible());

  await page.click('#rb-print');
  await page.waitForSelector('#bpr-ok', { timeout: 15000 });
  const listed = await page.locator('tbody .bpr-ck').count();
  check('弹窗列出 3 张待打单据', listed === 3, String(listed));
  check('弹窗默认全选', (await page.locator('tbody .bpr-ck:checked').count()) === 3);
  const sum0 = (await page.locator('#bpr-sum').textContent()).trim();
  check('合计金额已算出', /^[¥￥]?[\d,]+\.\d{2}$/.test(sum0.replace(/\s/g, '')), sum0);
  await page.screenshot({ path: path.join(OUT, 'dialog.png') });

  // 去掉最后一张（模拟「这张不打」）
  await page.locator('tbody .bpr-ck').last().uncheck();
  await page.waitForTimeout(200);
  check('去勾选后按钮计数跟着变', (await page.locator('#bpr-n').textContent()).trim() === '2',
    await page.locator('#bpr-n').textContent());

  const popupP = page.waitForEvent('popup', { timeout: 30000 });
  await page.click('#bpr-ok');
  const popup = await popupP;
  await popup.waitForLoadState('load');
  await popup.waitForTimeout(2000);
  const html = await popup.content();
  fs.writeFileSync(path.join(OUT, 'print-batch.html'), html);
  await popup.close();

  const breaks = (html.match(/class="pg-break"/g) || []).length;
  check('两张单之间 1 个分页符（末尾不多打空白页）', breaks === 1, String(breaks));
  const pages = (html.match(/class="page"/g) || []).length;
  check('打印文档含 2 张单据版式', pages === 2, String(pages));
  const corners = (html.match(/class="bc-corner"/g) || []).length;
  check('每张单各带自己的核验码', corners === 2, String(corners));
  const hit = data.codes.filter((c) => html.includes(c));
  check('只打出勾选的那两张', hit.length === 2, hit.join(','));
  check('无 JS 报错', errors.length === 0, errors.slice(0, 3).join(' | '));

  // 落盘后再开一次，用于人工看版式
  const p2 = await ctx.newPage();
  await p2.goto('file://' + path.join(OUT, 'print-batch.html'), { waitUntil: 'load' });
  await p2.waitForTimeout(600);
  await p2.emulateMedia({ media: 'print' });
  await p2.waitForTimeout(300);
  await p2.screenshot({ path: path.join(OUT, 'sheet.png'), fullPage: true });

  await browser.close();
  console.log(`\n结果：${PASS.length} 通过 / ${FAIL.length} 失败`);
  if (FAIL.length) process.exit(1);
})().catch((e) => { console.error('失败：', e); process.exit(1); });
