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
  const V = '2.9.29';                 // 与 index.html 的 ?v= 一致，改版要同步
  const PDF_MAX_PAGES = 5;            // 单份 PDF 最多渲染页数（防超大文件把内存吃干）
  const WIDE_RATIO = 1.35;            // 宽高比 ≥ 此值按「横长票」处理，独占一行

  // 清晰度档位：maxSide = 转图后最长边像素；quality = JPEG 质量
  const QUALITY = {
    std: { label: '标准', maxSide: 1600, quality: 0.82 },
    high: { label: '高清', maxSide: 2400, quality: 0.9 },
  };

  // 拼版档位：cols × rows = 每页格数；auto 由票据长宽比决定占几格
  const LAYOUTS = [
    { id: 'auto', label: '智能自适应', cols: 2, rows: 2 },
    { id: '1', label: '1 张/页', cols: 1, rows: 1 },
    { id: '2', label: '2 张/页', cols: 1, rows: 2 },
    { id: '4', label: '4 张/页', cols: 2, rows: 2 },
    { id: '6', label: '6 张/页', cols: 2, rows: 3 },
  ];

  const KINDS = [
    { id: 'invoice', label: '发票影像' },
    { id: 'itinerary', label: '行程单' },
    { id: 'folio', label: '消费水单' },
    { id: 'other', label: '其他材料' },
  ];

  const state = {
    layout: 'auto',
    quality: 'std',
    kinds: KINDS.map((k) => k.id),
    cutLine: true,
    pageNo: true,
  };

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
        lib.GlobalWorkerOptions.workerSrc = `vendor/pdf/pdf.worker.min.js?v=${V}`;
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
    return { dataUrl: cv.toDataURL('image/jpeg', quality), ratio: cw / ch };
  }

  async function imageTiles(blob, q) {
    const bmp = await createImageBitmap(blob);
    try {
      const r = toDataUrl(bmp, bmp.width, bmp.height, q.maxSide, q.quality);
      return [{ dataUrl: r.dataUrl, ratio: r.ratio }];
    } finally {
      if (bmp.close) bmp.close();
    }
  }

  async function pdfTiles(blob, q) {
    const lib = await loadPdfjs();
    const buf = await blob.arrayBuffer();
    const doc = await lib.getDocument({ data: buf }).promise;
    const out = [];
    const n = Math.min(doc.numPages, PDF_MAX_PAGES);
    for (let p = 1; p <= n; p++) {
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
    }
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
    }
    .vp-cell img { width: 100%; height: 100%; object-fit: contain; display: block; }
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
        <div class="nd-t">${U.esc(t.title || '无法打印的凭证')}</div>
        <div class="nd-s">${U.esc(t.note || '').replace(/\n/g, '<br>')}</div>
      </div>`;
    }
    return `<div class="vp-cell" style="${style}"><img src="${t.dataUrl}" alt=""></div>`;
  }

  function buildHtml(opts) {
    const { tiles, meta, o } = opts;
    const lay = layoutOf(o.layout);
    const pages = paginate(tiles, lay);
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
   * 设置弹窗
   * ------------------------------------------------------------------ */
  function segButtons(list, cur, attr) {
    return `<div class="vp-seg">${list
      .map(
        (x) =>
          `<button type="button" class="vp-seg-b${x.id === cur ? ' on' : ''}" data-${attr}="${x.id}">${U.esc(x.label)}</button>`
      )
      .join('')}</div>`;
  }

  function settingsHtml(data) {
    const s = data.stats || {};
    const kinds = KINDS.map(
      (k) =>
        `<label class="vp-ck"><input type="checkbox" data-kind="${k.id}" ${state.kinds.indexOf(k.id) >= 0 ? 'checked' : ''}><span>${U.esc(k.label)}</span></label>`
    ).join('');
    const warn = s.no_doc_invoices
      ? `<div class="vp-note warn">有 <b>${s.no_doc_invoices}</b> 张发票还没上传影像，打印时不会出现在纸张上。</div>`
      : '';
    return `
      <div class="vp-sum">
        <div><b>${s.invoice_count || 0}</b><span>发票</span></div>
        <div><b>${s.doc_count || 0}</b><span>凭证</span></div>
        <div><b>${s.pdf_count || 0}</b><span>PDF</span></div>
        <div><b>${s.image_count || 0}</b><span>图片</span></div>
      </div>
      ${warn}
      <div class="vp-row">
        <div class="vp-lbl">拼版</div>
        <div class="vp-ctl">${segButtons(LAYOUTS, state.layout, 'lay')}</div>
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
        <div class="vp-lbl">凭证类型</div>
        <div class="vp-ctl vp-kinds">${kinds}</div>
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
      <div class="vp-prog" id="vp-prog" style="display:none">
        <div class="vp-bar"><i id="vp-bar-i"></i></div>
        <div class="vp-prog-t" id="vp-prog-t">准备中…</div>
      </div>`;
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
      width: 560,
      body: settingsHtml(data),
      footer: `<button class="btn" data-close>取消</button>
               <button class="btn btn-primary" id="vp-go">开始打印</button>`,
      onMount(apiMod) {
        const el = apiMod.el;
        U.on(el, 'click', '.vp-seg-b[data-lay]', (e, t) => {
          state.layout = t.dataset.lay;
          U.qsa('.vp-seg-b[data-lay]', el).forEach((b) => b.classList.toggle('on', b === t));
        });
        U.on(el, 'click', '.vp-seg-b[data-q]', (e, t) => {
          state.quality = t.dataset.q;
          U.qsa('.vp-seg-b[data-q]', el).forEach((b) => b.classList.toggle('on', b === t));
        });
        U.qsa('input[data-kind]', el).forEach((c) => {
          c.onchange = () => {
            const id = c.dataset.kind;
            const i = state.kinds.indexOf(id);
            if (c.checked && i < 0) state.kinds.push(id);
            if (!c.checked && i >= 0) state.kinds.splice(i, 1);
          };
        });
        const cut = U.qs('#vp-cut', el);
        const pn = U.qs('#vp-pn', el);
        cut.onchange = () => { state.cutLine = cut.checked; };
        pn.onchange = () => { state.pageNo = pn.checked; };

        U.qs('#vp-go', el).onclick = async () => {
          const kinds = state.kinds.slice();
          const q = QUALITY[state.quality] || QUALITY.std;
          const picked = [];
          (data.invoices || []).forEach((inv) => {
            (inv.attachments || []).forEach((a) => {
              if (kinds.indexOf(a.kind || 'invoice') >= 0) picked.push({ inv, doc: a });
            });
          });
          if (!picked.length) {
            U.toast('当前凭证类型筛选下没有可打印的凭证', 'warn');
            return;
          }

          const prog = U.qs('#vp-prog', el);
          const bar = U.qs('#vp-bar-i', el);
          const txt = U.qs('#vp-prog-t', el);
          prog.style.display = 'block';
          U.qs('#vp-go', el).disabled = true;

          const tiles = [];
          let failed = 0;
          for (let i = 0; i < picked.length; i++) {
            const { doc } = picked[i];
            txt.textContent = `正在处理第 ${i + 1} / ${picked.length} 份凭证…`;
            bar.style.width = Math.round((i / picked.length) * 100) + '%';
            // 让进度条有机会重绘（同步的 canvas 渲染会卡住主线程）
            await new Promise((r) => setTimeout(r, 0));
            try {
              if (doc.media === 'image' || doc.media === 'pdf') {
                const blob = await authFetch(doc.url);
                const got = doc.media === 'pdf'
                  ? await pdfTiles(blob, q)
                  : await imageTiles(blob, q);
                got.forEach((g, gi) => {
                  tiles.push({
                    dataUrl: g.dataUrl,
                    ratio: g.ratio,
                    multi: got.length > 1 ? `（第 ${gi + 1}/${got.length} 页）` : '',
                  });
                });
              } else {
                // ofd/xml/xlsx 原件：打不出图，给一张占位卡写清文件名，提醒用原件补打
                tiles.push({
                  nodoc: true,
                  ratio: 1.41,
                  title: doc.kind_label || '凭证',
                  note: `${doc.filename || ''}\n该格式无法直接打印，请用原件打印后粘贴`,
                });
              }
            } catch (e) {
              failed += 1;
              tiles.push({
                nodoc: true,
                ratio: 1.41,
                title: doc.kind_label || '凭证',
                note: `${doc.filename || ''}\n${e.message || '读取失败'}`,
              });
            }
          }

          bar.style.width = '100%';
          txt.textContent = '排版中…';
          await new Promise((r) => setTimeout(r, 0));

          const reb = data.reimbursement;
          const html = buildHtml({
            tiles,
            o: Object.assign({}, state),
            meta: {
              title: reb ? `凭证 ${reb.code}` : '凭证打印',
              right: reb
                ? `${reb.code} · ${reb.applicant || ''}`
                : `发票 ${data.stats.invoice_count} 张 · 凭证 ${data.stats.doc_count} 份`,
            },
          });
          apiMod.close();
          deliver(html);
          if (failed) U.toast(`有 ${failed} 份凭证读取失败，已以空白卡占位`, 'warn');
        };
      },
    });
  }

  WB.voucherPrint = { open, buildHtml, paginate, LAYOUTS, QUALITY };
})();
