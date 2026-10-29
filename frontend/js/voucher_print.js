/* =====================================================================
 * 凭证打印（v2.9.29）—— 发票影像 / 行程单 / 消费水单的 A4 拼版打印
 *
 * 贴票场景：把一批凭证按页排好打出来，剪下来贴在报销单背面。
 * 难点与取舍：
 *  1) 影像接口要带 Bearer token，而打印窗口是 Blob 文档——里面的 <img>
 *     既带不上 header、也解析不了相对路径（同 print.js 的 DM 码踩过的坑）。
 *     所以**在主页面**把图片/PDF 一律转成 dataURL 再写进打印 HTML，
 *     打印窗口里只有 <img src="data:...">，零外部请求、零鉴权问题。
 *  2) PDF（数电票）不能直接 <img>，用本地 pdf.js（禁 CDN）渲染成位图，
 *     与图片凭证走同一条拼版链路。pdf.js 懒加载，不打印就不下载那 1.3MB。
 *  3) 拼版默认「智能自适应」：横长票（出租车票/火车票/数电票）独占一行，
 *     方形票（A4 行程单、拍照件）两列并排——顺序保持与发票列表一致，
 *     财务按明细顺序贴票不会串行；实在排不下的空格留白可手写备注。
 * ===================================================================== */
