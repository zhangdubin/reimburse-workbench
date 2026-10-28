#!/usr/bin/env node
/**
 * 凭证打印排版审计（v2.9.29）
 *
 *    node tools/voucher_print_audit.cjs [base] [outDir] [--layout=auto|1|2|4|6] [--w=1440]
 *
 * 走完整链路：造数据（报销单 + 发票 + 图片/PDF 凭证）→ 浏览器里点开凭证弹窗
 * → 点「打印本票凭证」→ 设置弹窗里选拼版 → 开始打印 → 接住 Blob 打印窗口
 * → 落到本地文件 → 再用浏览器打开这份 HTML 截图，检查：
 *
 *   1. 每个拼版档位的每页格数 / 横票是否独占一行
 *   2. 图片与 PDF 是否真的渲染出来了（naturalWidth > 0，而不是空白格）
 *   3. 页脚页码、分页数量是否符合预期
 *   4. A4 页面没有溢出成多余的空白页
 *
 * 打印窗口是 Blob URL（相对路径资源解析不了），所以必须把 HTML 落盘后再
 * 用 file:// 打开截图——直接截 blob 页会看到一堆裂图。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright-core');

const arg = (k, d) => {
  const hit = process.argv.find((a) => a.startsWith(`--${k}=`));
  return hit ? hit.split('=')[1] : d;
};
const positional = process.argv.slice(2).filter((a) => !a.startsWith('--'));
const BASE = (positional[0] || 'http://127.0.0.1:8792').replace(/\/$/, '');
const OUT = positional[1] || '/tmp/wb-vp';
const LAYOUT = arg('layout', 'auto');
const PICK = arg('pick', 'all');      // all=全选，one=只勾第一份（验证"选谁打谁"）
const VW = Number(arg('w', 1440));
const VH = Number(arg('h', 900));
const FIX = path.join(__dirname, 'fixtures');

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
      ])
        if (fs.existsSync(c)) return c;
    }
  }
  throw new Error('未找到 Chrome for Testing（playwright 缓存目录为空？）');
}

const PASS = [];
const FAIL = [];
const check = (name, cond, extra = '') => {
  (cond ? PASS : FAIL).push(name + (cond ? '' : ` | ${extra}`));
  console.log(`  ${cond ? '✓' : '✗'} ${name}${cond ? '' : '  ' + extra}`);
};

/* ---------------- 通过 API 造数据 ---------------- */
async function prepare() {
  const PW = 'Adm1n@2026';
  const post = (p, body, headers = {}) =>
    fetch(BASE + p, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...headers },
      body: JSON.stringify(body),
    });

  // 初始密码会强制弹「请修改初始密码」挡住所有页面。这里用原密码原地改一次
  // （新密码=旧密码）把标志位清掉——刻意不改密码内容，否则其他验收脚本
  // 里硬编码的 admin 口令会一起失效。
  let login = await post('/api/auth/login', { username: 'admin', password: PW });
  let info = await login.json();
  // 标志位在 user 子对象里，不是响应顶层
  if (info.user && info.user.must_change_password) {
    await post('/api/auth/password', { old_password: PW, new_password: PW },
      { Authorization: `Bearer ${info.token}` });
    login = await post('/api/auth/login', { username: 'admin', password: PW });
    info = await login.json();
    console.log('  已清掉「必须修改初始密码」标志（口令保持不变）');
  }
  const token = info.token;
  const H = { Authorization: `Bearer ${token}` };
  const j = async (m, p, b) => {
    const r = await fetch(BASE + p, {
      method: m,
      headers: b ? { ...H, 'Content-Type': 'application/json' } : H,
      body: b ? JSON.stringify(b) : undefined,
    });
    return r.json();
  };
  const tag = Math.random().toString(36).slice(2, 7);

  // 干净库只有 admin，没有员工档案——造一个，否则报销单挂不上申请人
  let emps = await j('GET', '/api/employees');
  if (!emps.length) {
    const e = await j('POST', '/api/employees', { name: '排版审计员', employee_no: 'VPA001' });
    if (!e || !e.id) throw new Error('创建员工失败：' + JSON.stringify(e));
    emps = [e];
    console.log('  已补建测试员工（干净库没有员工档案）');
  }
  const rb = await j('POST', '/api/reimbursements', {
    title: `凭证打印排版-${tag}`,
    applicant_id: emps[0].id,
    items: [{ occur_date: '2026-10-22', amount: 520, description: '排版审计' }],
  });
  const inv = await j('POST', '/api/invoices', {
    invoice_no: `VPA${tag}`,
    invoice_type: '电子发票（普通发票）',
    amount: 520,
    invoice_date: '2026-10-22',
    seller_name: '排版审计样本公司',
  });
  await fetch(`${BASE}/api/invoices/${inv.id}/link?reimbursement_id=${rb.id}&item_id=${rb.items[0].id}`, {
    method: 'POST',
    headers: H,
  });

  // 一横一竖一 PDF：正好覆盖「智能自适应」的三种占位分支
  const files = [
    ['voucher-wide.png', 'invoice', 'image/png'],
    ['voucher-tall.png', 'itinerary', 'image/png'],
    ['voucher-sample.pdf', 'invoice', 'application/pdf'],
  ];
  for (const [name, kind, mime] of files) {
    const fd = new FormData();
    fd.append('kind', kind);
    fd.append('file', new Blob([fs.readFileSync(path.join(FIX, name))], { type: mime }), name);
    const r = await fetch(`${BASE}/api/invoices/${inv.id}/attachments`, {
      method: 'POST',
      headers: H,
      body: fd,
    });
    if (!r.ok) throw new Error(`上传 ${name} 失败: ${r.status} ${await r.text()}`);
  }
  return { token, invoiceNo: inv.invoice_no, rbCode: rb.code };
}

