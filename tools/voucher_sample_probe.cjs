/* 疑难凭证样本探针：用 Playwright 路由把本地样本文件「假装」成同源资源，
 * 在真浏览器里跑与 voucher_print.js 相同的解码链路，判断哪些会白屏/失败。
 *   node tools/voucher_sample_probe.cjs http://127.0.0.1:8792 /tmp/vp_samples
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright-core');

const BASE = process.argv[2] || 'http://127.0.0.1:8792';
const DIR = process.argv[3] || '/tmp/vp_samples';

function findChrome() {
  const caches = [path.join(os.homedir(), 'Library/Caches/ms-playwright'),
                  path.join(os.homedir(), '.cache/ms-playwright')];
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

const MIME = { '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
               '.webp': 'image/webp', '.pdf': 'application/pdf' };

const RUN = `(async (files) => {
  const out = [];
  const ensurePdfjs = async () => {
    if (window.pdfjsLib) return window.pdfjsLib;
    await new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = 'vendor/pdf/pdf.min.js';
      s.onload = res; s.onerror = () => rej(new Error('pdf.min.js 加载失败'));
      document.head.appendChild(s);
    });
    window.pdfjsLib.GlobalWorkerOptions.workerSrc = 'vendor/pdf/pdf.worker.min.js';
    return window.pdfjsLib;
  };
  const statsOf = (cv) => {
    const s = document.createElement('canvas');
    s.width = 40; s.height = 40;
    const c = s.getContext('2d');
    c.fillStyle = '#fff'; c.fillRect(0, 0, 40, 40);
    c.drawImage(cv, 0, 0, 40, 40);
    const d = c.getImageData(0, 0, 40, 40).data;
    let sum = 0, ink = 0;
    for (let i = 0; i < d.length; i += 4) {
      const g = d[i] * .299 + d[i+1] * .587 + d[i+2] * .114;
      sum += g; if (g < 230) ink++;
    }
    const n = d.length / 4;
    return { lum: +(sum / n).toFixed(1), ink: +(100 * ink / n).toFixed(1) };
  };
  const drawTo = (src, w, h, maxSide) => {
    const long = Math.max(w, h) || 1, sc = Math.min(1, maxSide / long);
    const cv = document.createElement('canvas');
    cv.width = Math.max(1, Math.round(w * sc));
    cv.height = Math.max(1, Math.round(h * sc));
    const c = cv.getContext('2d');
    c.fillStyle = '#fff'; c.fillRect(0, 0, cv.width, cv.height);
    c.drawImage(src, 0, 0, cv.width, cv.height);
    return cv;
  };

  for (const f of files) {
    const rec = { name: f.name, kind: f.name.endsWith('.pdf') ? 'pdf' : 'image' };
    try {
      const r = await fetch(f.url);
      const blob = await r.blob();
      rec.bytes = blob.size;
      rec.ctype = r.headers.get('content-type') || '';
      if (rec.kind === 'pdf') {
        const lib = await ensurePdfjs();
        const doc = await lib.getDocument({ data: await blob.arrayBuffer() }).promise;
        rec.numPages = doc.numPages;
        rec.pages = [];
        for (let p = 1; p <= Math.min(doc.numPages, 5); p++) {
          const page = await doc.getPage(p);
          const base = page.getViewport({ scale: 1 });
          const vp = page.getViewport({ scale: 1600 / Math.max(base.width, base.height) });
          const cv = document.createElement('canvas');
          cv.width = Math.round(vp.width); cv.height = Math.round(vp.height);
          const c = cv.getContext('2d');
          c.fillStyle = '#fff'; c.fillRect(0, 0, cv.width, cv.height);
          await page.render({ canvasContext: c, viewport: vp }).promise;
          rec.pages.push(Object.assign({ wh: cv.width + '×' + cv.height }, statsOf(cv)));
          page.cleanup();
        }
        await doc.destroy();
        rec.ok = true;
      } else {
        try {
          const bmp = await createImageBitmap(blob);
          rec.wh = bmp.width + '×' + bmp.height;
          rec.via = 'createImageBitmap';
          Object.assign(rec, statsOf(drawTo(bmp, bmp.width, bmp.height, 1600)));
          if (bmp.close) bmp.close();
          rec.ok = true;
        } catch (e1) {
          rec.bmpErr = String(e1 && e1.message || e1);
          const img = await new Promise((res, rej) => {
            const i = new Image(), u = URL.createObjectURL(blob);
            i.onload = () => { URL.revokeObjectURL(u); res(i); };
            i.onerror = () => { URL.revokeObjectURL(u); rej(new Error('img 解码失败')); };
            i.src = u;
          });
          rec.wh = img.naturalWidth + '×' + img.naturalHeight;
          rec.via = 'Image()';
          Object.assign(rec, statsOf(drawTo(img, img.naturalWidth, img.naturalHeight, 1600)));
          rec.ok = true;
        }
      }
    } catch (e) {
      rec.ok = false;
      rec.err = String(e && e.message || e);
    }
    out.push(rec);
  }
  return out;
})`;

(async () => {
  const files = fs.readdirSync(DIR).filter(f => MIME[path.extname(f).toLowerCase()]);
  const browser = await chromium.launch({ executablePath: findChrome(), args: ['--no-sandbox'] });
  const page = await (await browser.newContext({ viewport: { width: 1200, height: 800 } })).newPage();
  const list = [];
  await page.route('**/__sample/**', (route) => {
    const name = decodeURIComponent(route.request().url().split('/__sample/')[1].split('?')[0]);
    const fp = path.join(DIR, name);
    if (!fs.existsSync(fp)) return route.fulfill({ status: 404, body: 'nf' });
    route.fulfill({
      status: 200,
      contentType: MIME[path.extname(name).toLowerCase()] || 'application/octet-stream',
      body: fs.readFileSync(fp),
    });
  });
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  // 静态资源探针不需要登录态，直接换到公开的登录页上下文即可
  const parsed = files.map((f, i) => { const url = '/__sample/' + encodeURIComponent(f); list.push(url); return { name: f, url }; });

  const res = await page.evaluate('(' + RUN + ')(' + JSON.stringify(parsed) + ')');

  console.log('\n疑难样本解码体检（ink<0.6% 视为空白）\n');
  for (const r of res) {
    const flag = !r.ok ? '✘ 失败' :
      (r.pages ? (r.pages.every(p => p.ink < 0.6) ? '✘ 空白' : '✔ 正常')
               : (r.ink < 0.6 ? '✘ 空白' : '✔ 正常'));
    let d = r.ok
      ? (r.pages
          ? `${r.numPages}页 ` + r.pages.map(p => `${p.wh} ink=${p.ink}%`).join('; ')
          : `${r.wh} ink=${r.ink}% lum=${r.lum} ${r.via}${r.bmpErr ? ' | bmpErr: ' + r.bmpErr : ''} | ${r.ctype} ${Math.round(r.bytes/1024)}KB`)
      : (r.err + ' | ' + (r.ctype || '') + ' ' + Math.round((r.bytes||0)/1024) + 'KB');
    console.log(`${flag.padEnd(7)} ${r.name.padEnd(24)} ${d}`);
  }
  await browser.close();
})();