(function () {
  'use strict';

  const U = WB.util;
  const V = '2.9.37';                 // 与 index.html 的 ?v= 一致，改版要同步
  const PDF_MAX_PAGES = 5;            // 单份 PDF 最多渲染页数（防超大文件把内存吃干）
  const WIDE_RATIO = 1.35;            // 宽高比 ≥ 此值按「横长票」处理，独占一行

  // 清晰度档位：maxSide = 转图后最长边像素；quality = JPEG 质量
  const QUALITY = {
    std: { label: '标准', maxSide: 1600, quality: 0.82 },
    high: { label: '高清', maxSide: 2400, quality: 0.9 },
  };

  // 拼版档位：cols × rows = 每页格数；auto 由票据长宽比决定占几格，
  // manual 不预设版面，转图后由用户在排版编辑器里逐张决定宽度与顺序。
  const LAYOUTS = [
    { id: 'auto', label: '智能自适应', cols: 2, rows: 2 },
    { id: '1', label: '1 张/页', cols: 1, rows: 1 },
    { id: '2', label: '2 张/页', cols: 1, rows: 2 },
    { id: '4', label: '4 张/页', cols: 2, rows: 2 },
    { id: '6', label: '6 张/页', cols: 2, rows: 3 },
    { id: 'manual', label: '手动排版', cols: 2, rows: 2 },
  ];

  const state = {
    layout: 'auto',
    quality: 'std',
    cutLine: true,
    pageNo: true,
  };

  // 凭证类型标签由后端 kind_label 提供，前端只在快捷选择里区分「发票影像 / 随票凭证」
  const PRESET_KEY = 'wb.vp.preset';

  /* ------------------------------------------------------------------ *
   * pdf.js 懒加载
   * ------------------------------------------------------------------ */
  let pdfjsTask = null;
  function loadPdfjs() {
    if (window.pdfjsLib) return Promise.resolve(window.pdfjsLib);
    if (pdfjsTask) return pdfjsTask;
    pdfjsTask = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = `vendor/pdf/pdf.min.js?v=${V}`;
      s.onload = () => {
        const lib = window.pdfjsLib;
        if (!lib) return reject(new Error('pdf.js 已下载但未挂载'));
        // worker 也走绝对地址，页面在子路径下打开时不会被解析到别处
        lib.GlobalWorkerOptions.workerSrc = `${new URL('vendor/pdf/pdf.worker.min.js', document.baseURI).href}?v=${V}`;
        resolve(lib);
      };
      s.onerror = () => {
        pdfjsTask = null;
        reject(new Error('PDF 渲染组件加载失败，请检查网络或刷新页面重试'));
      };
      document.head.appendChild(s);
    });
    return pdfjsTask;
  }

  /* ------------------------------------------------------------------ *
   * 资源 → 位图 dataURL
   * ------------------------------------------------------------------ */
  function authFetch(url) {
    return fetch(url, {
      headers: Object.assign(
        { Accept: '*/*' },
        WB.auth && WB.auth.token ? { Authorization: 'Bearer ' + WB.auth.token } : {}
      ),
    }).then((r) => {
      if (!r.ok) throw new Error(`读取凭证失败 (${r.status})`);
      return r.blob();
    });
  }

  /** 带登录态取文本；404 返回 null（OFD 逐页探测的终止信号），其余状态抛错 */
  function authFetchText(url) {
    return fetch(url, {
      headers: Object.assign(
        { Accept: 'image/svg+xml, */*' },
        WB.auth && WB.auth.token ? { Authorization: 'Bearer ' + WB.auth.token } : {}
      ),
    }).then((r) => {
      if (r.status === 404) return null;
      if (!r.ok) throw new Error(`读取凭证失败 (${r.status})`);
      return r.text();
    });
  }

  const OFD_MAX_PAGES = 20;

  /** OFD 原件 → 逐页 SVG → 位图瓦片。页数由 404 探测，上限 20 页 */
  async function ofdTiles(base, q) {
    const out = [];
    for (let p = 1; p <= OFD_MAX_PAGES; p++) {
      const sep = base.includes('?') ? '&' : '?';
      let text = null;
      try {
        text = await authFetchText(`${base}${sep}page=${p}`);
      } catch (e) {
        if (!out.length) throw e;
        break; // 后续页失败但已有内容：用手头的页继续打
      }
      if (text === null) break;
      const blob = new Blob([text], { type: 'image/svg+xml' });
      const tiles = await imageTiles(blob, q);
      tiles.forEach((t) => {
        if (!t.nodoc) out.push(t);
      });
    }
    if (!out.length) throw new Error('OFD 渲染失败：没有可显示的页面');
    return out;
  }

  /** 白底 + 等比缩放 + JPEG，避免 PNG 透明底打印成黑块、也压掉数据体积 */
  function toDataUrl(source, w, h, maxSide, quality) {
    const long = Math.max(w, h) || 1;
    const scale = Math.min(1, maxSide / long);
    const cw = Math.max(1, Math.round(w * scale));
    const ch = Math.max(1, Math.round(h * scale));
    const cv = document.createElement('canvas');
    cv.width = cw;
    cv.height = ch;
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, cw, ch);
    ctx.drawImage(source, 0, 0, cw, ch);
    return { dataUrl: cv.toDataURL('image/jpeg', quality), ratio: cw / ch, cv };
  }

  /** 判断是不是「解码出了个黑块」——16 位 PNG 经 createImageBitmap 画进 8 位
   *  canvas 会整张变黑（实测 ink=100%），这种必须换 <img> 再解一次。
   *  只判全黑不判全白：空白扫描件可能是用户的真实票据，替人做主更糟。 */
  function looksBroken(cv) {
    const s = document.createElement('canvas');
    s.width = 24;
    s.height = 24;
    const c = s.getContext('2d');
    c.fillStyle = '#fff';
    c.fillRect(0, 0, 24, 24);
    c.drawImage(cv, 0, 0, 24, 24);
    const d = c.getImageData(0, 0, 24, 24).data;
    let dark = 0;
    let sum = 0;
    for (let i = 0; i < d.length; i += 4) {
      const g = d[i] * 0.299 + d[i + 1] * 0.587 + d[i + 2] * 0.114;
      sum += g;
      if (g < 40) dark += 1;
    }
    const n = d.length / 4;
    return dark / n > 0.9 && sum / n < 30;
  }

  /** <img> 解码兜底：走浏览器原生解码器，兼容 createImageBitmap 不吃的格式 */
  function loadImageEl(blob) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(blob);
      const img = new Image();
      img.onload = () => {
        URL.revokeObjectURL(url);
        resolve(img);
      };
      img.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error('图片解码失败（文件可能已损坏或是网页另存的假图片）'));
      };
      img.src = url;
    });
  }

  async function imageTiles(blob, q) {
    let broken = null;
    try {
      const bmp = await createImageBitmap(blob);
      const r = toDataUrl(bmp, bmp.width, bmp.height, q.maxSide, q.quality);
      if (bmp.close) bmp.close();
      if (!looksBroken(r.cv)) return [{ dataUrl: r.dataUrl, ratio: r.ratio }];
      broken = r;
    } catch (_) { /* 落到 <img> 兜底 */ }
    const img = await loadImageEl(blob);
    const w = img.naturalWidth;
    const h = img.naturalHeight;
    if (!w || !h) throw new Error('图片尺寸为 0，无法打印');
    const r2 = toDataUrl(img, w, h, q.maxSide, q.quality);
    if (broken && looksBroken(r2.cv)) {
      throw new Error('图像解码后整张全黑（多为 16 位/特殊色彩模式），请用原件打印');
    }
    return [{ dataUrl: r2.dataUrl, ratio: r2.ratio }];
  }

  async function pdfTiles(blob, q) {
    const lib = await loadPdfjs();
    const buf = await blob.arrayBuffer();
    // cMapUrl 必须带：数电票/铁路客票的值字段用 CID 编码字体，没有 CMap
    // pdf.js 解不出字形——模板框线在、票面值全空白（2.9.35 修复）
    const cmapUrl = new URL('vendor/pdf/cmaps/', document.baseURI).href;
    // 注意：pdf.js 的参数名是大写 C 的 cMapUrl，变量名是小写 cmapUrl，
    // 这里必须写全 `cMapUrl: cmapUrl`——简写成 `{ cMapUrl }` 会去找同名变量，
    // 直接 ReferenceError，整个预览空白（2.9.35 翻车点）
    const docOpts = { data: buf, cMapUrl: cmapUrl, cMapPacked: true };
    let doc;
    try {
      doc = await lib.getDocument(docOpts).promise;
    } catch (e) {
      const msg = String((e && e.message) || e);
      // 数电票 PDF 常带「所有者密码」（只限制改、不限制看），pdf.js 会直接抛
      // PasswordException；先用空密码再试一次，仍不行就把原话抛给用户
      if ((e && e.name === 'PasswordException') || /password/i.test(msg)) {
        try {
          doc = await lib.getDocument(Object.assign({}, docOpts, { password: '' })).promise;
        } catch (e2) {
          throw new Error('PDF 已加密，无法渲染：' + String((e2 && e2.message) || e2));
        }
      } else {
        throw new Error('PDF 无法解析：' + msg);
      }
    }
    const out = [];
    const n = Math.min(doc.numPages, PDF_MAX_PAGES);
    for (let p = 1; p <= n; p++) {
      try {
        const page = await doc.getPage(p);
        const base = page.getViewport({ scale: 1 });
        const scale = q.maxSide / Math.max(base.width, base.height);
        const vp = page.getViewport({ scale });
        const cv = document.createElement('canvas');
        cv.width = Math.round(vp.width);
        cv.height = Math.round(vp.height);
        const ctx = cv.getContext('2d');
        ctx.fillStyle = '#fff';
        ctx.fillRect(0, 0, cv.width, cv.height);
        await page.render({ canvasContext: ctx, viewport: vp }).promise;
        out.push({ dataUrl: cv.toDataURL('image/jpeg', q.quality), ratio: cv.width / cv.height });
        page.cleanup();
      } catch (e) {
        // 某一页画不出来不该拖垮整份凭证，单独标出来让用户知道缺第几页
        out.push({ nodoc: true, ratio: 1.41, title: 'PDF 渲染失败', note: `第 ${p} 页无法渲染：${(e && e.message) || e}` });
      }
    }
    if (!doc.numPages) throw new Error('PDF 没有页面');
    await doc.destroy();
    return out;
  }

  /* ------------------------------------------------------------------ *
   * 拼版
   * ------------------------------------------------------------------ */
  function layoutOf(id) {
    return LAYOUTS.find((l) => l.id === id) || LAYOUTS[0];
  }

  /** 按序装箱：横长票占 2 格、方形票占 1 格，跨行放不下就留空 */
  function paginate(tiles, lay) {
    const per = lay.cols * lay.rows;
    const pages = [];
    let cur = [];
    let used = 0;

    const flush = () => {
      if (cur.length) pages.push(cur);
      cur = [];
      used = 0;
    };

    tiles.forEach((t) => {
      const span = lay.id === 'auto' ? (t.ratio >= WIDE_RATIO ? 2 : 1) : 1;
      const want = Math.min(span, lay.cols); // 单列版面里横票也只能占 1 格
      if (want === 2) {
        // 必须落在行的起点，否则把本行剩下的格子留空
        if (used % lay.cols !== 0) {
          cur.push({ blank: true });
          used += 1;
        }
        if (used + 2 > per) flush();
        cur.push(Object.assign({}, t, { span: 2 }));
        used += 2;
      } else {
        if (used + 1 > per) flush();
        cur.push(Object.assign({}, t, { span: 1 }));
        used += 1;
      }
      if (used >= per) flush();
    });
    flush();

    // 一页只有一张时让它铺满整页，别在半张纸上缩成一小块
    pages.forEach((p) => {
      if (p.length === 1 && !p[0].blank) p[0].fill = true;
    });
    return pages;
  }

  /** 手动排版：完全按用户给的顺序与宽度装箱，不再替用户判断横竖。
   *  span: 1 = 半栏、2 = 整行、'page' = 独占一页 */
  function paginateManual(plan, cols) {
    const per = cols * 2; // 手动模式固定两列两行，宽度由每张凭证自己决定
    const pages = [];
    let cur = [];
    let used = 0;
    const flush = () => {
      if (cur.length) pages.push(cur);
      cur = [];
      used = 0;
    };

    plan.forEach((it) => {
      if (it.span === 'page') {
        flush();
        pages.push([Object.assign({}, it, { fill: true })]);
        return;
      }
      if (it.span === 2) {
        if (used % cols !== 0) {
          cur.push({ blank: true });
          used += 1;
        }
        if (used + 2 > per) flush();
        cur.push(Object.assign({}, it, { span: 2 }));
        used += 2;
      } else {
        if (used + 1 > per) flush();
        cur.push(Object.assign({}, it, { span: 1 }));
        used += 1;
      }
      if (used >= per) flush();
    });
    flush();
    return pages;
  }

  /* ------------------------------------------------------------------ *
   * 打印 HTML
   * ------------------------------------------------------------------ */
  const CSS = `
    @page { size: A4 portrait; margin: 0; }
    *, *::before, *::after { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; background: #eee;
      font-family: "PingFang SC","Microsoft YaHei","Helvetica Neue",sans-serif; }
    .vp-page {
      width: 210mm; height: 297mm; margin: 0 auto 6mm; padding: 7mm;
      background: #fff; display: flex; flex-direction: column;
    }
    .vp-grid { flex: 1; display: grid; gap: 3mm; min-height: 0; }
    .vp-cell {
      border: 1px dashed #9aa3ad; background: #fff;
      display: flex; align-items: center; justify-content: center;
      overflow: hidden; padding: 1.5mm; min-width: 0; min-height: 0;
      position: relative;
    }
    /* 绝对定位撑满：Chrome 打印引擎对 grid 里 height:100% 的图片有高度塌陷
       的老毛病——屏幕预览好好的，出纸就是空白格。绝对定位不受影响。 */
    .vp-cell img {
      position: absolute; inset: 0; margin: auto;
      width: 100%; height: 100%; object-fit: contain; display: block;
    }
    .vp-cell.blank { border-style: dotted; border-color: #d5d9de; }
    .vp-cell.nodoc { border-style: solid; border-color: #ddd; flex-direction: column; gap: 3mm; }
    .vp-cell .nd-t { font-size: 13px; font-weight: 700; color: #444; }
    .vp-cell .nd-s { font-size: 11px; color: #888; text-align: center; line-height: 1.6; }
    .vp-foot {
      flex: none; height: 5mm; line-height: 5mm; font-size: 10px; color: #8a8a8a;
      display: flex; justify-content: space-between; padding: 0 1mm;
    }
    .no-print { text-align: center; padding: 6px; border-bottom: 1px solid #ddd; background: #f8f8f8; }
    .no-print button { font-size: 13px; padding: 5px 20px; cursor: pointer;
      border: 1px solid #aaa; border-radius: 4px; background: #fff; }
    @media print {
      html, body { background: #fff; }
      .vp-page { margin: 0; page-break-after: always; break-after: page; }
      .vp-page:last-child { page-break-after: auto; break-after: auto; }
      .no-print { display: none !important; }
    }
  `;

  function cellHtml(t, lay, cut) {
    let style = '';
    if (t.fill) style = `grid-column: span ${lay.cols}; grid-row: span ${lay.rows};`;
    else if (t.span === 2) style = 'grid-column: span 2;';
    // 关掉裁剪线时不要留白边：虚线改成透明，格子位置与间距都保持不变
    if (!cut) style += 'border-color: transparent;';

    if (t.blank) return `<div class="vp-cell blank" style="${style}"></div>`;
    if (t.nodoc) {
      return `<div class="vp-cell nodoc" style="${style}">
        <div class="nd-t">${U.esc(t.title || t.label || '无法打印的凭证')}</div>
        <div class="nd-s">${U.esc(t.note || '').replace(/\n/g, '<br>')}</div>
      </div>`;
    }
    return `<div class="vp-cell" style="${style}"><img src="${t.dataUrl}" alt=""></div>`;
  }

  function buildHtml(opts) {
    const { tiles, meta, o, plan } = opts;
    const lay = layoutOf(o.layout);
    // 手动排版走用户给的顺序与宽度；其余档位仍按图像比例智能装箱
    const pages = plan && plan.length ? paginateManual(plan, lay.cols) : paginate(tiles, lay);
    const total = pages.length;
    const gridStyle = `grid-template-columns: repeat(${lay.cols}, 1fr); grid-template-rows: repeat(${lay.rows}, 1fr);`;
    const right = meta && meta.right ? U.esc(meta.right) : '';

    const body = pages
      .map((p, i) => `
        <section class="vp-page">
          <div class="vp-grid" style="${gridStyle}">
            ${p.map((t) => cellHtml(t, lay, o.cutLine)).join('')}
          </div>
          ${o.pageNo
            ? `<div class="vp-foot"><span>${right}</span><span>第 ${i + 1} / ${total} 页</span></div>`
            : ''}
        </section>`)
      .join('');

    return `<!DOCTYPE html><html lang="zh-CN"><head>
<meta charset="utf-8"><title>${U.esc((meta && meta.title) || '凭证打印')}</title>
<style>${CSS}</style>
</head><body>
<div class="no-print">
  <button onclick="window.print()">打 印</button>
  <span style="margin-left:12px;font-size:12px;color:#888">共 ${total} 页，建议先预览再出纸</span>
</div>
${body}
<script>
window.onload = () => {
  // dataURL 图不存在网络等待，但仍要等解码完成；留 300ms 让布局稳定
  const imgs = Array.from(document.images).filter(i => !i.complete);
  const go = () => setTimeout(() => { try { window.print(); } catch (e) {} }, 300);
  if (!imgs.length) return go();
  const tid = setTimeout(go, 3000);
  const check = () => { if (imgs.every(i => i.complete)) { clearTimeout(tid); go(); } };
  imgs.forEach(i => { i.addEventListener('load', check, { once: true });
                      i.addEventListener('error', check, { once: true }); });
};
<\/script>
</body></html>`;
  }

  /* ------------------------------------------------------------------ *
   * 开窗送纸（与 print.js 的 deliver 同款：Blob 优先，拦截则降级 iframe）
   * ------------------------------------------------------------------ */
  function deliver(html) {
    try {
      const url = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
      const w = window.open(url, '_blank');
      if (w && !w.closed) {
        setTimeout(() => URL.revokeObjectURL(url), 60000);
        return true;
      }
    } catch (_) { /* 降级 */ }
    let f = document.getElementById('_wb_vpf');
    if (f) f.remove();
    f = document.createElement('iframe');
    f.id = '_wb_vpf';
    Object.assign(f.style, {
      position: 'fixed', right: '0', bottom: '0', width: '0', height: '0',
      border: '0', visibility: 'hidden',
    });
    document.body.appendChild(f);
    const doc = f.contentWindow.document;
    doc.open();
    doc.write(html);
    doc.close();
    f.addEventListener('load', () => {
      try {
        f.contentWindow.focus();
        f.contentWindow.print();
      } catch (_) {}
    }, { once: true });
    return true;
  }

  /* ------------------------------------------------------------------ *
   * 凭证选择 + 打印设置弹窗
   * ------------------------------------------------------------------ */
  function segButtons(list, cur, attr) {
    return `<div class="vp-seg">${list
      .map(
        (x) =>
          `<button type="button" class="vp-seg-b${x.id === cur ? ' on' : ''}" data-${attr}="${x.id}">${U.esc(x.label)}</button>`
      )
      .join('')}</div>`;
  }

  // 快捷选择：全选/清空最常用，按类型的两个按钮覆盖「只要发票影像」这类典型场景
  const PRESETS = [
    { id: 'all', label: '全选' },
    { id: 'none', label: '清空' },
    { id: 'invoice', label: '仅发票影像' },
    { id: 'attach', label: '仅随票凭证' },
  ];

  function fmtSize(n) {
    if (!n && n !== 0) return '';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  function pickHtml(data) {
    const groups = (data.invoices || [])
      .filter((inv) => (inv.attachments || []).length)
      .map((inv) => {
        const metas = [
          inv.invoice_no ? `票号 ${inv.invoice_no}` : '',
          inv.amount ? `¥${Number(inv.amount).toFixed(2)}` : '',
          inv.invoice_date || '',
          inv.seller_name || '',
        ].filter(Boolean).join(' · ');
        const cards = (inv.attachments || [])
          .map(
            (a) => `
            <label class="vp-doc" data-doc="${a.id}" data-kind="${U.esc(a.kind || 'invoice')}">
              <input type="checkbox" data-doc="${a.id}">
              <span class="vp-thumb" data-thumb-box="${a.id}">
                ${a.media === 'image'
                  ? `<img data-thumb="${a.id}" alt="">`
                  : `<i class="vp-ext">${a.media === 'pdf' ? 'PDF' : (a.filename || '').split('.').pop().slice(0, 4).toUpperCase() || '文件'}</i>`}
              </span>
              <span class="vp-doc-m">
                <span class="vp-doc-t">
                  <b>${U.esc(a.kind_label || '凭证')}</b>
                  <em class="vp-tag">${a.media === 'pdf' ? 'PDF' : a.media === 'image' ? '图片' : '原件'}</em>
                </span>
                <span class="vp-doc-f" title="${U.esc(a.filename || '')}">${U.esc(a.filename || '')}${a.size ? ' · ' + fmtSize(a.size) : ''}</span>
              </span>
            </label>`
          )
          .join('');
        return `
          <div class="vp-grp" data-grp="${inv.id}">
            <div class="vp-grp-h">
              <label class="vp-ck"><input type="checkbox" data-grp-all="${inv.id}"><span>${U.esc(inv.invoice_no || ('发票 #' + inv.id))}</span></label>
              <span class="vp-grp-m">${U.esc(metas)}</span>
            </div>
            <div class="vp-grp-b">${cards}</div>
          </div>`;
      })
      .join('');
    return groups || '';
  }

  function settingsHtml(data) {
    const s = data.stats || {};
    const warn = s.no_doc_invoices
      ? `<div class="vp-note warn">有 <b>${s.no_doc_invoices}</b> 张发票还没上传影像，无法打印。</div>`
      : '';
    return `
      <div class="vp-sum">
        <div><b>${s.invoice_count || 0}</b><span>发票</span></div>
        <div><b>${s.doc_count || 0}</b><span>可选凭证</span></div>
        <div><b>${s.pdf_count || 0}</b><span>PDF</span></div>
        <div><b>${s.image_count || 0}</b><span>图片</span></div>
      </div>
      ${warn}
      <div class="vp-sec">
        <div class="vp-sec-h">
          <span><i class="vp-step">1</i>选择要打印的凭证</span>
          <span class="vp-sel" id="vp-sel">已选 0 / ${s.doc_count || 0}</span>
        </div>
        <div class="vp-quick">
          <span class="vp-quick-l">快捷选择</span>
          ${PRESETS.map((p) => `<button type="button" class="vp-quick-b" data-preset="${p.id}">${U.esc(p.label)}</button>`).join('')}
        </div>
        <div class="vp-pick" id="vp-pick">${pickHtml(data)}</div>
      </div>
      <div class="vp-sec">
        <div class="vp-sec-h"><span><i class="vp-step">2</i>打印设置</span></div>
        <div class="vp-row">
          <div class="vp-lbl">拼版</div>
          <div class="vp-ctl">${segButtons(LAYOUTS, state.layout, 'lay')}</div>
        </div>
        <div class="vp-row vp-row-tip">
          <div class="vp-lbl"></div>
          <div class="vp-ctl"><span class="muted" id="vp-lay-tip" style="font-size:12px">${
            state.layout === 'manual'
              ? '转图后进入排版面板，逐张决定宽度与顺序'
              : '按票据长宽比自动决定占半栏还是整行'
          }</span></div>
        </div>
        <div class="vp-row">
          <div class="vp-lbl">清晰度</div>
          <div class="vp-ctl">${segButtons(
            Object.keys(QUALITY).map((k) => ({ id: k, label: QUALITY[k].label })),
            state.quality,
            'q'
          )}</div>
        </div>
        <div class="vp-row">
          <div class="vp-lbl">裁剪虚线</div>
          <div class="vp-ctl"><label class="switch"><input type="checkbox" id="vp-cut" ${state.cutLine ? 'checked' : ''}><span class="slider"></span></label>
            <span class="muted" style="font-size:12px">沿虚线剪下即可贴票</span></div>
        </div>
        <div class="vp-row">
          <div class="vp-lbl">页码</div>
          <div class="vp-ctl"><label class="switch"><input type="checkbox" id="vp-pn" ${state.pageNo ? 'checked' : ''}><span class="slider"></span></label>
            <span class="muted" style="font-size:12px">页脚标注单号与第几页/共几页</span></div>
        </div>
      </div>
      <div class="vp-prog" id="vp-prog" style="display:none">
        <div class="vp-bar"><i id="vp-bar-i"></i></div>
        <div class="vp-prog-t" id="vp-prog-t">准备中…</div>
      </div>`;
  }

  /** 图片凭证的缩略图：书脊式的小图，让用户一眼认出是哪一张票 */
  async function loadThumbs(root, data) {
    const queue = [];
    (data.invoices || []).forEach((inv) =>
      (inv.attachments || []).forEach((a) => {
        if (a.media === 'image') queue.push(a);
      })
    );
    if (!queue.length) return;

    const run = async () => {
      while (queue.length) {
        if (!root.isConnected) return; // 弹窗已关闭，别再占带宽
        const a = queue.shift();
        try {
          const blob = await authFetch(a.url);
          const bmp = await createImageBitmap(blob);
          const long = Math.max(bmp.width, bmp.height) || 1;
          const sc = Math.min(1, 200 / long);
          const cv = document.createElement('canvas');
          cv.width = Math.max(1, Math.round(bmp.width * sc));
          cv.height = Math.max(1, Math.round(bmp.height * sc));
          const cx = cv.getContext('2d');
          cx.fillStyle = '#fff';
          cx.fillRect(0, 0, cv.width, cv.height);
          cx.drawImage(bmp, 0, 0, cv.width, cv.height);
          if (bmp.close) bmp.close();
          const img = U.qs(`[data-thumb="${a.id}"]`, root);
          if (img && root.isConnected) {
            img.src = cv.toDataURL('image/jpeg', 0.7);
            img.classList.add('on');
          }
        } catch (_) {
          const box = U.qs(`[data-thumb-box="${a.id}"]`, root);
          if (box) box.classList.add('failed');
        }
      }
    };
    for (let i = 0; i < 3; i++) run();
  }

  /* ------------------------------------------------------------------ *
   * 主入口
   * ------------------------------------------------------------------ */
  async function open(ctx) {
    const params = {};
    if (ctx && ctx.reimbursementId) params.reimbursement_id = ctx.reimbursementId;
    else if (ctx && ctx.invoiceIds && ctx.invoiceIds.length) params.ids = ctx.invoiceIds.join(',');
    else {
      U.toast('请先选择要打印的发票', 'warn');
      return;
    }

    let data;
    try {
      const res = await fetch('/api/print/vouchers?' + new URLSearchParams(params), {
        headers: { Authorization: 'Bearer ' + WB.auth.token },
      });
      if (!res.ok) {
        let msg = `读取失败 (${res.status})`;
        try {
          const j = await res.json();
          msg = (j && j.detail) || msg;
        } catch (_) {}
        throw new Error(msg);
      }
      data = await res.json();
    } catch (e) {
      U.toast(e.message || '读取凭证清单失败', 'error');
      return;
    }

    if (!data.stats || !data.stats.doc_count) {
      U.toast('这批发票还没有上传任何凭证影像', 'warn');
      return;
    }

    U.modal({
      title: '打印凭证',
      width: 720,
      body: settingsHtml(data),
      footer: `<button class="btn" data-close>取消</button>
               <button class="btn btn-primary" id="vp-go" disabled>开始打印</button>`,
      onMount(apiMod) {
        const el = apiMod.el;
        const bodyEl = apiMod.body;
        const total = U.qsa('input[data-doc]', el).length;
        let selIds = [];        // 勾选态在「设置 ↔ 排版」两次渲染之间保持
        let tilesCache = [];    // 手动排版复用已转好的图，不重复转
        let plan = [];          // [{ i, span }]，i 是 tilesCache 的下标
        let failsCache = [];    // 没能打出票面的文件名，收尾时统一提示

        /* ---------------- 第一屏：选凭证 + 打印设置 ---------------- */
        function paintSettings() {
          bodyEl.innerHTML = settingsHtml(data);
          // 恢复上一次的勾选（从排版面板返回时不能让用户重选一遍）
          U.qsa('input[data-doc]', bodyEl).forEach((c) => {
            c.checked = selIds.indexOf(Number(c.dataset.doc)) >= 0;
          });
          apiMod.setFooter(`<button class="btn" data-close>取消</button>
            <button class="btn btn-primary" id="vp-go" disabled>开始打印</button>`);
          const cut = U.qs('#vp-cut', bodyEl);
          const pn = U.qs('#vp-pn', bodyEl);
          if (cut) cut.onchange = () => { state.cutLine = cut.checked; };
          if (pn) pn.onchange = () => { state.pageNo = pn.checked; };
          const goBtn = U.qs('#vp-go', el); // 按钮在 modal footer，不在 body 内
          if (goBtn) goBtn.onclick = startFlow;
          refreshCount();
        }

        function refreshCount() {
          const n = selIds.length;
          const selTxt = U.qs('#vp-sel', bodyEl);
          const goBtn = U.qs('#vp-go', el); // 同上：footer 里的按钮要从 el 找
          if (selTxt) {
            selTxt.textContent = `已选 ${n} / ${total}`;
            selTxt.classList.toggle('on', n > 0);
          }
          if (goBtn) {
            goBtn.disabled = n === 0;
            goBtn.textContent = n ? `开始打印（${n} 份）` : '开始打印';
          }
          U.qsa('.vp-doc', bodyEl).forEach((d) => {
            const c = U.qs('input[data-doc]', d);
            d.classList.toggle('on', !!(c && c.checked));
          });
          syncGroups();
        }

        /** 读 DOM 勾选回 selIds —— 界面看到的和实际打印的必须是同一份状态 */
        function collectSel() {
          selIds = U.qsa('input[data-doc]', bodyEl)
            .filter((c) => c.checked)
            .map((c) => Number(c.dataset.doc));
        }

        function syncGroups() {
          // 发票分组框：全选打勾、部分选中打横线
          U.qsa('.vp-grp', bodyEl).forEach((g) => {
            const subs = U.qsa('input[data-doc]', g);
            if (!subs.length) return;
            const hits = subs.filter((c) => c.checked).length;
            const head = U.qs('input[data-grp-all]', g);
            if (!head) return;
            head.checked = hits > 0 && hits === subs.length;
            head.indeterminate = hits > 0 && hits < subs.length;
            g.classList.toggle('picked', hits > 0);
          });
        }

        function applyPreset(p) {
          U.qsa('input[data-doc]', bodyEl).forEach((c) => {
            const k = c.closest('.vp-doc').dataset.kind;
            if (p === 'all') c.checked = true;
            else if (p === 'none') c.checked = false;
            else if (p === 'invoice') c.checked = k === 'invoice';
            else c.checked = k !== 'invoice'; // 仅随票凭证
          });
          collectSel();
          try { localStorage.setItem(PRESET_KEY, p); } catch (_) {}
          refreshCount();
        }

        /* ---------------- 事件：全部委托到弹窗根节点，重画 DOM 不用重绑 ---------------- */
        U.on(el, 'click', '.vp-quick-b[data-preset]', (e, t) => applyPreset(t.dataset.preset));
        U.on(el, 'change', 'input[data-doc]', () => {
          collectSel();
          refreshCount();
        });
        U.on(el, 'change', 'input[data-grp-all]', (e, head) => {
          const g = head.closest('.vp-grp');
          U.qsa('input[data-doc]', g).forEach((c) => { c.checked = head.checked; });
          collectSel();
          refreshCount();
        });

        U.on(el, 'click', '.vp-seg-b[data-lay]', (e, t) => {
          state.layout = t.dataset.lay;
          U.qsa('.vp-seg-b[data-lay]', el).forEach((b) => b.classList.toggle('on', b === t));
          const tip = U.qs('#vp-lay-tip', bodyEl);
          if (tip) tip.textContent = state.layout === 'manual'
            ? '转图后进入排版面板，逐张决定宽度与顺序'
            : '按票据长宽比自动决定占半栏还是整行';
        });
        U.on(el, 'click', '.vp-seg-b[data-q]', (e, t) => {
          state.quality = t.dataset.q;
          U.qsa('.vp-seg-b[data-q]', el).forEach((b) => b.classList.toggle('on', b === t));
        });

        /* 排版面板的委托 */
        U.on(el, 'click', '.vp-pi-w button', (e, t) => {
          const i = Number(t.closest('.vp-pi').dataset.i);
          setSpan(i, t.dataset.w);
        });
        U.on(el, 'click', '.vp-pi-o [data-mv]', (e, t) => {
          moveTile(Number(t.closest('.vp-pi').dataset.i), Number(t.dataset.mv));
        });
        U.on(el, 'click', '.vp-pi-o [data-del]', (e, t) => {
          dropTile(Number(t.closest('.vp-pi').dataset.i));
        });
        U.on(el, 'click', '#vp-pa', () => autoArrange());
        U.on(el, 'click', '[data-all]', (e, t) => {
          const w = Number(t.dataset.all);
          plan.forEach((p) => { p.span = w; });
          redrawPlan();
        });

        // 默认不替用户做主：一张都不预选；上次用过的快捷方式会被记住，常打印全
        // 部的人点一次「全选」后，下次打开就是这个习惯。
        let last = 'none';
        try { last = localStorage.getItem(PRESET_KEY) || 'none'; } catch (_) {}
        paintSettings();
        applyPreset(last);
        loadThumbs(el, data);

        /* ---------------- 转图 → 出纸 / 进排版面板 ---------------- */
        async function startFlow() {
          const ids = selIds.slice();
          if (!ids.length) {
            U.toast('请先勾选要打印的凭证', 'warn');
            return;
          }
          const q = QUALITY[state.quality] || QUALITY.std;
          // 严格按「发票顺序 → 凭证顺序」取，跟列表所见一致
          const picked = [];
          (data.invoices || []).forEach((inv) => {
            (inv.attachments || []).forEach((a) => {
              if (ids.indexOf(a.id) >= 0) picked.push({ inv, doc: a });
            });
          });

          const prog = U.qs('#vp-prog', bodyEl);
          const bar = U.qs('#vp-bar-i', bodyEl);
          const txt = U.qs('#vp-prog-t', bodyEl);
          const goBtn = U.qs('#vp-go', el);
          if (prog) prog.style.display = 'block';
          if (goBtn) goBtn.disabled = true;

          const tiles = [];
          failsCache = [];
          for (let i = 0; i < picked.length; i++) {
            const { doc } = picked[i];
            const label = doc.kind_label || '凭证';
            const ext = String(doc.ext || (doc.filename || '').split('.').pop() || '').toUpperCase();
            const sub = `${doc.filename || ''}${ext ? ' · ' + ext : ''}`;
            if (txt) txt.textContent = `正在处理第 ${i + 1} / ${picked.length} 份凭证…`;
            if (bar) bar.style.width = Math.round((i / picked.length) * 100) + '%';
            // 让进度条有机会重绘（同步的 canvas 渲染会卡住主线程）
            await new Promise((r) => setTimeout(r, 0));
            try {
              if (doc.media === 'image' || doc.media === 'pdf') {
                const blob = await authFetch(doc.url);
                const got = doc.media === 'pdf'
                  ? await pdfTiles(blob, q)
                  : await imageTiles(blob, q);
                got.forEach((g, gi) => {
                  tiles.push(Object.assign({}, g, {
                    label,
                    sub,
                    multi: got.length > 1 ? `第 ${gi + 1}/${got.length} 页` : '',
                  }));
                  if (g.nodoc) failsCache.push(sub);
                });
              } else if (doc.media === 'ofd') {
                // OFD 电子发票原件：服务端已转好 SVG，逐页拉取转位图
                const base = doc.svg_url || `/api/attachments/${doc.id}/ofd.svg`;
                const got = await ofdTiles(base, q);
                got.forEach((g, gi) => {
                  tiles.push(Object.assign({}, g, {
                    label,
                    sub,
                    multi: got.length > 1 ? `第 ${gi + 1}/${got.length} 页` : '',
                  }));
                  if (g.nodoc) failsCache.push(sub);
                });
              } else {
                // ofd/xml/xlsx 原件：打不出图，占位卡写清文件名与格式
                const fmt = ext || '原件';
                tiles.push({
                  nodoc: true,
                  ratio: 1.41,
                  label,
                  sub,
                  note: `${doc.filename || ''}\n${fmt} 格式无法直接生成票面图像，请用原件打印后粘贴`,
                });
                failsCache.push(sub);
              }
            } catch (e) {
              tiles.push({
                nodoc: true,
                ratio: 1.41,
                label,
                sub,
                note: `${doc.filename || ''}\n${e.message || '读取失败'}`,
              });
              failsCache.push(sub);
            }
          }

          if (bar) bar.style.width = '100%';
          if (txt) txt.textContent = '排版中…';
          await new Promise((r) => setTimeout(r, 0));

          tilesCache = tiles;
          if (state.layout === 'manual') {
            openPlan();
            return;
          }
          finish(tiles, null);
        }

        /** 生成打印文档并送纸。planArr 有值时按用户排好的顺序与宽度装箱 */
        function finish(tiles, planArr) {
          const reb = data.reimbursement;
          const count = planArr ? planArr.length : tiles.length;
          const html = buildHtml({
            tiles,
            plan: planArr,
            o: Object.assign({}, state),
            meta: {
              title: reb ? `凭证 ${reb.code}` : '凭证打印',
              right: reb
                ? `${reb.code} · ${reb.applicant || ''} · 凭证 ${count} 份`
                : `凭证 ${count} 份`,
            },
          });
          apiMod.close();
          deliver(html);
          if (failsCache.length) {
            const head = failsCache.slice(0, 2).join('、');
            U.toast(
              `有 ${failsCache.length} 份没能打出票面：${head}${failsCache.length > 2 ? ' 等' : ''}`,
              'warn',
              6000
            );
          }
        }

        /* ---------------- 手动排版面板 ---------------- */
        function openPlan() {
          // 初值仍按图像比例给个合理建议，用户再改
          plan = tilesCache.map((t, i) => ({ i, span: t.ratio >= WIDE_RATIO ? 2 : 1 }));
          bodyEl.innerHTML = planShell();
          apiMod.setFooter(`<button class="btn" id="vp-back">← 返回设置</button>
            <button class="btn btn-primary" id="vp-do">打印</button>`);
          const back = U.qs('#vp-back', el); // footer 里的按钮，必须从 modal 根节点找
          if (back) back.onclick = () => paintSettings();
          const doBtn = U.qs('#vp-do', el);
          if (doBtn) {
            doBtn.onclick = () => {
              if (!plan.length) {
                U.toast('排版里已经没有凭证了，返回设置重新勾选', 'warn');
                return;
              }
              finish(tilesCache, plan.map((p) => Object.assign({}, tilesCache[p.i], { span: p.span })));
            };
          }
          redrawPlan();
        }

        function planShell() {
          const bad = tilesCache.filter((t) => t.nodoc).length;
          return `
            <div class="vp-plan">
              <div class="vp-plan-note">
                转图已完成，下面是实际会打印的内容。顺序就是贴票顺序，
                「半栏 / 整行 / 整页」决定每张占多宽，✕ 表示这次不打这张。
                ${bad ? `<br><b class="vp-plan-bad">其中 ${bad} 份无法打出票面</b>（列表标红）：多为 OFD/XML 电子发票原件或文件损坏，需用原件打印。` : ''}
              </div>
              <div class="vp-plan-bar">
                <span>共 <b>${tilesCache.length}</b> 份 · 预计 <b id="vp-pages">0</b> 页</span>
                <span class="spacer"></span>
                <button type="button" class="btn btn-sm" id="vp-pa">按图像自动排</button>
                <button type="button" class="btn btn-sm" data-all="1">全部半栏</button>
                <button type="button" class="btn btn-sm" data-all="2">全部整行</button>
              </div>
              <div class="vp-plan-list" id="vp-plan-list"></div>
              <div class="vp-plan-h2">分页预览（A4）</div>
              <div class="vp-prev" id="vp-prev"></div>
            </div>`;
        }

        function rowHtml(t, p, n) {
          const th = t.dataUrl
            ? `<img src="${t.dataUrl}" alt="">`
            : '<i class="vp-ext">原件</i>';
          const wBtn = ['1', '2', 'page']
            .map((w) => {
              const lab = w === '1' ? '半栏' : w === '2' ? '整行' : '整页';
              return `<button type="button" data-w="${w}" class="${String(p.span) === w ? 'on' : ''}">${lab}</button>`;
            })
            .join('');
          return `<div class="vp-pi${t.nodoc ? ' bad' : ''}" data-i="${p.i}">
            <span class="vp-pi-n">${n + 1}</span>
            <span class="vp-pi-th">${th}</span>
            <span class="vp-pi-m">
              <b>${U.esc(t.label || '凭证')}</b>
              <em>${U.esc(t.sub || '')}${t.multi ? ' · ' + U.esc(t.multi) : ''}${t.nodoc ? ' · 无法打出票面' : ''}</em>
            </span>
            <span class="vp-pi-w">${wBtn}</span>
            <span class="vp-pi-o">
              <button type="button" data-mv="-1" ${n === 0 ? 'disabled' : ''} title="上移">↑</button>
              <button type="button" data-mv="1" ${n === plan.length - 1 ? 'disabled' : ''} title="下移">↓</button>
              <button type="button" data-del="1" title="这次不打印这张">✕</button>
            </span>
          </div>`;
        }

        function miniHtml(page, idx) {
          const cells = page
            .map((t) => {
              const cls = t.blank ? ' blank' : t.fill ? ' fill' : t.span === 2 ? ' row' : '';
              const bg = t.dataUrl ? ` style="background-image:url(${t.dataUrl})"` : '';
              return `<i class="vp-mc${cls}"${bg}></i>`;
            })
            .join('');
          return `<div class="vp-mini"><div class="vp-mini-g">${cells}</div>
            <span class="vp-mini-n">第 ${idx + 1} 页</span></div>`;
        }

        function redrawPlan() {
          const list = U.qs('#vp-plan-list', bodyEl);
          if (list) list.innerHTML = plan.map((p, n) => rowHtml(tilesCache[p.i], p, n)).join('');
          const arr = plan.map((p) => Object.assign({}, tilesCache[p.i], { span: p.span }));
          const pages = paginateManual(arr, 2);
          const pgEl = U.qs('#vp-pages', bodyEl);
          if (pgEl) pgEl.textContent = pages.length;
          const prev = U.qs('#vp-prev', bodyEl);
          if (prev) prev.innerHTML = pages.map(miniHtml).join('');
          const doBtn = U.qs('#vp-do', el);
          if (doBtn) doBtn.textContent = `打印（${plan.length} 份 / ${pages.length} 页）`;
        }

        function setSpan(i, w) {
          const p = plan.find((x) => x.i === i);
          if (!p) return;
          p.span = w === 'page' ? 'page' : Number(w);
          redrawPlan();
        }

        function moveTile(i, dir) {
          const idx = plan.findIndex((x) => x.i === i);
          const to = idx + dir;
          if (idx < 0 || to < 0 || to >= plan.length) return;
          const it = plan.splice(idx, 1)[0];
          plan.splice(to, 0, it);
          redrawPlan();
        }

        function dropTile(i) {
          plan = plan.filter((x) => x.i !== i);
          redrawPlan();
        }

        function autoArrange() {
          plan.forEach((p) => {
            p.span = tilesCache[p.i].ratio >= WIDE_RATIO ? 2 : 1;
          });
          redrawPlan();
        }
      },
    });
  }

  WB.voucherPrint = { open, buildHtml, paginate, paginateManual, LAYOUTS, QUALITY, ofdTiles };
})();
