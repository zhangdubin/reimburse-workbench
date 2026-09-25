/* 手机版外壳（断点 820px）
 *
 * 只做四件事，业务逻辑一行不碰：
 *   1. 侧栏 → 抽屉导航（汉堡按钮 + 遮罩 + 底部标签栏）
 *   2. 表格 → 卡片（从 <thead> 读列名回填到 <td data-label>，样式在 mobile.css）
 *   3. 工具条筛选折叠（把筛选控件收进「筛选」面板，动作按钮留在外面）
 *   4. 拍照上传桥接（相机拍到的文件塞回既有 file input，复用原有识别链路）
 *
 * 为什么表格卡片化放在这里而不是各视图里：视图有 15 张表格、结构各异，
 * 逐处改既容易漏也容易在下次改版时脱节。这里统一后置处理，新增视图自动受益；
 * 结构对不上（列数不匹配）时直接跳过，宁可保持成可横向滚动的表格，也不猜。
 */
window.WB = window.WB || {};

(function () {
  const U = WB.util;

  const MQ = window.matchMedia('(max-width: 820px)');

  /* 底部标签栏的取用顺序：把「报销单」放第一位——这是唯一每天都要用的入口，
     看板是「看一下就走」，放第二位。 */
  /* 手机底部标签栏：只放前 4 个（TAB_LIMIT），其余进「全部」抽屉。
   * 扫码核验放在第 4 位 —— 手机扫纸质单据是高频动作，值得占一个固定位。 */
  const TAB_ORDER = [
    'reimbursements', 'dashboard', 'invoices', 'scan',
    'inbox', 'expenses', 'alerts', 'users', 'settings', 'ai_settings', 'audit',
  ];
  const TAB_LIMIT = 4;

  const ICON_MORE =
    '<path d="M4 6h16M4 12h16M4 18h16"/>';

  const M = {
    on() {
      return MQ.matches;
    },
  };

  /* ---------------- 导航同步（由 app.js 的 renderNav 调用） ---------------- */
  let nav = { items: [], current: '' };

  M.syncNav = function (items, current) {
    nav = { items: items || [], current: current || '' };
    drawTabs();
  };

  function tabHtml(item, active) {
    return `<div class="mtab ${active ? 'active' : ''}" data-key="${item.key}" title="${U.esc(item.name)}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
        stroke-linecap="round" stroke-linejoin="round">${item.icon}</svg>
      <span>${U.esc(item.short || item.name)}</span>
      ${item.badge ? `<span class="nav-badge">${item.badge > 99 ? '99+' : item.badge}</span>` : ''}
    </div>`;
  }

  function drawTabs() {
    const bar = document.getElementById('mtabbar');
    if (!bar) return;
    if (!nav.items.length) {
      bar.innerHTML = '';
      return;
    }
    const picked = TAB_ORDER.map((k) => nav.items.find((i) => i.key === k))
      .filter(Boolean)
      .slice(0, TAB_LIMIT);
    const needMore = picked.length < nav.items.length;
    bar.innerHTML =
      picked.map((i) => tabHtml(i, i.key === nav.current)).join('') +
      (needMore
        ? `<div class="mtab mtab-more" data-more="1" title="全部功能">
             <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
               stroke-linecap="round" stroke-linejoin="round">${ICON_MORE}</svg>
             <span>全部</span>
           </div>`
        : '');
  }

  /* ---------------- 抽屉 ---------------- */
  function openNav() {
    document.body.classList.add('m-nav-open');
  }
  function closeNav() {
    document.body.classList.remove('m-nav-open');
  }

  function bindShell() {
    const btn = document.getElementById('m-nav-btn');
    if (btn) btn.onclick = () => (document.body.classList.contains('m-nav-open') ? closeNav() : openNav());

    const close = document.getElementById('m-nav-close');
    if (close) close.onclick = closeNav;

    const back = document.getElementById('m-backdrop');
    if (back) back.onclick = closeNav;

    // 点中任一条导航就收起抽屉，否则选完还挡着半屏（事件委托，导航重绘也不失效）
    const navEl = document.getElementById('nav');
    if (navEl) {
      navEl.addEventListener('click', (e) => {
        if (e.target.closest('.nav-item')) closeNav();
      });
    }

    const bar = document.getElementById('mtabbar');
    if (bar) {
      bar.addEventListener('click', (e) => {
        const more = e.target.closest('[data-more]');
        if (more) return openNav();
        const tab = e.target.closest('.mtab[data-key]');
        if (tab) {
          closeNav();
          WB.go(tab.dataset.key);
        }
      });
    }

    window.addEventListener('hashchange', closeNav);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeNav();
    });

    // 断点切换（转屏、桌面窗口拖窄）：进出手机布局都要把工具条还原干净，
    // 否则从手机拖回桌面会留下折叠面板这种半残状态。
    const onChange = () => {
      if (MQ.matches) M.afterView();
      else {
        restoreToolbar();
        closeNav();
      }
    };
    if (MQ.addEventListener) MQ.addEventListener('change', onChange);
    else if (MQ.addListener) MQ.addListener(onChange);
  }

  /* ---------------- 表格 → 卡片 ---------------- */
  function enhanceTable(t) {
    const ths = Array.from(t.querySelectorAll('thead th'));
    if (!ths.length) return;
    const labels = ths.map((th) => (th.textContent || '').trim());

    t.querySelectorAll('tbody tr').forEach((tr) => {
      const tds = Array.from(tr.children);
      // 空态占位行（colspan 一条 td）：不套卡片，保持原来的居中提示
      if (tds.length === 1 && tds[0].hasAttribute('colspan')) {
        tr.classList.add('m-plain');
        return;
      }
      // 列数对不上说明这张表结构特殊，原样留成可横向滚动的表格，不臆测
      if (tds.length !== labels.length) return;

      let hasCk = false;
      let hasAct = false;
      tds.forEach((td, i) => {
        if (td.classList.contains('ck-col') || td.querySelector('input.ck-row')) {
          td.classList.add('m-ck');
          td.setAttribute('data-label', '');
          hasCk = true;
          return;
        }
        td.setAttribute('data-label', labels[i] || '');
        if (td.querySelector('.row-actions')) {
          td.classList.add('m-act');
          hasAct = true;
        }
      });
      if (hasCk) tr.classList.add('m-has-ck');
      if (!hasAct) {
        // 没有操作列的表（如审计日志），最后一格补一条虚线，视觉上收个尾
        const last = tds[tds.length - 1];
        if (last) last.classList.add('m-last');
      }
      // 首列当卡片标题：去掉标签、字号加大
      const head = tds.find(
        (td) => !td.classList.contains('m-ck') && !td.classList.contains('m-act') && !td.querySelector('.empty')
      );
      if (head) head.classList.add('m-head');
    });
    t.dataset.mCards = '1';
  }

  /** 把范围内的表格全部卡片化。重复调用是安全的（已处理的表带标记直接跳过）。 */
  M.enhance = function (root) {
    if (!M.on()) return;
    const scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll('table.tbl, .items-editor table').forEach((t) => {
      if (t.dataset.mCards === '1') return;
      enhanceTable(t);
    });
  };

  /* 视图与弹窗的内容大多是异步 fetch 后整体 innerHTML 替换进来的，
     渲染完成的时机不统一。这里挂一个观察器兜底：只要冒出新的表格就补一次
     卡片化。只监听 childList（不看属性），而卡片化本身只改属性，
     所以不会自触发成死循环。 */
  function observeTables() {
    let queued = false;
    const mo = new MutationObserver(() => {
      if (queued || !M.on()) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        M.enhance(document);
      });
    });
    mo.observe(document.body, { childList: true, subtree: true });
  }

  /* ---------------- 工具条：筛选折叠 ---------------- */
  const CARET =
    '<svg class="m-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" ' +
    'stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>';

  function collapseToolbar() {
    const tb = document.getElementById('toolbar');
    if (!tb || tb.style.display === 'none') return;
    // 工具条内容是整体替换的，折叠面板也随之消失；有面板就说明这个视图已经折叠过
    if (tb.querySelector(':scope > .m-filters')) return;

    const kids = Array.from(tb.children);
    const sp = tb.querySelector(':scope > .spacer');
    const spIdx = sp ? kids.indexOf(sp) : kids.length;
    const before = kids.slice(0, spIdx);
    const search = before.find((el) => el.classList.contains('search-input'));
    // 搜索框留在外面（最高频）；主操作按钮（.btn-primary）也留在外面，
    // 它们通常同时是这一页唯一想让人一眼看到的东西
    const movable = before.filter(
      (el) => el !== search && el.tagName !== 'SPAN' && !el.classList.contains('btn-primary')
    );
    if (movable.length < 2) return; // 只有一两个筛选条件，折叠反而多一次点击

    const box = document.createElement('div');
    box.className = 'm-filters';
    box.hidden = true;
    // 把筛选控件搬进面板。appendChild 是「移动」不是「复制」，
    // 控件本身的 id、事件绑定都原样保留，还原时按原顺序放回去即可。
    movable.forEach((el) => box.appendChild(el));

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-sm m-filter-toggle';
    btn.innerHTML = `筛选<span class="m-cnt">${movable.length}</span>${CARET}`;
    btn.onclick = () => {
      box.hidden = !box.hidden;
      btn.classList.toggle('open', !box.hidden);
    };

    if (search) {
      search.insertAdjacentElement('afterend', btn);
      btn.insertAdjacentElement('afterend', box);
    } else {
      tb.insertBefore(box, tb.firstChild);
      tb.insertBefore(btn, box);
    }
  }

  /* 还原：只做「拆掉包装」，节点本身一个没动，所以事件绑定不会丢。
     从手机拖回桌面时必须还原，否则筛选条件被藏在停用的面板里。 */
  function restoreToolbar() {
    document.querySelectorAll('#toolbar > .m-filters').forEach((box) => {
      const parent = box.parentNode;
      while (box.firstChild) parent.insertBefore(box.firstChild, box);
      box.remove();
    });
    document.querySelectorAll('#toolbar > .m-filter-toggle').forEach((b) => b.remove());
  }

  /* ---------------- 拍照上传桥接 ----------------
   * 相机拍到的照片没有「文件路径」，只能现场取到 File 对象；而识别链路
   * 全都挂在原有 <input type="file"> 的 change 上。用 DataTransfer 把照片
   * 塞回那个 input 再派发一次 change，既有链路原封不动就能跑起来——
   * 不为手机版另写一套上传/识别代码，就不会出现两套行为不一致。
   */
  function bindCamera() {
    document.addEventListener('click', (e) => {
      const b = e.target.closest('[data-camera]');
      if (!b) return;
      const target = document.querySelector(b.dataset.camera);
      if (!target) return;
      e.preventDefault();

      const inp = document.createElement('input');
      inp.type = 'file';
      inp.accept = 'image/*';
      inp.setAttribute('capture', 'environment'); // 直接唤起后置摄像头
      inp.style.display = 'none';
      document.body.appendChild(inp);

      inp.addEventListener('change', () => {
        const f = inp.files && inp.files[0];
        inp.remove();
        if (!f) return; // 用户拍照后取消了
        try {
          const dt = new DataTransfer();
          dt.items.add(f);
          target.files = dt.files;
          target.dispatchEvent(new Event('change', { bubbles: true }));
        } catch (err) {
          // 极端老浏览器没有 DataTransfer：退回让用户用系统选择器（一样能调到相机）
          U.toast('当前浏览器不支持直接拍照上传，请用「选择文件」里的拍照', 'warn');
        }
      });
      inp.click();
    });
  }

  /* ---------------- 生命周期 ---------------- */
  M.init = function () {
    if (M._inited) return;
    M._inited = true;
    bindShell();
    bindCamera();
    observeTables();
    if (M.on()) collapseToolbar();
  };

  /** 每次视图渲染完成后调用 */
  M.afterView = function () {
    drawTabs();
    if (!M.on()) {
      restoreToolbar();
      return;
    }
    collapseToolbar();
    M.enhance(document);
  };

  M.closeNav = closeNav;
  M.openNav = openNav;

  WB.mobile = M;
})();
