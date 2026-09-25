/* ZXing Data Matrix 解码能力核验（免登录）
 * 验证前端能否用本地 vendor/zxing.min.js 解码 pylibdmtx 生成的 DM 图，
 * 覆盖干净图 / 放大 / 旋转 3° / 高斯模糊 四种拍摄条件。
 */
const path = require('path');
const fs = require('fs');
const { chromium } = require('playwright-core');

const BASE = process.argv[2] || 'http://127.0.0.1:8080';
const DIR = '/tmp/dmtests';

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
  const page = await browser.newPage();
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.addScriptTag({ path: path.join(__dirname, '..', 'frontend', 'vendor', 'zxing.min.js') });

  const info = await page.evaluate(() => ({
    loaded: !!window.ZXing,
    hasBrowserReader: !!(window.ZXing && window.ZXing.BrowserMultiFormatReader),
    hasDataMatrix: !!(window.ZXing && window.ZXing.BarcodeFormat && window.ZXing.BarcodeFormat.DATA_MATRIX),
  }));
  console.log('ZXing 加载:', JSON.stringify(info));
  if (!info.loaded) { await browser.close(); process.exit(1); }

  let pass = 0, total = 0;
  for (const f of ['raw', 'big', 'rot3', 'blur']) {
    const file = path.join(DIR, f + '.png');
    if (!fs.existsSync(file)) { console.log(`${f.padEnd(6)} 跳过（无文件）`); continue; }
    total++;
    const b64 = fs.readFileSync(file).toString('base64');
    const r = await page.evaluate(async (b64) => {
      const img = new Image();
      await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = 'data:image/png;base64,' + b64; });
      const hints = new Map();
      hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS, [ZXing.BarcodeFormat.DATA_MATRIX]);
      hints.set(ZXing.DecodeHintType.TRY_HARDER, true);
      const reader = new ZXing.BrowserMultiFormatReader(hints);
      try {
        const res = await reader.decodeFromImageElement(img);
        return { ok: true, text: res.getText() };
      } catch (e) { return { ok: false, err: String((e && e.message) || e) }; }
    }, b64);
    if (r.ok) pass++;
    console.log(`${f.padEnd(6)} ${r.ok ? 'OK   -> "' + r.text + '"' : 'FAIL -> ' + r.err}`);
  }

  console.log(`\n结果：${pass}/${total} 种拍摄条件解码成功`);
  await browser.close();
  process.exit(pass === total ? 0 : 1);
})();
