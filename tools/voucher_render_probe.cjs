/* 凭证渲染探针：把「打印凭证」取图/渲染链路在真浏览器里跑一遍，
 * 逐份报告 成功/失败 与 **画面是否空白**（平均亮度接近 255 就是白纸）。
 *
 * 起因：用户反馈「火车票没有票面内容，有的发票也没有」——照片、PDF 都可能
 * 出现「渲染成功但一片白」，光看有没有报错是抓不到的，必须量像素。
 *
 * 用法：
 *   NODE_PATH=/Users/rickey/.workbuddy/binaries/node/workspace/node_modules \
 *     node tools/voucher_render_probe.cjs [baseUrl] [invoiceIds，逗号分隔，可省]
 * 省略 invoiceIds 时自动取前 50 张有附件的发票。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright-core');

const BASE = process.argv[2] || process.env.WB_BASE_URL || 'http://127.0.0.1:8792';
const IDS = process.argv[3] || '';
const USER = process.env.ADMIN_USERNAME || 'admin';
const PASS = process.env.ADMIN_PASSWORD || 'Adm1n@2026';

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

/* 页面内执行：与 voucher_print.js 同一条链路，额外做空白检测 */
const PROBE = `(async (idsCsv) => {
  const tok = (window.WB && WB.auth && WB.auth.token) || localStorage.getItem('wb_token') || '';
  const H = { Authorization: 'Bearer ' + tok };
  const out = [];

  async function getJson(u) { const r = await fetch(u, { headers: H }); if (!r.ok) throw new Error(u + ' -> ' + r.status); return r.json(); }
  async function getBlob(u) {
    const r = await fetch(u, { headers: Object.assign({ Accept: '*/*' }, H) });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return { blob: await r.blob(), ctype: r.headers.get('content-type') || '', disp: r.headers.get('content-disposition') || '' };
  }

  // 亮度：缩到 40×40 取灰度均值；同时算「非白像素占比」，比纯均值更能说明有没有内容
  function statsOf(cv) {
    const s = document.createElement('canvas');
    s.width = 40; s.height = 40;
    const c = s.getContext('2d');
    c.fillStyle = '#fff'; c.fillRect(0, 0, 40, 40);
    c.drawImage(cv, 0, 0, 40, 40);
    const d = c.getImageData(0, 0, 40, 40).data;
    let sum = 0, nonWhite = 0;
    for (let i = 0; i < d.length; i += 4) {
      const g = (d[i] * 0.299 + d[i + 1] * 0.587 + d[i + 2] * 0.114);
      sum += g;
      if (g < 230) nonWhite++;
    }
    const n = d.length / 4;
    return { lum: +(sum / n).toFixed(1), ink: +(100 * nonWhite / n).toFixed(1) };
  }

  function drawToCanvas(src, w, h, maxSide) {
    const long = Math.max(w, h) || 1;
    const sc = Math.min(1, maxSide / long);
    const cv = document.createElement('canvas');
    cv.width = Math.max(1, Math.round(w * sc));
    cv.height = Math.max(1, Math.round(h * sc));
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, cv.width, cv.height);
    ctx.drawImage(src, 0, 0, cv.width, cv.height);
    return cv;
  }

  // 图片：先 createImageBitmap（现网实现），失败再走 <img> 原生解码兜底
  async function renderImage(url) {
    const { blob, ctype, disp } = await getBlob(url);
    const info = { bytes: blob.size, ctype, disp };
    let cv = null, how = '';
    try {
      const bmp = await createImageBitmap(blob);
      info.wh = bmp.width + '×' + bmp.height;
      cv = drawToCanvas(bmp, bmp.width, bmp.height, 1600);
      if (bmp.close) bmp.close();
      how = 'createImageBitmap';
    } catch (e1) {
      info.bmpErr = String(e1 && e1.message || e1);
      const bmp2 = await new Promise((res, rej) => {
        const i = new Image();
        const u = URL.createObjectURL(blob);
        i.onload = () => { URL.revokeObjectURL(u); res(i); };
        i.onerror = () => { URL.revokeObjectURL(u); rej(new Error('img 解码失败')); };
        i.src = u;
      });
      info.wh = bmp2.naturalWidth + '×' + bmp2.naturalHeight;
      cv = drawToCanvas(bmp2, bmp2.naturalWidth, bmp2.naturalHeight, 1600);
      how = 'Image()';
    }
    return Object.assign(info, { ok: true, how }, statsOf(cv));
  }

  // PDF：与现网一致用 pdf.js
  async function ensurePdfjs() {
    if (window.pdfjsLib) return window.pdfjsLib;
    await new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = 'vendor/pdf/pdf.min.js';
      s.onload = res; s.onerror = () => rej(new Error('pdf.min.js 加载失败'));
      document.head.appendChild(s);
    });
    window.pdfjsLib.GlobalWorkerOptions.workerSrc = 'vendor/pdf/pdf.worker.min.js';
    return window.pdfjsLib;
  }

  async function renderPdf(url) {
    const lib = await ensurePdfjs();
    const { blob, ctype, disp } = await getBlob(url);
    const buf = await blob.arrayBuffer();
    const doc = await lib.getDocument({ data: buf }).promise;
    const pages = [];
    for (let p = 1; p <= Math.min(doc.numPages, 5); p++) {
      const page = await doc.getPage(p);
      const base = page.getViewport({ scale: 1 });
      const scale = 1600 / Math.max(base.width, base.height);
      const vp = page.getViewport({ scale });
      const cv = document.createElement('canvas');
      cv.width = Math.round(vp.width); cv.height = Math.round(vp.height);
      const ctx = cv.getContext('2d');
      ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, cv.width, cv.height);
      await page.render({ canvasContext: ctx, viewport: vp }).promise;
      pages.push(Object.assign({ wh: cv.width + '×' + cv.height }, statsOf(cv)));
      page.cleanup();
    }
    const num = doc.numPages;
    await doc.destroy();
    return { ok: true, bytes: blob.size, ctype, disp, numPages: num, pages };
  }

  let ids = idsCsv ? idsCsv.split(',').filter(Boolean) : null;
  if (!ids) {
    const list = await getJson('/api/invoices?page=1&page_size=50');
    const items = list.items || list.data || [];
    ids = items.filter(v => (v.attachments || []).length).map(v => v.id);
    if (!ids.length) {
      // 列表可能不带 attachments，退化成整单打印清单
      const all = await getJson('/api/invoices?page=1&page_size=200');
      const it2 = all.items || all.data || [];
      const withDoc = [];
      for (const v of it2.slice(0, 40)) {
        try { const d = await getJson('/api/print/vouchers?ids=' + v.id);
              if ((d.invoices || []).some(x => (x.attachments || []).length)) withDoc.push(v.id); } catch (e) {}
      }
      ids = withDoc;
    }
  }
  if (!ids.length) return { error: '没有找到带附件的发票' };

  const data = await getJson('/api/print/vouchers?ids=' + ids.join(','));
  for (const inv of (data.invoices || [])) {
    for (const a of (inv.attachments || [])) {
      const rec = { inv: inv.id, invoice_no: inv.invoice_no, type: inv.invoice_type,
                    aid: a.id, kind: a.kind, kind_label: a.kind_label,
                    filename: a.filename, media: a.media, mime: a.mime };
      if (a.media === 'other') { rec.verdict = '非图片/PDF（走占位卡）'; out.push(rec); continue; }
      try {
        const r = a.media === 'pdf' ? await renderPdf(a.url) : await renderImage(a.url);
        Object.assign(rec, r);
        if (a.media === 'pdf') {
          const blank = r.pages.filter(p => p.ink < 0.6).length;
          rec.verdict = blank === r.pages.length ? '全部空白' : (blank ? blank + ' 页空白' : '正常');
        } else {
          rec.verdict = r.ink < 0.6 ? '画面空白' : '正常';
        }
      } catch (e) {
        rec.ok = false; rec.err = String(e && e.message || e); rec.verdict = '渲染失败';
      }
      out.push(rec);
    }
  }
  return { items: out, ctx: { ua: navigator.userAgent } };
})`;

