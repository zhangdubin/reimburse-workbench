/* 视图：扫码核验（v2.8.0）
 *
 * 扫打印单据左下角的 Data Matrix，解析出单号后跳转到对应记录
 * （报销单 → 详情弹窗；发票 → 发票详情；清单批次 → 发票台账）。
 *
 * 取像走两条路：
 *   1) 拍照 / 选图（默认，内网 http 也能用）
 *      getUserMedia 要求安全上下文（https 或 localhost），内网 http://IP:8080
 *      下浏览器直接拒绝调用摄像头。所以主通道用
 *      <input type="file" accept="image/*" capture="environment">：手机上点一下
 *      直接唤起后置摄像头，拍完在本地解码 —— 不依赖摄像头 API，一样能扫。
 *   2) 实时扫码（仅在 https / localhost 下出现）：video + 连续解码，体验更好。
 *
 * 解码用本地 vendor/zxing.min.js（零 CDN），限定 DATA_MATRIX（顺带兼容 QR）。
 */
window.WB = window.WB || {};
WB.views = WB.views || {};

(function () {
  const U = WB.util;
  const api = WB.api;

  const HISTORY_KEY = 'wb_scan_history';
  const MAX_HISTORY = 12;

  /* ---------------- ZXing ---------------- */
  function zxingReady() {
    return typeof window.ZXing !== 'undefined' && !!window.ZXing.BrowserMultiFormatReader;
  }

  function makeHints() {
    const hints = new Map();
    hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS, [
      ZXing.BarcodeFormat.DATA_MATRIX,
      ZXing.BarcodeFormat.QR_CODE,
    ]);
    hints.set(ZXing.DecodeHintType.TRY_HARDER, true);
    return hints;
  }

  function fileToImage(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        URL.revokeObjectURL(url);
        resolve(img);
      };
      img.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error('图片读取失败'));
      };
      img.src = url;
    });
  }

  /** canvas → Image（ZXing 的 decodeFromCanvas 在 0.21.x 上识别不稳，统一走 Image） */
  function canvasToImage(canvas) {
    return new Promise((resolve, reject) => {
      const out = new Image();
      out.onload = () => resolve(out);
      out.onerror = () => reject(new Error('图像转换失败'));
      out.src = canvas.toDataURL('image/png');
    });
  }

  /**
   * 按参数渲染一张候选图。
   * filter 走 canvas 的 CSS filter（老浏览器不支持时静默忽略，不影响流程）。
   */
  function render(img, w, h, opts) {
    const o = opts || {};
    const k = o.edge ? Math.min(1, o.edge / Math.max(w, h)) : 1;
    let sx = 0, sy = 0, sw = w, sh = h;
    if (o.crop > 0 && o.crop < 1) {
      sw = Math.round(w * o.crop);
      sh = Math.round(h * o.crop);
      sx = Math.round((w - sw) / 2);
      sy = Math.round((h - sh) / 2);
    }
    const cw = Math.max(1, Math.round(sw * k));
    const ch = Math.max(1, Math.round(sh * k));
    const rot = ((o.rotate || 0) % 360 + 360) % 360;
    const c = document.createElement('canvas');
    if (rot === 90 || rot === 270) { c.width = ch; c.height = cw; } else { c.width = cw; c.height = ch; }
    const ctx = c.getContext('2d');
    if (o.filter) ctx.filter = o.filter;
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, c.width, c.height);
    if (rot) {
      ctx.translate(c.width / 2, c.height / 2);
      ctx.rotate((rot * Math.PI) / 180);
      ctx.drawImage(img, sx, sy, sw, sh, -cw / 2, -ch / 2, cw, ch);
    } else {
      ctx.drawImage(img, sx, sy, sw, sh, 0, 0, cw, ch);
    }
    return c;
  }

  /** 候选图序列：按「最可能成功且最便宜」排队 */
  const F_GRAY = 'grayscale(1) contrast(1.6)';
  const F_HARD = 'grayscale(1) contrast(2.2) brightness(1.08)';

  function candidates(img, w, h) {
    const out = [];
    const add = (o) => out.push(render(img, w, h, o));
    // 1) 尺度金字塔：小图每个 module 像素少但噪声也少，实测比原图更好解
    [1600, 1100, 780].forEach((e) => add({ edge: e }));
    // 2) 去色 + 拉对比度：手机拍单据最常见的「灰蒙蒙」就靠这一档救
    [1600, 780].forEach((e) => add({ edge: e, filter: F_GRAY }));
    add({ edge: 520, filter: F_HARD });
    // 3) 手持歪了 / 单据横着拍
    [90, 180, 270].forEach((r) => add({ edge: 1100, rotate: r }));
    // 4) 码在整页照片里占比太小：裁中间出来等于放大
    add({ edge: 1600, crop: 0.5 });
    add({ edge: 1600, crop: 0.34, filter: F_GRAY });
    return out;
  }

  /**
   * 解码一张图（v2.9.21 增强）。
   * 注意：不要用 ZXing 的 decodeFromCanvas —— 同样一张 Data Matrix，
   * decodeFromImageElement 能解出来、decodeFromCanvas 却报 NotFound（实测）。
   * 所以每个候选都转回 Image 再解。
   *
   * 解不出来时抛出 LOCAL_FAIL，由调用方把原图发服务端用 libdmtx 兜底。
   */
  const LOCAL_FAIL = 'LOCAL_FAIL';

  async function decodeImage(img) {
    if (!zxingReady()) throw new Error('扫码组件未加载，请刷新页面');
    const reader = new ZXing.BrowserMultiFormatReader(makeHints(), { delayBetweenScanAttempts: 50 });
    const w = img.naturalWidth || img.width || 0;
    const h = img.naturalHeight || img.height || 0;
    if (!w || !h) throw new Error('图片尺寸异常，请重新拍照');

    for (const canvas of candidates(img, w, h)) {
      try {
        const target = await canvasToImage(canvas);
        const r = await reader.decodeFromImageElement(target);
        if (r && r.getText()) return r.getText();
      } catch (_) {
        /* 换下一个候选继续试 */
      }
    }
    throw new Error(LOCAL_FAIL);
  }

  /* ---------------- 历史 ---------------- */
  function loadHistory() {
    try {
      const raw = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
      return Array.isArray(raw) ? raw : [];
    } catch (_) {
      return [];
    }
  }

  function saveHistory(list) {
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, MAX_HISTORY)));
    } catch (_) {
      /* 隐私模式下写不了，忽略 */
    }
  }

  function pushHistory(r) {
    const list = loadHistory().filter((x) => !(x.type === r.type && x.id === r.id));
    list.unshift({
      type: r.type, id: r.id, code: r.code,
      title: r.title, subtitle: r.subtitle, ts: Date.now(),
    });
    saveHistory(list);
  }

  /* ---------------- 解析 + 跳转 ---------------- */
  function openTarget(r) {
    if (r.type === 'reimbursement' && r.id) {
      if (WB.openReimbursement) WB.openReimbursement(r.id);
      else location.hash = '#/reimbursements';
    } else if (r.type === 'invoice' && r.id) {
      if (WB.openInvoice) WB.openInvoice(r.id);
      else location.hash = '#/invoices';
    } else {
      location.hash = '#/invoices';
    }
  }

  let lastStatus = '';

  async function handleCode(code, onStatus) {
    const say = (t, cls) => {
      lastStatus = t;
      if (onStatus) onStatus(t, cls);
    };
    if (!code) return;
    say('正在解析 ' + code + ' …');
    let r;
    try {
      r = await api.get('/api/scan/resolve', { code });
    } catch (e) {
      say('查询失败：' + (e && e.message ? e.message : e), 'bad');
      U.toast('查询失败，请稍后重试', 'error');
      return false;
    }
    if (!r || !r.found) {
      say((r && r.hint) || '没有找到对应记录', 'bad');
      U.toast((r && r.hint) || '没有找到对应记录', 'warn');
      return false;
    }
    pushHistory(r);
    say('已定位：' + r.title, 'ok');
    U.toast('已打开 ' + r.title, 'ok');
    openTarget(r);
    return true;
  }

  /* ---------------- 视图 ---------------- */
  WB.views.scan = async function (root) {
    const secure = !!window.isSecureContext;
    const hasZxing = zxingReady();

    root.innerHTML = `
      <style>
        .scan-grid { display: grid; grid-template-columns: 1.15fr 1fr; gap: var(--sp-5); }
        @media (max-width: 900px) { .scan-grid { grid-template-columns: 1fr; } }
        .scan-stage {
          position: relative; border: 1px dashed var(--border-strong); border-radius: var(--radius-sm);
          background: var(--panel-2); min-height: 210px; display: flex; align-items: center;
          justify-content: center; overflow: hidden; margin-bottom: var(--sp-4);
        }
        .scan-stage img, .scan-stage video { max-width: 100%; max-height: 300px; display: block; }
        .scan-stage video { display: none; width: 100%; }
        .scan-ph { text-align: center; color: var(--text-3); font-size: var(--fs-sm); line-height: 1.9; }
        .scan-ph b { display: block; font-size: 30px; color: var(--primary); margin-bottom: 4px; }
        .scan-actions { display: flex; flex-wrap: wrap; gap: var(--sp-3); align-items: center; }
        .scan-status { margin-top: var(--sp-4); font-size: var(--fs-sm); color: var(--text-2); min-height: 20px; }
        .scan-status.ok { color: #15803d; font-weight: 600; }
        .scan-status.bad { color: #b91c1c; }
        .scan-manual { display: flex; gap: var(--sp-3); margin-bottom: var(--sp-4); }
        .scan-manual input {
          flex: 1; min-width: 0; padding: 8px 12px; font-size: 16px; font-family: inherit;
          border: 1px solid var(--border-strong); border-radius: var(--radius-sm); color: var(--text-1);
        }
        .scan-hist { list-style: none; margin: 0; padding: 0; max-height: 320px; overflow: auto; }
        .scan-hist li {
          padding: 9px 10px; border-bottom: 1px solid var(--border); cursor: pointer;
          display: flex; gap: var(--sp-3); align-items: center;
        }
        .scan-hist li:hover { background: var(--primary-weak); }
        .scan-hist .h-code { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: var(--fs-sm); color: var(--primary); font-weight: 600; }
        .scan-hist .h-sub { font-size: var(--fs-xs); color: var(--text-3); }
        .scan-hist .h-ts { margin-left: auto; font-size: var(--fs-xs); color: var(--text-3); white-space: nowrap; }
      </style>

      <div class="scan-grid">
        <div class="card">
          <div class="card-head">
            <h3>扫描单据上的 Data Matrix</h3>
            <span class="hint">对准打印单据左下角的方码</span>
          </div>
          <div class="card-body">
            <div class="scan-stage" id="scan-stage">
              <div class="scan-ph" id="scan-ph">
                <b>▣</b>
                拍下单据左下角的方码，系统自动识别并跳到对应记录
              </div>
              <video id="scan-video" playsinline muted></video>
            </div>
            <div class="scan-actions">
              <input type="file" id="scan-file" accept="image/*" hidden>
              <!-- 桌面端入口（m-camera 拍照按钮只在 ≤820px 显示，桌面必须有这个兜底） -->
              <label class="btn btn-sm" for="scan-file">选择图片识别</label>
              ${U.cameraBtn('#scan-file', '拍照识别')}
              ${secure ? '<button type="button" class="btn btn-sm" id="scan-live">开启实时扫码</button>' : ''}
              <button type="button" class="btn btn-sm" id="scan-reset" style="display:none">重新扫描</button>
            </div>
            <div class="scan-status" id="scan-status">
              ${hasZxing
                ? secure
                  ? '点「拍照 / 选图识别」拍下码图；也可以用实时扫码连续识别。'
                  : '点「拍照 / 选图识别」拍下码图即可识别。'
                : '<span style="color:#b91c1c">扫码组件未加载，请强制刷新页面（Cmd/Ctrl+Shift+R）。</span>'}
            </div>
            ${!secure
              ? '<div class="hint" style="margin-top:8px">提示：本系统走内网 http，浏览器不允许网页直接开摄像头，所以用「拍照识别」。若部署了 https，会出现实时扫码按钮。</div>'
              : ''}
          </div>
        </div>

        <div class="card">
          <div class="card-head"><h3>手动输入单号</h3><span class="hint">扫不出时可手工核对</span></div>
          <div class="card-body">
            <div class="scan-manual">
              <input id="scan-input" type="text" placeholder="如 BX20260924001 / 发票号码" autocomplete="off">
              <button class="btn btn-sm" id="scan-go">查询</button>
            </div>
            <div class="card-head" style="padding:0 0 10px;border:0">
              <h3 style="font-size:var(--fs-sm)">最近扫描</h3>
              <button class="btn btn-xs" id="scan-clear">清空</button>
            </div>
            <ul class="scan-hist" id="scan-hist"></ul>
          </div>
        </div>
      </div>`;

    const stage = U.qs('#scan-stage', root);
    const ph = U.qs('#scan-ph', root);
    const video = U.qs('#scan-video', root);
    const fileInp = U.qs('#scan-file', root);
    const statusEl = U.qs('#scan-status', root);
    const histEl = U.qs('#scan-hist', root);
    const resetBtn = U.qs('#scan-reset', root);

    const say = (t, cls) => {
      statusEl.textContent = t;
      statusEl.className = 'scan-status' + (cls ? ' ' + cls : '');
    };

    function renderHistory() {
      const list = loadHistory();
      if (!list.length) {
        histEl.innerHTML = '<li style="cursor:default;color:var(--text-3);font-size:var(--fs-sm)">还没有扫描记录</li>';
        return;
      }
      histEl.innerHTML = list
        .map(
          (h) => `<li data-i="${U.esc(String(h.id == null ? '' : h.id))}" data-t="${U.esc(h.type || '')}">
            <div>
              <div class="h-code">${U.esc(h.code || '')}</div>
              <div class="h-sub">${U.esc(h.title || '')}${h.subtitle ? ' · ' + U.esc(h.subtitle) : ''}</div>
            </div>
            <span class="h-ts">${h.ts ? U.datetime(new Date(h.ts).toISOString()) : ''}</span>
          </li>`
        )
        .join('');
    }

    function preview(img) {
      ph.style.display = 'none';
      const old = U.qs('img', stage);
      if (old) old.remove();
      stage.appendChild(img);
      img.style.maxWidth = '100%';
      img.style.maxHeight = '300px';
      resetBtn.style.display = '';
    }

    async function scanFile(file) {
      if (!file) return;
      say('正在识别图片…');
      try {
        const img = await fileToImage(file);
        let text = null;
        try {
          text = await decodeImage(img);
        } catch (e) {
          // 本地多尺度/多预处理都没解出来 —— 把原图发给服务端，
          // 用 libdmtx（ISO 参考实现）再来一轮，对糊图/歪图提升明显
          if ((e && e.message) !== LOCAL_FAIL) throw e;
          say('本地没认出来，正在用服务端增强识别…');
          const r = await api.scanDecode(file);
          if (r && r.found && r.text) text = r.text;
          else throw new Error((r && r.hint) || '没能识别出码图，请靠近一些、让码占满取景框再试');
        }
        preview(img);
        const okGo = await handleCode(text, say);
        if (!okGo) say('识别到「' + text + '」，但系统里没有对应记录', 'bad');
      } catch (e) {
        say((e && e.message) || '识别失败', 'bad');
        U.toast((e && e.message) || '识别失败', 'warn');
      }
    }

    fileInp.addEventListener('change', () => {
      const f = fileInp.files && fileInp.files[0];
      fileInp.value = '';
      if (f) scanFile(f);
    });

    resetBtn.onclick = () => {
      const old = U.qs('img', stage);
      if (old) old.remove();
      ph.style.display = '';
      resetBtn.style.display = 'none';
      say('对准方码，重新拍照或选图。');
    };

    /* 手动输入 */
    const go = () => {
      const v = (U.qs('#scan-input', root).value || '').trim();
      if (!v) return U.toast('请输入单号', 'warn');
      handleCode(v, say);
    };
    U.qs('#scan-go', root).onclick = go;
    U.qs('#scan-input', root).addEventListener('keydown', (e) => {
      if (e.key === 'Enter') go();
    });

    /* 历史 */
    U.qs('#scan-clear', root).onclick = () => {
      saveHistory([]);
      renderHistory();
      say('已清空扫描记录。');
    };
    histEl.addEventListener('click', (e) => {
      const li = e.target.closest('li[data-t]');
      if (!li) return;
      const list = loadHistory();
      const hit = list.find((h) => String(h.id) === li.dataset.i && h.type === li.dataset.t);
      if (hit) handleCode(hit.code, say);
    });

    /* 实时扫码：只有安全上下文才可能成功 */
    let live = null;
    if (secure) {
      const liveBtn = U.qs('#scan-live', root);
      liveBtn.onclick = async () => {
        if (live) {
          live.stop();
          live = null;
          liveBtn.textContent = '开启实时扫码';
          return;
        }
        if (!zxingReady()) return U.toast('扫码组件未加载，请刷新页面', 'error');
        const reader = new ZXing.BrowserMultiFormatReader(makeHints(), { delayBetweenScanAttempts: 150 });
        try {
          ph.style.display = 'none';
          video.style.display = 'block';
          resetBtn.style.display = '';
          live = await reader.decodeFromConstraints(
            { video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } } },
            video,
            (result) => {
              if (!result) return;
              const text = result.getText();
              if (live) { live.stop(); live = null; liveBtn.textContent = '开启实时扫码'; }
              video.style.display = 'none';
              handleCode(text, say);
            }
          );
          liveBtn.textContent = '停止实时扫码';
          say('正在实时识别，把方码放进画面中间…');
        } catch (e) {
          video.style.display = 'none';
          ph.style.display = '';
          say('无法打开摄像头：' + ((e && e.message) || e), 'bad');
        }
      };
    }

    renderHistory();
    say(lastStatus || (hasZxing ? '对准方码，点下方按钮拍照识别。' : '扫码组件未加载。'));

    return {
      title: '扫码核验',
      sub: '扫单据左下角的 Data Matrix，直接跳到对应记录',
    };
  };
})();
