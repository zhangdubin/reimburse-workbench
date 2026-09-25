/* 视图：操作审计（仅管理员） */
window.WB = window.WB || {};
WB.views = WB.views || {};

(function () {
  const U = WB.util;
  const api = WB.api;

  const state = {
    q: '', username: '', action: '', entity: '',
    date_from: '', date_to: '', page: 1, page_size: 50,
  };

  const FILTER_KEYS = ['q', 'username', 'action', 'entity', 'date_from', 'date_to'];

  function params(withPage = true) {
    const p = {};
    FILTER_KEYS.forEach((k) => (p[k] = state[k]));
    if (withPage) {
      p.page = state.page;
      p.page_size = state.page_size;
    }
    return p;
  }

  /* 状态码 -> 语义色：拒绝的请求用红色，方便一眼扫出异常 */
  function statusCell(code) {
    if (!code) return '<span class="muted">—</span>';
    const cls = code >= 500 ? 'b-red' : code >= 400 ? 'b-orange' : 'b-green';
    return `<span class="badge ${cls}">${code}</span>`;
  }

  function actionTone(action) {
    if (!action) return 'b-gray';
    if (action.includes('失败')) return 'b-red';
    if (action.includes('删除')) return 'b-red';
    if (action.includes('登录') || action.includes('登出')) return 'b-blue';
    if (action.includes('审批') || action.includes('付款')) return 'b-green';
    return 'b-gray';
  }

  WB.views.audit = async function (root) {
    root.innerHTML = `
      <div class="card">
        <div style="padding:12px 16px 0">
          <div class="batch-bar" id="au-bar" style="display:none">
            已选 <b id="au-picked">0</b> 条日志
            <span class="muted">清理审计日志本身也会留下一条记录</span>
            <div class="spacer"></div>
            <button class="btn btn-sm btn-danger" id="au-del">删除选中</button>
          </div>
        </div>
        <div class="table-wrap" id="host">${U.loading(8)}</div>
        <div id="pager-host"></div>
      </div>`;

    const host = U.qs('#host', root);
    const pagerHost = U.qs('#pager-host', root);
    const bar = U.qs('#au-bar', root);
    let actions = [];

    function syncBar() {
      const ids = U.pickedIds(host);
      U.qs('#au-picked', bar).textContent = ids.length;
      bar.style.display = ids.length ? '' : 'none';
      U.qsa('tr[data-id]', host).forEach((tr) => {
        const c = U.qs('.ck-row', tr);
        tr.classList.toggle('picked', !!(c && c.checked));
      });
    }

    async function load() {
      host.innerHTML = U.loading(8);
      const data = await api.auditLogs(params());
      actions = data.actions || actions;

      host.innerHTML = `
        <table class="tbl">
          <thead><tr>
            <th class="ck-col"><input type="checkbox" id="ck-all"></th>
            <th style="width:150px">时间</th><th>操作人</th><th>角色</th><th>动作</th>
            <th>对象</th><th>请求</th><th class="num">状态</th><th>来源 IP</th><th>详情</th>
          </tr></thead>
          <tbody>
            ${
              data.items.length
                ? data.items
                    .map(
                      (r) => `<tr data-id="${r.id}">
              <td class="ck-col"><input type="checkbox" class="ck-row" value="${r.id}"></td>
              <td class="sub-line nowrap">${U.datetime(r.created_at)}</td>
              <td><div class="strong">${U.esc(r.username || '匿名')}</div></td>
              <td class="muted">${U.esc(r.role || '—')}</td>
              <td><span class="badge ${actionTone(r.action)}">${U.esc(r.action)}</span></td>
              <td class="sub-line">${U.esc(r.entity || '—')}${r.entity_id ? ` #${U.esc(r.entity_id)}` : ''}</td>
              <td class="sub-line mono ellipsis">${U.esc(r.method || '')} ${U.esc(r.path || '')}</td>
              <td class="num">${statusCell(r.status_code)}</td>
              <td class="sub-line mono">${U.esc(r.ip || '—')}</td>
              <td class="muted ellipsis" title="${U.esc(r.detail || '')}">${U.esc(r.detail || '—')}</td>
            </tr>`
                    )
                    .join('')
                : U.emptyRow(10, '没有符合条件的审计记录')
            }
          </tbody>
        </table>`;
      U.bindCheckAll(host);
      syncBar();
      pagerHost.innerHTML = U.pager(data.total, data.page, data.page_size);
    }

    host.addEventListener('change', (e) => {
      if (e.target.classList.contains('ck-row') || e.target.id === 'ck-all') {
        if (e.target.id === 'ck-all') U.qsa('.ck-row', host).forEach((c) => (c.checked = e.target.checked));
        syncBar();
      }
    });

    bar.addEventListener('click', async (e) => {
      if (!e.target.closest('#au-del')) return;
      const ids = U.pickedIds(host);
      if (!ids.length) return U.toast('请先勾选要删除的日志', 'warn');
      const ok = await U.dangerConfirm(
        `即将删除 <b>${ids.length}</b> 条审计日志。审计留痕被清除后无法还原。`,
        '删除审计日志',
        { okText: `删除 ${ids.length} 条` }
      );
      if (!ok) return;
      const r = await api.deleteAuditLogs(ids);
      U.toast(`已删除 ${r.deleted || 0} 条审计日志`, 'success');
      await load();
    });

    pagerHost.addEventListener('click', (e) => {
      const p = e.target.closest('button[data-page]');
      if (!p || p.disabled) return;
      state.page = Number(p.dataset.page);
      load();
      root.parentElement.scrollTop = 0;
    });

    await load();

    return {
      title: '操作审计',
      sub: '所有写操作与登录事件的留痕',
      toolbar: `
        <input class="search-input" id="f-q" placeholder="操作人 / 路径 / 详情" value="${U.esc(state.q)}">
        <div class="field"><label>动作</label>
          <select id="f-action">
            <option value="">全部</option>
            ${actions.map((a) => `<option value="${U.esc(a)}" ${state.action === a ? 'selected' : ''}>${U.esc(a)}</option>`).join('')}
          </select></div>
        <div class="field"><label>对象</label>
          <select id="f-entity">
            <option value="">全部</option>
            ${['auth', 'reimbursement', 'invoice', 'user', 'setting', 'department', 'employee', 'category', 'customer', 'project', 'budget', 'mail_account', 'mail_message']
              .map((x) => `<option value="${x}" ${state.entity === x ? 'selected' : ''}>${x}</option>`)
              .join('')}
          </select></div>
        <div class="field"><label>日期</label>
          <input type="date" id="f-from" value="${state.date_from}"> <span class="muted">~</span>
          <input type="date" id="f-to" value="${state.date_to}"></div>
        <button class="btn btn-sm" id="f-reset">重置</button>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="btn-purge-old">清理 90 天前</button>
        <button class="btn btn-sm btn-danger" id="btn-purge-all">清空全部</button>
        <button class="btn btn-sm" id="btn-refresh">刷新</button>`,
      onToolbar(tb) {
        const reload = () => {
          state.page = 1;
          load();
        };
        U.qs('#f-q', tb).oninput = U.debounce((e) => {
          state.q = e.target.value.trim();
          reload();
        }, 400);
        U.qs('#f-action', tb).onchange = (e) => {
          state.action = e.target.value;
          reload();
        };
        U.qs('#f-entity', tb).onchange = (e) => {
          state.entity = e.target.value;
          reload();
        };
        ['#f-from', '#f-to'].forEach((s, i) => {
          U.qs(s, tb).onchange = (e) => {
            state[i === 0 ? 'date_from' : 'date_to'] = e.target.value;
            reload();
          };
        });
        U.qs('#f-reset', tb).onclick = () => {
          FILTER_KEYS.forEach((k) => (state[k] = ''));
          state.page = 1;
          WB.rerender();
        };
        U.qs('#btn-refresh', tb).onclick = () => load();
        U.qs('#btn-purge-old', tb).onclick = async () => {
          const ok = await U.dangerConfirm(
            '即将清理 <b>90 天前</b>的全部审计日志，近期记录会保留。',
            '清理 90 天前',
            { okText: '清理 90 天前' }
          );
          if (!ok) return;
          const r = await api.purgeAuditLogs('清空审计日志', { before_days: 90 });
          U.toast(`已清理 ${r.deleted || 0} 条（范围：${r.scope || '90 天前'}）`, 'success', 4200);
          state.page = 1;
          await load();
        };
        U.qs('#btn-purge-all', tb).onclick = async () => {
          const ok = await U.dangerConfirm(
            '即将<b>清空全部审计日志</b>，所有历史留痕都会消失。建议先导出或改用「清理 90 天前」。',
            '清空审计日志',
            { okText: '清空全部' }
          );
          if (!ok) return;
          const r = await api.purgeAuditLogs('清空审计日志');
          U.toast(`已清空 ${r.deleted || 0} 条审计日志`, 'success', 4200);
          state.page = 1;
          await load();
        };
      },
    };
  };
})();
