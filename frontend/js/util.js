/* 通用工具：格式化、DOM、Toast、Modal */
window.WB = window.WB || {};

(function () {
  const U = {};

  /* ---------------- 格式化 ---------------- */
  const moneyFmt = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const intFmt = new Intl.NumberFormat('zh-CN');

  U.money = (v) => '¥' + moneyFmt.format(Number(v || 0));
  U.moneyShort = (v) => {
    const n = Number(v || 0);
    if (Math.abs(n) >= 1e8) return '¥' + (n / 1e8).toFixed(2) + '亿';
    if (Math.abs(n) >= 1e4) return '¥' + (n / 1e4).toFixed(1) + '万';
    return '¥' + moneyFmt.format(n);
  };
  U.num = (v) => intFmt.format(Number(v || 0));
  U.date = (s) => (s ? String(s).slice(0, 10) : '—');
  U.datetime = (s) => (s ? String(s).slice(0, 16).replace('T', ' ') : '—');
  U.today = () => new Date().toISOString().slice(0, 10);
  U.daysAgo = (n) => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10);
  U.monthStart = () => new Date().toISOString().slice(0, 8) + '01';
  U.year = () => new Date().getFullYear();
  U.esc = (s) =>
    String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  /* 状态 -> 徽标样式 */
  const STATUS_CLASS = {
    草稿: 'b-gray', 待审批: 'b-orange', 已通过: 'b-blue', 已付款: 'b-green', 已驳回: 'b-red',
    未查验: 'b-gray', 已查验: 'b-green', 异常: 'b-red',
    进行中: 'b-blue', 已结束: 'b-gray',
  };
  U.badge = (text) => `<span class="badge ${STATUS_CLASS[text] || 'b-gray'}">${U.esc(text || '—')}</span>`;

  /* ---------------- DOM ---------------- */
  U.qs = (sel, root) => (root || document).querySelector(sel);
  U.qsa = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  U.debounce = (fn, ms = 300) => {
    let t;
    return function (...a) {
      clearTimeout(t);
      t = setTimeout(() => fn.apply(this, a), ms);
    };
  };
  U.on = (root, evt, sel, fn) => {
    root.addEventListener(evt, (e) => {
      const t = e.target.closest(sel);
      if (t && root.contains(t)) fn(e, t);
    });
  };

  /* ---------------- Toast ---------------- */
  U.toast = (msg, type = 'info', ms = 2600) => {
    const box = document.getElementById('toast-root');
    const el = document.createElement('div');
    el.className = `toast t-${type}`;
    el.innerHTML = `<span class="t-dot"></span>${U.esc(msg)}`;
    box.appendChild(el);
    setTimeout(() => {
      el.classList.add('out');
      setTimeout(() => el.remove(), 260);
    }, ms);
  };

  /* ---------------- Modal ---------------- */
  U.modal = function ({ title, body, footer, width = 720, onMount }) {
    const root = document.getElementById('modal-root');
    const wrap = document.createElement('div');
    wrap.className = 'modal-mask';
    wrap.innerHTML = `
      <div class="modal" style="width:${width}px">
        <div class="modal-head">
          <h3>${U.esc(title)}</h3>
          <button class="modal-x" data-close>&times;</button>
        </div>
        <div class="modal-body"></div>
        <div class="modal-foot"></div>
      </div>`;
    const bodyEl = U.qs('.modal-body', wrap);
    if (typeof body === 'string') bodyEl.innerHTML = body;
    else if (body) bodyEl.appendChild(body);

    const footEl = U.qs('.modal-foot', wrap);
    const api = {
      el: wrap,
      body: bodyEl,
      close() {
        wrap.classList.add('out');
        setTimeout(() => wrap.remove(), 180);
      },
      setFooter(html) {
        footEl.innerHTML = html;
      },
    };

    if (footer === null) {
      footEl.remove();
    } else if (typeof footer === 'string') {
      footEl.innerHTML = footer;
    } else if (footer) {
      footEl.appendChild(footer);
    }

    wrap.addEventListener('click', (e) => {
      if (e.target === wrap || e.target.closest('[data-close]')) api.close();
    });
    root.appendChild(wrap);
    requestAnimationFrame(() => wrap.classList.add('in'));
    if (onMount) onMount(api);
    return api;
  };

  U.confirm = function (message, { title = '请确认', okText = '确定', danger = false } = {}) {
    return new Promise((resolve) => {
      let done = false;
      const m = U.modal({
        title,
        width: 420,
        body: `<p class="confirm-msg">${message}</p>`,
        footer: `<button class="btn" data-close>取消</button>
                 <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-ok>${U.esc(okText)}</button>`,
        onMount(api) {
          U.qs('[data-ok]', api.el).onclick = () => {
            done = true;
            api.close();
            resolve(true);
          };
          api.el.addEventListener('click', (e) => {
            if (e.target === api.el) resolve(false);
          });
        },
      });
      const origClose = m.close;
      m.close = function () {
        if (!done) resolve(false);
        origClose.call(m);
      };
    });
  };

  /* ---------------- 表单辅助 ---------------- */
  U.serialize = (form) => {
    const out = {};
    U.qsa('[name]', form).forEach((el) => {
      if (el.type === 'checkbox') out[el.name] = el.checked;
      else if (el.type === 'number') out[el.name] = el.value === '' ? null : Number(el.value);
      else out[el.name] = el.value === '' ? null : el.value;
    });
    return out;
  };

  U.options = (list, { valueKey = 'id', labelKey = 'name', selected, placeholder } = {}) =>
    (placeholder ? `<option value="">${U.esc(placeholder)}</option>` : '') +
    list
      .map(
        (o) =>
          `<option value="${U.esc(o[valueKey])}" ${String(o[valueKey]) === String(selected) ? 'selected' : ''}>${U.esc(
            o[labelKey]
          )}</option>`
      )
      .join('');

  U.download = (url) => {
    const a = document.createElement('a');
    a.href = url;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  U.emptyRow = (cols, text = '暂无数据') =>
    `<tr class="empty-row"><td colspan="${cols}"><div class="empty">${U.esc(text)}</div></td></tr>`;

  U.loading = (cols) => `<tr class="empty-row"><td colspan="${cols}"><div class="empty"><span class="spin"></span>加载中…</div></td></tr>`;

  /* ---------------- 手机版：拍照入口 ----------------
   * 桌面端由 mobile.css 隐藏（.m-camera 默认 display:none），移动端才露出来。
   * data-camera 存的是**既有 file input 的选择器**，mobile.js 把相机拍到的 File
   * 用 DataTransfer 塞回那个 input 再派发一次 change —— 上传与识别链路一行没动，
   * 所以手机上拍的照片和桌面选的文件的处理路径完全一致。
   * 选择器写成 '#v-file' 这种「当前文档唯一」的形式即可：每次只开一个弹窗。 */
  U.CAMERA_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M3 9a2.5 2.5 0 0 1 2.5-2.5h1.1a1.5 1.5 0 0 0 1.3-.75l.5-.9a1.5 1.5 0 0 1 1.3-.75h4.6a1.5 1.5 0 0 1 1.3.75l.5.9a1.5 1.5 0 0 0 1.3.75h1.1A2.5 2.5 0 0 1 21 9v8.5A2.5 2.5 0 0 1 18.5 20h-13A2.5 2.5 0 0 1 3 17.5z"/>' +
    '<circle cx="12" cy="13" r="3.4"/></svg>';

  U.cameraBtn = (target, label = '拍照') =>
    `<button type="button" class="btn btn-sm m-camera" data-camera="${U.esc(target)}">${U.CAMERA_ICON}${U.esc(label)}</button>`;

  /* 分页条 */
  U.pager = (total, page, pageSize) => {
    const pages = Math.max(1, Math.ceil(total / pageSize));
    const from = total ? (page - 1) * pageSize + 1 : 0;
    const to = Math.min(page * pageSize, total);
    return `<div class="pager">
      <span class="pager-info">共 <b>${U.num(total)}</b> 条，当前 ${from}-${to}</span>
      <div class="pager-btns">
        <button class="btn btn-sm" data-page="1" ${page <= 1 ? 'disabled' : ''}>首页</button>
        <button class="btn btn-sm" data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>上一页</button>
        <span class="pager-cur">${page} / ${pages}</span>
        <button class="btn btn-sm" data-page="${page + 1}" ${page >= pages ? 'disabled' : ''}>下一页</button>
        <button class="btn btn-sm" data-page="${pages}" ${page >= pages ? 'disabled' : ''}>末页</button>
      </div>
    </div>`;
  };

  U.colorByIndex = (i) =>
    ['#1677ff', '#00b42a', '#ff7d00', '#f53f3f', '#722ed1', '#13c2c2', '#eb2f96', '#faad14',
     '#2f54eb', '#a0d911', '#fa541c', '#08979c'][i % 12];

  /* ---------------- 批量操作 ---------------- */

  /** 收集勾选的行 id。scope 省略时取整个文档。 */
  U.pickedIds = (scope, selector = '.ck-row') =>
    U.qsa(selector, scope || document)
      .filter((c) => c.checked)
      .map((c) => Number(c.value))
      .filter((n) => !Number.isNaN(n));

  /** 表头全选：绑定一次即可，子表重绘后依然有效（事件委托） */
  U.bindCheckAll = (scope) => {
    const all = U.qs('#ck-all', scope || document);
    if (!all) return;
    all.onclick = () => {
      U.qsa('.ck-row', scope || document).forEach((c) => (c.checked = all.checked));
      all.checked = U.qsa('.ck-row', scope || document).every((c) => c.checked);
    };
  };

  /**
   * 危险操作确认：除了点「确定」，还要求把确认词原样打一遍。
   * 批量删除、清空台账这类不可恢复的动作都走它。
   * resolve(true/false)。
   */
  U.dangerConfirm = (message, keyword, { title = '危险操作确认', okText = '确认删除' } = {}) =>
    new Promise((resolve) => {
      const m = U.modal({
        title,
        width: 460,
        body: `<div class="danger-box">
            <div class="danger-msg">${message}</div>
            <div class="danger-tip">此操作不可恢复。请在下框中原样输入
              <b class="danger-key">${U.esc(keyword)}</b> 以确认：</div>
            <input class="danger-input" id="dc-input" autocomplete="off" placeholder="${U.esc(keyword)}">
          </div>`,
        footer: `<button class="btn" data-close>取消</button>
                 <button class="btn btn-danger" id="dc-ok" disabled>${U.esc(okText)}</button>`,
        onMount(apiMod) {
          const input = U.qs('#dc-input', apiMod.el);
          const ok = U.qs('#dc-ok', apiMod.el);
          input.oninput = () => {
            ok.disabled = input.value.trim() !== keyword;
          };
          input.focus();
          input.onkeydown = (e) => {
            if (e.key === 'Enter' && !ok.disabled) ok.click();
          };
          ok.onclick = () => {
            apiMod.close();
            resolve(true);
          };
          apiMod.el.addEventListener('click', (e) => {
            if (e.target.closest('[data-close]')) resolve(false);
          });
        },
      });
      void m;
    });

  /** 把批量删除接口的返回渲染成一句人话 */
  U.batchResult = (r, unit = '条') => {
    if (!r) return '';
    const parts = [`成功删除 ${r.deleted || 0} ${unit}`];
    if (r.files_removed) parts.push(`清理影像 ${r.files_removed} 个`);
    if (r.skipped_count) parts.push(`跳过 ${r.skipped_count} ${unit}`);
    if (r.denied_count) parts.push(`无权删除 ${r.denied_count} ${unit}`);
    return parts.join('，');
  };

  /** 把后端返回的「跳过原因」汇总，用于超长提示 */
  U.skipReasons = (r) =>
    ((r && r.skipped) || [])
      .slice(0, 5)
      .map((x) => `${x.code || x.name || '#' + x.id}：${x.reason}`)
      .join('；');

  WB.util = U;
})();
