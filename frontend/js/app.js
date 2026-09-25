/* 应用外壳、导航与路由 */
window.WB = window.WB || {};
WB.views = WB.views || {};

(function () {
  const U = WB.util;
  const api = WB.api;

  const ICONS = {
    dashboard: '<path d="M3 13h8V3H3zM13 21h8V11h-8zM3 21h8v-5H3zM13 8h8V3h-8z"/>',
    doc: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h5"/>',
    invoice: '<path d="M4 3h16v18l-3-2-2 2-3-2-3 2-2-2-3 2z"/><path d="M8 8h8M8 12h8M8 16h5"/>',
    wallet: '<path d="M3 7a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M16 12h3M3 9h16"/>',
    mail: '<rect x="2.5" y="4.5" width="19" height="15" rx="2.2"/><path d="M3 7l9 6 9-6"/><path d="M15.5 12.5l5 5M8.5 12.5l-5 5"/>',
    alert: '<path d="M12 3l9 16H3z"/><path d="M12 9v5M12 17h.01"/>',
    users: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/>',
    spark: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M18 15.5l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9z"/>',
    update: '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
    scan: '<path d="M3 8V5.5A2.5 2.5 0 0 1 5.5 3H8M16 3h2.5A2.5 2.5 0 0 1 21 5.5V8M21 16v2.5a2.5 2.5 0 0 1-2.5 2.5H16M8 21H5.5A2.5 2.5 0 0 1 3 18.5V16"/><path d="M3.5 12h17"/>',
    backup: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  };

  /* perm 为空表示所有已登录用户可见。
     short 是底部标签栏用的短名——标签一格只有 1/5 屏宽，全称会被截断成
     「报销单管…」，不如直接给一个短语。 */
  const VIEWS = [
    { key: 'dashboard', name: '统计看板', short: '看板', icon: 'dashboard', group: '总览' },
    { key: 'reimbursements', name: '报销单管理', short: '报销', icon: 'doc', group: '业务办理' },
    { key: 'invoices', name: '发票管理', short: '发票', icon: 'invoice', group: '业务办理', badge: 'invoiceBadge' },
    { key: 'scan', name: '扫码核验', short: '扫码', icon: 'scan', group: '业务办理' },
    { key: 'inbox', name: '发票收件箱', short: '收件箱', icon: 'mail', group: '业务办理', perm: 'inbox.view' },
    { key: 'expenses', name: '费用管理', short: '费用', icon: 'wallet', group: '基础与预算' },
    { key: 'alerts', name: '异常与预警', short: '预警', icon: 'alert', group: '风险控制', badge: 'alertBadge', perm: 'stats.analyst' },
    { key: 'users', name: '用户管理', short: '用户', icon: 'users', group: '系统管理', perm: 'admin.users' },
    { key: 'settings', name: '系统参数', short: '参数', icon: 'settings', group: '系统管理', perm: 'settings.view' },
    { key: 'ai_settings', name: 'AI 设置', short: 'AI', icon: 'spark', group: '系统管理', perm: 'admin.ai' },
    { key: 'audit', name: '操作审计', short: '审计', icon: 'shield', group: '系统管理', perm: 'admin.audit' },
    /* hidden：可路由（#/upgrade）但不进侧边栏 / 手机标签栏——入口在右上角用户菜单 */
    { key: 'upgrade', name: '系统更新', short: '更新', icon: 'update', group: '系统管理', perm: 'admin.settings.write', hidden: true },
    { key: 'backup', name: '数据备份', short: '备份', icon: 'backup', group: '系统管理', perm: 'admin.settings.write', hidden: true },
  ];

  const LEGACY = { reimbursements_view: 'reimbursements' };

  WB.meta = { departments: [], employees: [], categories: [], customers: [], projects: [], invoice_types: [] };
  WB.viewParams = null;

  function visibleViews() {
    return VIEWS.filter((v) => !v.perm || WB.auth.can(v.perm));
  }

  function currentKey() {
    const list = visibleViews();
    const h = (location.hash || '').replace(/^#\/?/, '').split('?')[0];
    const key = LEGACY[h] || h;
    return list.some((v) => v.key === key) ? key : list[0].key;
  }

  WB.go = function (key, params) {
    WB.viewParams = params || null;
    if (currentKey() === key) return WB.rerender();
    location.hash = '#/' + key;
  };

  /* ---------------- 导航渲染 ---------------- */
  let navBadges = {};

  function renderNav() {
    const cur = currentKey();
    const items = visibleViews().filter((v) => !v.hidden).map((v) => ({
      key: v.key,
      name: v.name,
      short: v.short,
      icon: ICONS[v.icon],
      group: v.group,
      badge: v.badge ? navBadges[v.badge] || 0 : 0,
    }));

    let html = '';
    let lastGroup = null;
    for (const v of items) {
      if (v.group !== lastGroup) {
        html += `<div class="nav-group">${v.group}</div>`;
        lastGroup = v.group;
      }
      html += `<div class="nav-item ${cur === v.key ? 'active' : ''}" data-key="${v.key}">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
          stroke-linecap="round" stroke-linejoin="round">${v.icon}</svg>
        <span class="lbl">${v.name}</span>
        ${v.badge ? `<span class="nav-badge">${v.badge > 99 ? '99+' : v.badge}</span>` : ''}
      </div>`;
    }
    U.qs('#nav').innerHTML = html;

    // 手机端底部标签栏复用同一份导航数据，权限过滤只发生在这里一次
    if (WB.mobile) WB.mobile.syncNav(items, cur);
  }

  function renderTopbar(cfg) {
    U.qs('#page-title').textContent = cfg.title;
    U.qs('#page-sub').textContent = cfg.sub || '';
    const tb = U.qs('#toolbar');
    if (cfg.toolbar) {
      tb.style.display = '';
      tb.innerHTML = cfg.toolbar;
    } else {
      tb.style.display = 'none';
      tb.innerHTML = '';
    }
    renderUserChip();
  }

  function renderUserChip() {
    const u = WB.auth.user || {};
    U.qs('#user-chip').innerHTML = `
      <span class="uc-avatar">${U.esc((u.name || '?').slice(0, 1))}</span>
      <span class="uc-body">
        <b>${U.esc(u.name || '')}</b>
        <em class="role-tag ${WB.auth.roleTone()}">${U.esc(WB.auth.roleLabel())}</em>
      </span>
      <svg class="uc-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
        stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg>`;
  }

  /* 用户菜单：改密码 / 退出登录 */
  function openUserMenu(anchor) {
    const u = WB.auth.user || {};
    const old = document.getElementById('user-menu');
    if (old) {
      old.remove();
      return;
    }
    const el = document.createElement('div');
    el.id = 'user-menu';
    el.className = 'user-menu';
    const meta = [];
    if (u.username) meta.push(`账号 ${U.esc(u.username)}`);
    if (u.department_name) meta.push(U.esc(u.department_name));
    if (u.employee_name) meta.push(U.esc(u.employee_name));
    el.innerHTML = `
      <div class="um-head">
        <b>${U.esc(u.name || '')}</b>
        <div class="um-role"><span class="role-tag ${WB.auth.roleTone()}">${U.esc(WB.auth.roleLabel())}</span></div>
        <div class="um-meta">${meta.join(' · ')}</div>
      </div>
      <div class="um-item" data-act="pwd">修改密码</div>
      ${WB.auth.can('admin.settings.write') ? '<div class="um-item" data-act="upgrade">系统更新</div>' : ''}
      ${WB.auth.can('admin.settings.write') ? '<div class="um-item" data-act="backup">数据备份</div>' : ''}
      <div class="um-item um-danger" data-act="logout">退出登录</div>`;
    document.body.appendChild(el);

    const rect = anchor.getBoundingClientRect();
    el.style.top = rect.bottom + 8 + 'px';
    el.style.right = Math.max(12, window.innerWidth - rect.right) + 'px';

    const close = (e) => {
      if (!el.contains(e.target) && !anchor.contains(e.target)) {
        el.remove();
        document.removeEventListener('click', close, true);
      }
    };
    setTimeout(() => document.addEventListener('click', close, true), 0);

    el.querySelector('[data-act="pwd"]').onclick = () => {
      el.remove();
      WB.auth.promptForceChange();
    };
    const upgItem = el.querySelector('[data-act="upgrade"]');
    if (upgItem) {
      upgItem.onclick = () => {
        el.remove();
        WB.go('upgrade');
      };
    }
    const bkpItem = el.querySelector('[data-act="backup"]');
    if (bkpItem) {
      bkpItem.onclick = () => {
        el.remove();
        WB.go('backup');
      };
    }
    el.querySelector('[data-act="logout"]').onclick = async () => {
      el.remove();
      const ok = await U.confirm('确定要退出登录吗？', { okText: '退出' });
      if (!ok) return;
      await WB.auth.logout();
      // 登出时把「视图代次」推一格：可能还有一次 renderView 卡在 await 里没回来，
      // 它回来后会走到收尾的 refreshNavBadges()，此时 token 已清 → 白发一串 401。
      viewToken += 1;
      // 对话里可能带金额与公司名，登出时一并清掉，别留在会话存储里
      sessionStorage.removeItem('wb.ai.history');
      location.reload();
    };
  }

  /* ---------------- 视图生命周期 ---------------- */
  let viewToken = 0;

  async function renderView() {
    const key = currentKey();
    const token = ++viewToken;
    renderNav();

    const root = U.qs('#view-inner');
    WB.charts.disposeAll();
    root.innerHTML = '<div style="padding:80px"><div class="empty"><span class="spin"></span>加载中…</div></div>';

    const view = WB.views[key];
    if (!view) {
      root.innerHTML = '<div class="card"><div class="empty">视图不存在</div></div>';
      return;
    }
    try {
      const cfg = (await view(root)) || {};
      if (token !== viewToken) return;
      renderTopbar(cfg);
      if (cfg.onToolbar) cfg.onToolbar(U.qs('#toolbar'), () => WB.rerender());
      // 手机端：折叠筛选、把表格转成卡片。放在 onToolbar 之后——
      // 折叠要搬动工具条里的控件，得等视图把 onToolbar 绑完再动，
      // 否则这里搬一次、onToolbar 又按旧节点取值，会插进空气里。
      if (WB.mobile) WB.mobile.afterView();
      refreshNavBadges();
    } catch (e) {
      if (token !== viewToken) return;
      // 会话问题已经由 api 层弹回登录页，这里不再叠加报错
      if (e && e.message === '未登录或登录已过期') return;
      console.error(e);
      root.innerHTML = `<div class="card"><div class="empty">加载失败：${U.esc(e.message || e)}</div></div>`;
    }
  }

  WB.rerender = function () {
    return renderView();
  };

  WB.refreshMeta = async function () {
    const meta = await api.meta();
    WB.meta = Object.assign(WB.meta, meta);
    return WB.meta;
  };

  async function refreshNavBadges() {
    // 角标是登录态下的附加信息：没会话时直接不发，否则登出瞬间会打出一串 401
    if (!WB.auth.token) return;
    const jobs = [];
    if (WB.auth.can('stats.analyst') || WB.auth.can('invoice.write')) {
      jobs.push(
        api.invoiceSummary().then((s) => ({ k: 'invoiceBadge', v: s.duplicate_count })).catch(() => null)
      );
    }
    if (WB.auth.can('stats.analyst')) {
      jobs.push(
        api
          .alerts({})
          .then((a) => ({
            k: 'alertBadge',
            v:
              (a.summary.over_limit_count || 0) +
              (a.summary.bad_invoice_count || 0) +
              (a.summary.overdue_count || 0),
          }))
          .catch(() => null)
      );
    }
    const rs = (await Promise.all(jobs)).filter(Boolean);
    if (rs.length) {
      rs.forEach((r) => (navBadges[r.k] = r.v));
      renderNav();
    }
  }

  /* ---------------- 启动 ---------------- */
  WB.start = async function () {
    const u = WB.auth.user || {};
    U.qs('#app').style.display = '';
    renderUserChip();
    // 手机版外壳（抽屉、底部标签栏、表格卡片化）。init 幂等，重复登录也只挂一次。
    if (WB.mobile) WB.mobile.init();

    U.qs('#user-chip').onclick = (e) => {
      e.stopPropagation();
      openUserMenu(e.currentTarget);
    };

    try {
      const h = await api.health();
      U.qs('#db-chip').textContent = `v${h.version} · 数据库${h.database === 'up' ? '正常' : '异常'}`;
      U.qs('#db-chip').title = `${h.app} ${h.version} · schema ${h.schema_revision || '-'}`;
      U.qs('#brand-ver').textContent = 'v' + h.version;
    } catch (e) {
      U.qs('#db-chip').textContent = '后端未连接';
      U.qs('#brand-ver').textContent = 'v—';
    }

    await WB.refreshMeta();
    await renderView();
    refreshNavBadges();

    // 智能助手：挂在右下角，与主视图解耦，加载失败也不影响主流程
    try {
      await WB.ai.init();
    } catch (e) {
      console.warn('AI 助手初始化失败', e);
    }

    // 管理员代设的初始密码：进来先强制改掉
    if (u.must_change_password || sessionStorage.getItem('wb.force_change') === '1') {
      sessionStorage.removeItem('wb.force_change');
      WB.auth.promptForceChange();
    }
  };

  async function boot() {
    const nav = U.qs('#nav');
    nav.addEventListener('click', (e) => {
      const it = e.target.closest('.nav-item');
      if (it) WB.go(it.dataset.key);
    });
    window.addEventListener('hashchange', () => {
      WB.viewParams = null;
      if (WB.auth.token) renderView();
    });

    const user = await WB.auth.restore();
    if (!user) {
      U.qs('#app').style.display = 'none';
      WB.auth.showLogin();
      return;
    }
    await WB.start();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
