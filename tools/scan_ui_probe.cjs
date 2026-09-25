/* 扫码核验功能端到端探针（免登录）
 *
 * 验证链路：真实 Data Matrix 图片 → zxing 解码 → /api/scan/resolve → 打开详情
 *
 * 生产库需要口令，所以这里：
 *   - 前端脚本（util.js / api.js / scanner.js / zxing.min.js）用生产真实文件；
 *   - 只把 WB.api.get 与 WB.openReimbursement / WB.openInvoice 换成桩，
 *     记录调用参数；解码那一步是真跑的（真图、真 zxing）。
 *   这样「解码是否有效」「解析后跳去哪个记录」都被真实覆盖，
 *   后端自身的识别与权限由 backend/scan_test.py 覆盖。
 */
const path = require('path');
const fs = require('fs');
const { chromium } = require('playwright-core');

const BASE = process.argv[2] || 'http://127.0.0.1:8080';
const DM_IMG = process.argv[3] || '/tmp/dmtests/big.png'; // 内容 BX20260924001

function findChrome() {
  const c = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
  ];
  for (const p of c) if (fs.existsSync(p)) return p;
  throw new Error('找不到本机 Chrome');
}

(async () => {
  const browser = await chromium.launch({
    executablePath: findChrome(), headless: true,
    args: ['--no-sandbox', '--disable-features=Translate'],
  });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  const out = [];
  const ok = (n, c, x) => out.push(`${c ? 'PASS' : 'FAIL'}  ${n}${x ? '  —— ' + x : ''}`);

  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);

  const dmB64 = fs.readFileSync(DM_IMG).toString('base64');

  const r = await page.evaluate(async (b64) => {
    const logs = [];
    const calls = [];

    // ---- 桩：只替换网络与跳转，其余（解码）用真实实现 ----
    WB.api.get = async (p, params) => {
      logs.push('GET ' + p + (params ? '?' + new URLSearchParams(params).toString() : ''));
      if (p === '/api/scan/resolve') {
        const c = ((params && params.code) || '').trim();
        if (c.toUpperCase().startsWith('INV-LIST')) {
          return { found: true, type: 'invoice_list', id: null, code: c, title: '发票清单', subtitle: '打印批次 ' + c };
        }
        if (/^[A-Za-z]{2}/.test(c)) {
          return c.toUpperCase() === 'BX20260924001'
            ? { found: true, type: 'reimbursement', id: 7, code: 'BX20260924001', title: '差旅费报销', subtitle: '张三 · 技术部 · ¥1,234.56 · 待审批' }
            : { found: false, hint: '没有找到该编号对应的记录' };
        }
        if (/^\d{8,20}$/.test(c)) {
          return { found: true, type: 'invoice', id: 9, code: c, title: '发票 ' + c, subtitle: '某某科技 · ¥600.00' };
        }
        return { found: false, hint: '没有找到该编号对应的记录' };
      }
      return {};
    };
    WB.openReimbursement = (id) => calls.push({ fn: 'openReimbursement', id });
    WB.openInvoice = (id) => calls.push({ fn: 'openInvoice', id });

    const root = document.createElement('div');
    root.id = 'wb-scan-root';
    root.style.cssText = 'position:fixed;inset:0;z-index:9999;padding:24px;background:#f5f7fa;overflow:auto;';
    document.body.appendChild(root);
    await WB.views.scan(root);

    const ret = {
      zxing: !!window.ZXing,
      hasZxingFile: !!document.querySelector('script[src*="zxing.min.js"]'),
      hasScanner: typeof WB.views.scan === 'function',
      hasInput: !!root.querySelector('#scan-input'),
      hasFile: !!root.querySelector('#scan-file'),
      hasDesktopPicker: (() => {
        const l = Array.from(root.querySelectorAll('label[for="scan-file"]'));
        if (!l.length) return false;
        return getComputedStyle(l[0]).display !== 'none';
      })(),
      title: null,
      manual: null, invoice: null, decode: null, missing: null,
      status: {},
    };

    const $ = (s) => root.querySelector(s);

    // ① 手工输入报销单号
    $('#scan-input').value = 'BX20260924001';
    $('#scan-go').click();
    await new Promise((res) => setTimeout(res, 400));
    ret.manual = JSON.parse(JSON.stringify(calls));
    ret.status.manual = $('#scan-status').textContent;

    // ② 手工输入发票号
    calls.length = 0;
    $('#scan-input').value = '26912000000479801234';
    $('#scan-go').click();
    await new Promise((res) => setTimeout(res, 400));
    ret.invoice = JSON.parse(JSON.stringify(calls));

    // ③ 真实解码：把 Data Matrix 图片塞进文件输入框
    calls.length = 0;
    const blob = await (await fetch('data:image/png;base64,' + b64)).blob();
    const dt = new DataTransfer();
    dt.items.add(new File([blob], 'dm.png', { type: 'image/png' }));
    const inp = $('#scan-file');
    inp.files = dt.files;
    inp.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise((res) => setTimeout(res, 4000));
    ret.decode = JSON.parse(JSON.stringify(calls));
    ret.status.decode = $('#scan-status').textContent;
    ret.previewImg = !!$('#scan-stage img');

    // ④ 扫一个系统里没有的号
    calls.length = 0;
    $('#scan-input').value = 'BX99999999999';
    $('#scan-go').click();
    await new Promise((res) => setTimeout(res, 400));
    ret.missing = JSON.parse(JSON.stringify(calls));
    ret.status.missing = $('#scan-status').textContent;

    ret.logs = logs;
    return ret;
  }, dmB64);

  console.log('环境:', JSON.stringify({ zxing: r.zxing, zxingFile: r.hasZxingFile, scanner: r.hasScanner }));

  // 留一张扫码页截图，方便人工核对版式（未登录时 #app 是登录页，先藏掉）
  const shot = path.join(__dirname, '..', 'docs', 'screenshots', 'scan-view.png');
  fs.mkdirSync(path.dirname(shot), { recursive: true });
  await page.evaluate(() => {
    const app = document.getElementById('app');
    if (app) app.style.display = 'none';
  });
  await page.setViewportSize({ width: 1280, height: 820 });
  await page.screenshot({ path: shot });
  console.log('截图:', shot);

  ok('zxing.min.js 已由页面加载', r.zxing && r.hasZxingFile);
  ok('扫码视图可渲染（输入框/文件框存在）', r.hasScanner && r.hasInput && r.hasFile);
  ok('桌面端有「选择图片识别」入口（m-camera 拍照按钮在桌面被隐藏）',
    r.hasDesktopPicker,
    r.hasDesktopPicker ? '' : '桌面无取像入口将导致电脑上无法扫码');

  ok('手工输入报销单号 → 打开报销单详情',
    r.manual.length === 1 && r.manual[0].fn === 'openReimbursement' && r.manual[0].id === 7,
    JSON.stringify(r.manual));

  ok('手工输入发票号 → 打开发票详情',
    r.invoice.length === 1 && r.invoice[0].fn === 'openInvoice' && r.invoice[0].id === 9,
    JSON.stringify(r.invoice));

  ok('真实 DM 图片解码 → 打开报销单详情',
    r.decode.length === 1 && r.decode[0].fn === 'openReimbursement' && r.decode[0].id === 7,
    JSON.stringify(r.decode) + ' | 状态=' + r.status.decode);
  ok('解码后展示拍到的图（回显预览）', r.previewImg);

  ok('扫到不存在的号 → 不跳转、给提示',
    r.missing.length === 0 && /没有找到/.test(r.status.missing || ''),
    JSON.stringify(r.missing) + ' | 状态=' + r.status.missing);

  console.log('\n请求日志:', JSON.stringify(r.logs));
  console.log('\n' + out.join('\n'));
  const fail = out.filter((x) => x.startsWith('FAIL')).length;
  console.log(`\n结果：${out.length - fail}/${out.length} 通过`);

  await browser.close();
  process.exit(fail ? 1 : 0);
})();
