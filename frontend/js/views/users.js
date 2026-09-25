/* 视图：用户管理（仅管理员） */
window.WB = window.WB || {};
WB.views = WB.views || {};

(function () {
  const U = WB.util;
  const api = WB.api;

  const state = { q: '', role: '', page: 1, page_size: 20 };

  const ROLE_DESC = {
    申请人: '只能查看/维护自己的报销单',
    审批人: '按级别审批本部门的报销单',
    财务: '登记发票、维护预算与费用类型、付款',
    管理员: '全部权限，含账号、参数与审计',
  };

  function roleBadge(role) {
    const tone =
      { 管理员: 'r-admin', 财务: 'r-finance', 审批人: 'r-approver', 申请人: 'r-applicant' }[role] || 'r-applicant';
    return `<span class="role-tag ${tone}">${U.esc(role)}</span>`;
  }

  function userForm(row, meta, onDone) {
    const isNew = !row;
    const d = row || { username: '', name: '', role: '申请人', employee_id: null, approval_level: 0, active: true };
    const emps = meta.employees.map((e) => ({ id: e.id, name: `${e.name}（${e.department_name || '无部门'}）` }));

    U.modal({
      title: isNew ? '新增账号' : `编辑账号 · ${d.username}`,
      width: 640,
      body: `
        <div class="form-grid">
          <div class="form-item"><label>用户名<span class="req">*</span></label>
            <input id="u-username" value="${U.esc(d.username)}" ${isNew ? '' : 'disabled'} placeholder="登录用，2-64 字符"></div>
          <div class="form-item"><label>姓名<span class="req">*</span></label>
            <input id="u-name" value="${U.esc(d.name)}"></div>
          <div class="form-item full"><label>角色<span class="req">*</span></label>
            <select id="u-role">
              ${meta.roles.map((r) => `<option value="${U.esc(r)}" ${d.role === r ? 'selected' : ''}>${U.esc(r)} — ${U.esc(ROLE_DESC[r] || '')}</option>`).join('')}
            </select></div>
          <div class="form-item"><label>关联员工</label>
            <select id="u-emp">${U.options(emps, { selected: d.employee_id, placeholder: '不关联' })}</select>
            <div class="hint">申请人 / 审批人必须关联员工，否则无法建单与按部门审批</div></div>
          <div class="form-item" id="u-level-wrap"><label>审批级别</label>
            <select id="u-level">
              <option value="0" ${!d.approval_level ? 'selected' : ''}>0 — 无审批权限</option>
              <option value="1" ${d.approval_level === 1 ? 'selected' : ''}>1 — 可审一级（普通金额）</option>
              <option value="2" ${d.approval_level === 2 ? 'selected' : ''}>2 — 可审二级（大额）</option>
            </select></div>
          <div class="form-item full"><label>状态</label>
            <label style="display:flex;align-items:center;gap:6px;height:32px">
              <input type="checkbox" id="u-active" ${d.active ? 'checked' : ''}> 启用（停用后该账号会话立即失效）
            </label></div>
          ${
            isNew
              ? `<div class="form-item full"><label>初始密码<span class="req">*</span></label>
                   <input id="u-pwd" type="text" placeholder="至少 8 位，含字母与数字两类">
                   <div class="hint">作为「首次登录需修改」下发，用户登录后会被要求立即更换</div></div>`
              : ''
          }
        </div>`,
      footer: `<button class="btn" data-close>取消</button>
               <button class="btn btn-primary" data-ok>${isNew ? '创建' : '保存'}</button>`,
      onMount(m) {
        const roleSel = U.qs('#u-role', m.el);
        const levelWrap = U.qs('#u-level-wrap', m.el);
        const syncLevel = () => {
          levelWrap.style.display = roleSel.value === '审批人' ? '' : 'none';
        };
        roleSel.onchange = syncLevel;
        syncLevel();

        U.qs('[data-ok]', m.el).onclick = async () => {
          const payload = {
            name: U.qs('#u-name', m.el).value.trim(),
            role: roleSel.value,
            employee_id: U.qs('#u-emp', m.el).value ? Number(U.qs('#u-emp', m.el).value) : null,
            approval_level: Number(U.qs('#u-level', m.el).value) || 0,
            active: U.qs('#u-active', m.el).checked,
          };
          if (!payload.name) return U.toast('请填写姓名', 'warn');
          if (isNew) {
            payload.username = U.qs('#u-username', m.el).value.trim();
            if (!payload.username) return U.toast('请填写用户名', 'warn');
            payload.password = U.qs('#u-pwd', m.el).value;
            if (!payload.password) return U.toast('请设置初始密码', 'warn');
          }
          try {
            if (isNew) await api.createUser(payload);
            else await api.updateUser(d.id, payload);
            U.toast(isNew ? '账号已创建' : '已保存', 'success');
            m.close();
            onDone();
          } catch (e) {}
        };
      },
    });
  }

  function resetPwdForm(row, onDone) {
    U.modal({
      title: `重置密码 · ${row.username}`,
      width: 480,
      body: `
        <p class="muted" style="font-size:12.5px;margin:0 0 12px">
          重置后该账号所有登录状态会立即失效，需用新密码重新登录（首次登录仍需再改一次）。
        </p>
        <div class="form-item"><label>新密码</label>
          <input id="rp-pwd" type="text" placeholder="至少 8 位，含字母与数字两类"></div>`,
      footer: `<button class="btn" data-close>取消</button><button class="btn btn-primary" data-ok>重置</button>`,
      onMount(m) {
        U.qs('[data-ok]', m.el).onclick = async () => {
          const p = U.qs('#rp-pwd', m.el).value;
          if (!p) return U.toast('请输入新密码', 'warn');
          try {
            await api.resetUserPassword(row.id, p);
            U.toast('密码已重置，该账号登录状态已失效', 'success');
            m.close();
            onDone();
          } catch (e) {}
        };
      },
    });
  }

  WB.views.users = async function (root) {
    root.innerHTML = `
      <div class="card">
        <div class="table-wrap" id="host">${U.loading(8)}</div>
        <div id="pager-host"></div>
      </div>`;

    const host = U.qs('#host', root);
    const pagerHost = U.qs('#pager-host', root);
    let roles = ['申请人', '审批人', '财务', '管理员'];
    let cache = [];

    async function load() {
      host.innerHTML = U.loading(8);
      const data = await api.users({
        q: state.q, role: state.role, page: state.page, page_size: state.page_size,
      });
      roles = data.roles || roles;
      cache = data.items;
      const me = WB.auth.user || {};

      host.innerHTML = `
        <table class="tbl">
          <thead><tr>
            <th>用户名 / 姓名</th><th>角色</th><th>审批级别</th><th>关联员工</th>
            <th>所属部门</th><th>状态</th><th>最近登录</th><th style="width:1%">操作</th>
          </tr></thead>
          <tbody>
            ${
              data.items.length
                ? data.items
                    .map(
                      (u) => `<tr data-id="${u.id}">
              <td><div class="mono strong">${U.esc(u.username)}</div><div class="sub-line">${U.esc(u.name)}</div></td>
              <td>${roleBadge(u.role)}</td>
              <td>${u.role === '审批人' ? `${u.approval_level} 级` : '<span class="muted">—</span>'}</td>
              <td>${U.esc(u.employee_name || '—')}</td>
              <td>${U.esc(u.department_name || '—')}</td>
              <td>${u.active ? '<span class="badge b-green">启用</span>' : '<span class="badge b-gray">停用</span>'}
                ${u.must_change_password ? '<span class="badge b-orange">待改密</span>' : ''}</td>
              <td class="sub-line nowrap">${U.datetime(u.last_login_at)}</td>
              <td><div class="row-actions">
                <button class="btn btn-xs" data-act="edit">编辑</button>
                <button class="btn btn-xs" data-act="reset">重置密码</button>
                <button class="btn btn-xs ${u.active ? '' : 'btn-success'}" data-act="toggle" ${u.id === me.id ? 'disabled' : ''}>
                  ${u.active ? '停用' : '启用'}</button>
                <button class="btn btn-xs btn-danger" data-act="del" ${u.id === me.id ? 'disabled' : ''}>删除</button>
              </div></td>
            </tr>`
                    )
                    .join('')
                : U.emptyRow(8, '没有符合条件的账号')
            }
          </tbody>
        </table>`;
      pagerHost.innerHTML = U.pager(data.total, data.page, data.page_size);
    }

    host.addEventListener('click', async (e) => {
      const b = e.target.closest('button[data-act]');
      if (!b || b.disabled) return;
      const id = Number(b.closest('tr').dataset.id);
      const row = cache.find((x) => x.id === id);
      const act = b.dataset.act;
      const meta = { employees: WB.meta.employees, roles };

      if (act === 'edit') {
        userForm(row, meta, load);
      } else if (act === 'reset') {
        resetPwdForm(row, load);
      } else if (act === 'toggle') {
        const ok = await U.confirm(
          row.active
            ? `停用账号 <b>${U.esc(row.username)}</b>？该账号的所有登录状态会立即失效。`
            : `重新启用账号 <b>${U.esc(row.username)}</b>？`,
          { okText: row.active ? '停用' : '启用', danger: row.active }
        );
        if (!ok) return;
        await api.updateUser(id, { active: !row.active });
        U.toast(row.active ? '已停用' : '已启用', 'success');
        load();
      } else if (act === 'del') {
        const ok = await U.confirm(
          `确认删除账号 <b>${U.esc(row.username)}</b>？<span class="muted">删除后不可恢复，建议优先「停用」。</span>`,
          { title: '删除账号', okText: '删除', danger: true }
        );
        if (!ok) return;
        await api.deleteUser(id);
        U.toast('已删除', 'success');
        load();
      }
    });

    pagerHost.addEventListener('click', (e) => {
      const p = e.target.closest('button[data-page]');
      if (!p || p.disabled) return;
      state.page = Number(p.dataset.page);
      load();
    });

    await load();

    return {
      title: '用户管理',
      sub: '账号、角色与审批级别维护',
      toolbar: `
        <input class="search-input" id="f-q" placeholder="用户名 / 姓名" value="${U.esc(state.q)}">
        <div class="field"><label>角色</label>
          <select id="f-role">
            <option value="">全部</option>
            ${roles.map((r) => `<option value="${U.esc(r)}" ${state.role === r ? 'selected' : ''}>${U.esc(r)}</option>`).join('')}
          </select></div>
        <button class="btn btn-sm" id="f-reset">重置</button>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="btn-refresh">刷新</button>
        <button class="btn btn-sm btn-primary" id="btn-new">+ 新增账号</button>`,
      onToolbar(tb) {
        U.qs('#f-q', tb).oninput = U.debounce((e) => {
          state.q = e.target.value.trim();
          state.page = 1;
          load();
        }, 400);
        U.qs('#f-role', tb).onchange = (e) => {
          state.role = e.target.value;
          state.page = 1;
          load();
        };
        U.qs('#f-reset', tb).onclick = () => {
          state.q = '';
          state.role = '';
          state.page = 1;
          WB.rerender();
        };
        U.qs('#btn-refresh', tb).onclick = () => load();
        U.qs('#btn-new', tb).onclick = () => userForm(null, { employees: WB.meta.employees, roles }, load);
      },
    };
  };
})();