(async () => {
  const browser = await chromium.launch({ executablePath: findChrome(), args: ['--no-sandbox'] });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.on('console', (m) => { if (m.type() === 'error') console.log('  [console]', m.text().slice(0, 160)); });
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.fill('#lg-user', USER);
  await page.fill('#lg-pass', PASS);
  await page.click('#lg-submit');
  await page.waitForSelector('#app', { state: 'visible', timeout: 15000 });
  await page.waitForTimeout(600);

  // evaluate 传字符串时不吃第二个参数，得把 ids 拼进表达式
  const res = await page.evaluate('(' + PROBE + ')(' + JSON.stringify(IDS) + ')');
  if (res.error) { console.log('✘', res.error); await browser.close(); process.exit(1); }

  console.log('\n凭证渲染体检（ink<0.6% 视为空白）\n');
  console.log('aid  类型'.padEnd(22), '文件名'.padEnd(40), '结论 / 细节');
  console.log('-'.repeat(120));
  let bad = 0;
  for (const it of res.items) {
    const flag = it.verdict === '正常' ? '✔' : '✘';
    if (it.verdict !== '正常') bad++;
    const name = String(it.filename || '').slice(0, 38);
    let detail = it.verdict;
    if (it.pages) detail += ` | ${it.numPages}页 ` + it.pages.map(p => `${p.wh} ink=${p.ink}%`).join('; ');
    else if (it.ok) detail += ` | ${it.wh} ink=${it.ink}% lum=${it.lum} via ${it.how}${it.bmpErr ? ' (bmpErr: ' + it.bmpErr + ')' : ''}`;
    else detail += ' | ' + it.err;
    console.log(`${String(it.aid).padEnd(4)} ${String(it.kind_label || it.media).padEnd(10)}`, name.padEnd(40), `${flag} ${detail}`);
  }
  console.log(`\n合计 ${res.items.length} 份，异常 ${bad} 份`);
  await browser.close();
})();