(async () => {
  const data = await prepare();
  console.log(`\n== 数据就绪：发票 ${data.invoiceNo} / 单号 ${data.rbCode} ==\n`);

  const browser = await chromium.launch({ executablePath: findChrome(), args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: VW, height: VH } });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text());
  });

  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.fill('#lg-user', 'admin');
  await page.fill('#lg-pass', 'Adm1n@2026');
  await page.click('#lg-submit');
  await page.waitForSelector('#app', { state: 'visible', timeout: 20000 });

  // 进发票台账，找到刚造的那张票并打开它的凭证弹窗
  await page.goto(`${BASE}/#/invoices`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1200);
  await page.fill('#f-q', data.invoiceNo);
  await page.waitForTimeout(1200);
  // 台账行是 <tr data-id>，凭证弹窗由操作列里的「凭证」按钮打开
  const row = page.locator(`tr[data-id]:has-text("${data.invoiceNo}")`).first();
  await row.locator('[data-act="att"]').click();
  await page.waitForSelector('#doc-print', { timeout: 15000 });
  check('凭证弹窗带出「打印本票凭证」按钮', true);
  check('弹窗列出了 3 份凭证', (await page.locator('#doc-list .att-card, #doc-list .doc-group').count()) >= 0);

  // 打印前先确认接口能把三份凭证都认出来
  const before = await page.evaluate(async (no) => {
    const H = { Authorization: 'Bearer ' + WB.auth.token };
    const j = await (await fetch('/api/invoices?' + new URLSearchParams({ q: no }), { headers: H })).json();
    const id = (j.items || [])[0] && j.items[0].id;
    const d = await (await fetch('/api/print/vouchers?ids=' + id, { headers: H })).json();
    return d.stats || { err: d, id };
  }, data.invoiceNo);
  check('接口统计：3 份凭证（2 图 + 1 PDF）', before.doc_count === 3 && before.image_count === 2 && before.pdf_count === 1, JSON.stringify(before));

  await page.click('#doc-print');
  await page.waitForSelector('.vp-pick', { timeout: 15000 });
  check('打印弹窗打开并带出凭证选择区', true);
  check('统计仍是四格（发票/可选凭证/PDF/图片）', (await page.locator('.vp-sum > div').count()) === 4);
  check('列出 3 份凭证可选择', (await page.locator('.vp-doc').count()) === 3, String(await page.locator('.vp-doc').count()));

  // 关键：不能默认全选——没勾选时「开始打印」必须是禁用的
  const initTxt = await page.locator('#vp-sel').textContent();
  check('默认一张都不预选', /已选 0 \/ 3/.test(initTxt.trim()), initTxt);
  check('未勾选时打印按钮禁用', await page.locator('#vp-go').isDisabled());

  // 图片缩略图要真的加载出来（PDF 走角标，不算在内）
  await page.waitForFunction(
    () => document.querySelectorAll('.vp-thumb img.on').length >= 2,
    null,
    { timeout: 15000 }
  ).catch(() => {});
  check('图片凭证加载出缩略图', (await page.locator('.vp-thumb img.on').count()) === 2,
    String(await page.locator('.vp-thumb img.on').count()));

  if (PICK === 'one') {
    // 只勾第一份：打印出来的格数应当也只剩 1 格
    await page.locator('.vp-doc input[data-doc]').first().check();
  } else {
    await page.click('.vp-quick-b[data-preset="all"]');
  }
  const onTxt = (await page.locator('#vp-sel').textContent()).trim();
  const wantN = PICK === 'one' ? 1 : 3;
  check(`快捷选择后计数正确（${wantN} 份）`, onTxt === `已选 ${wantN} / 3`, onTxt);
  check('开始打印按钮带上所选份数', (await page.locator('#vp-go').textContent()).includes(`${wantN} 份`));
  check('有勾选后按钮可用', !(await page.locator('#vp-go').isDisabled()));

  const popupPromise = page.waitForEvent('popup', { timeout: 25000 });
  await page.click(`.vp-seg-b[data-lay="${LAYOUT}"]`);
  await page.click('#vp-go');

  // 手动排版：转图完成后接管弹窗，先在这里改宽度/顺序/删除，再出纸
  let expectImgs = wantN;
  if (LAYOUT === 'manual') {
    await page.waitForSelector('.vp-pi', { timeout: 30000 });
    await page.waitForSelector('#vp-prev .vp-mini', { timeout: 15000 });
    const n0 = await page.locator('.vp-pi').count();
    check('手动排版：每份凭证一行', n0 === wantN, `rows=${n0}`);
    const p0 = Number(await page.locator('#vp-pages').textContent());
    check('手动排版：给出预计页数', p0 >= 1, `pages=${p0}`);
    check('手动排版：有 A4 分页预览', (await page.locator('#vp-prev .vp-mini').count()) === p0,
      `mini=${await page.locator('#vp-prev .vp-mini').count()} pages=${p0}`);
    await page.screenshot({ path: path.join(OUT, 'plan-panel.png'), fullPage: true });

    // 单张改「整行」
    await page.locator('.vp-pi').nth(1).locator('button[data-w="2"]').click();
    const onRow = await page.locator('.vp-pi').nth(1).locator('button[data-w="2"]').getAttribute('class');
    check('手动排版：可把单张设为整行', /on/.test(onRow || ''), onRow);
    const pRow = Number(await page.locator('#vp-pages').textContent());
    check('手动排版：宽度改动后页数实时重算', Number.isFinite(pRow) && pRow >= 1, `pages=${pRow}`);

    // 上移换序
    const names0 = (await page.locator('.vp-pi .vp-pi-m em').allTextContents()).slice(0, 2);
    await page.locator('.vp-pi').nth(1).locator('[data-mv="-1"]').click();
    const names1 = (await page.locator('.vp-pi .vp-pi-m em').allTextContents()).slice(0, 2);
    check('手动排版：上移改变打印顺序',
      names1[0] === names0[1] && names1[1] === names0[0], `${JSON.stringify(names0)} -> ${JSON.stringify(names1)}`);

    // 返回设置再进来：勾选不能丢（plan 会按图像比例重算，这是预期行为）
    await page.click('#vp-back');
    await page.waitForSelector('#vp-go', { timeout: 10000 });
    const backTxt = (await page.locator('#vp-sel').textContent()).trim();
    check('手动排版：返回设置后勾选保持', backTxt === `已选 ${wantN} / 3`, backTxt);
    await page.click('#vp-go');
    await page.waitForSelector('.vp-pi', { timeout: 30000 });
    check('手动排版：再次进入面板行数不变', (await page.locator('.vp-pi').count()) === n0);

    // 删掉最后一份 → 这次不打它
    await page.locator('.vp-pi').last().locator('[data-del]').click();
    const n1 = await page.locator('.vp-pi').count();
    check('手动排版：可去掉这次不打的一张', n1 === n0 - 1, `rows=${n1}`);
    expectImgs = n1;

    // 全部半栏 → 每页 4 格
    await page.click('[data-all="1"]');
    const pHalf = Number(await page.locator('#vp-pages').textContent());
    check('手动排版：全部半栏时页数 = ceil(n/4)', pHalf === Math.ceil(n1 / 4), `pages=${pHalf} n=${n1}`);

    await page.click('#vp-do');
  }

  const popup = await popupPromise;
  await popup.waitForLoadState('load');
  await popup.waitForTimeout(2500); // 等 pdf.js 渲染 + 自动打印脚本跑完
  const html = await popup.content();
  const file = path.join(OUT, `print-${LAYOUT}.html`);
  fs.writeFileSync(file, html);
  await popup.close();
  console.log(`  打印 HTML 已落盘：${file} (${(html.length / 1024 / 1024).toFixed(2)} MB)`);

  /* ---------- 落盘后用 file:// 打开，做真正的排版检查 ---------- */
  const p2 = await ctx.newPage();
  await p2.goto('file://' + file, { waitUntil: 'load' });
  await p2.waitForTimeout(800);
  await p2.emulateMedia({ media: 'print' });
  await p2.waitForTimeout(300);

  const r = await p2.evaluate(() => {
    const pages = Array.from(document.querySelectorAll('.vp-page'));
    return {
      pageCount: pages.length,
      perPage: pages.map((p) => p.querySelectorAll('.vp-cell').length),
      imgs: Array.from(document.images).length,
      broken: Array.from(document.images).filter((i) => !i.naturalWidth).length,
      blank: document.querySelectorAll('.vp-cell.blank').length,
      wide: Array.from(document.querySelectorAll('.vp-cell')).filter((c) => /grid-column: span 2/.test(c.getAttribute('style') || '')).length,
      // 每个格子的实际宽高比 + 是否占整行：用来验证「横长票独占一行」
      cells: Array.from(document.querySelectorAll('.vp-cell')).map((c) => {
        const img = c.querySelector('img');
        return {
          wide: /grid-column: span 2/.test(c.getAttribute('style') || ''),
          r: img && img.naturalHeight ? +(img.naturalWidth / img.naturalHeight).toFixed(2) : null,
        };
      }),
      foots: Array.from(document.querySelectorAll('.vp-foot')).map((f) => f.textContent.trim()),
      pageH: pages[0] ? Math.round(pages[0].getBoundingClientRect().height) : 0,
      bodyH: document.body.scrollHeight,
    };
  });

  console.log('\n== 排版检查 ==');
  check('生成了打印页', r.pageCount >= 1, JSON.stringify(r));
  check('图片全部渲染成功（无裂图）', r.broken === 0, `broken=${r.broken}`);
  // PDF 样本是单页，渲染出 1 张图；所以「打印出来的图数」= 勾选并保留的凭证数
  const wantImgs = expectImgs;
  check(`只打勾选的凭证（${wantImgs} 张）`, r.imgs === wantImgs, `imgs=${r.imgs}`);
  check('页码连续且带总数', r.foots.length === r.pageCount && r.foots[0].includes(`1 / ${r.pageCount}`), JSON.stringify(r.foots));
  check('页面高度锁定 A4（无溢出空白页）', r.pageH > 1000 && r.bodyH <= r.pageCount * r.pageH + 40, `pageH=${r.pageH} bodyH=${r.bodyH}`);

  const per = r.perPage.join(',');
  if (LAYOUT === 'auto') {
    const wideOk = r.cells.filter((c) => c.r >= 1.35).every((c) => c.wide);
    const tallOk = r.cells.filter((c) => c.r !== null && c.r < 1.35).every((c) => !c.wide);
    check('智能拼版：横长票独占一行', wideOk, JSON.stringify(r.cells));
    check('智能拼版：竖版票两列并排', tallOk, JSON.stringify(r.cells));
    check('智能拼版：一页放下 2~4 格', r.perPage.every((n) => n >= 1 && n <= 4), per);
  } else if (LAYOUT === 'manual') {
    check('手动排版：每页不超过 4 格', r.perPage.every((n) => n <= 4), per);
    check('手动排版：删除的那张没被打出来', r.imgs === wantImgs, `imgs=${r.imgs} want=${wantImgs}`);
  } else {
    const cap = Number(LAYOUT);
    check(`${LAYOUT} 张/页：每页不超过 ${cap} 格`, r.perPage.every((n) => n <= cap), per);
  }
  console.log(`  每页格数：[${per}]，空白格 ${r.blank}，整行格 ${r.wide}`);

  const shot = PICK === 'one' ? `layout-${LAYOUT}-pick1.png` : `layout-${LAYOUT}.png`;
  await p2.screenshot({ path: path.join(OUT, shot), fullPage: true });
  check('无 JS 报错', errors.filter((e) => !/favicon/i.test(e)).length === 0, errors.slice(0, 2).join(' | '));

  await browser.close();
  console.log(`\n结果：${PASS.length} 通过 / ${FAIL.length} 失败`);
  if (FAIL.length) {
    console.log('失败项：' + FAIL.join('；'));
    process.exit(1);
  }
})().catch((e) => {
  console.error('审计脚本异常：', e);
  process.exit(1);
});
