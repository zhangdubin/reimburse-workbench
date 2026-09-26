/* 视图：费用管理 */
window.WB = window.WB || {};
WB.views = WB.views || {};

(function () {
  const U = WB.util;
  const api = WB.api;
  const C = WB.charts;

  const state = {
    tab: 'category',
    base: 'customer',
    q: '', category_id: '', department_id: '', status: '', date_from: '', date_to: '',
    sort: 'date_desc', page: 1, page_size: 20, year: U.year(),
  };

  /* 写权限按子页签/实体区分，与后端的分档保持一致 */
  const BASE_PERM = {
    department: 'master.dept.write',
    customer: 'master.customer.write',
    project: 'master.project.write',
    employee: 'master.employee.write',
  };

  function writePerm() {
    if (state.tab === 'category') return 'master.category.write';
    if (state.tab === 'budget') return 'master.budget.write';
    if (state.tab === 'items') return null;
    return BASE_PERM[state.base];
  }
  function canWrite() {
    const p = writePerm();
    return !!p && WB.can(p);
  }
  /** 预算页签读接口本身就需要财务/管理员权限，其他人直接不显示这个页签 */
  function tabAllowed(key) {
    if (key === 'budget') return WB.can('stats.analyst');
    return true;
  }
  const RO_ACTIONS = '<span class="muted" style="font-size:12px">只读</span>';

  /* ---------------- 通用批量删除装配 ----------------
   * 约定：scope 内的表格必须带 <th class="ck-col"><input id="ck-all">，
   * 每行带 <td class="ck-col"><input class="ck-row" value="id">，
   * 并在工具栏下方放一个 [data-batch-bar] 容器（内容由 batchBarHtml 生成）。
   */
  function batchBarHtml(unit, tip) {
    return `<div class="batch-bar" data-batch-bar style="display:none">
      已选 <b data-batch-n>0</b> ${unit}
      ${tip ? `<span class="muted">${tip}</span>` : ''}
      <div class="spacer"></div>
      <button class="btn btn-sm btn-danger" data-batch-del>批量删除</button>
    </div>`;
  }

  function wireBatch(scope, { res, unit = '条', reload, message }) {
    const bar = U.qs('[data-batch-bar]', scope);
    if (!bar) return;
    const sync = () => {
      const n = U.pickedIds(scope).length;
      U.qs('[data-batch-n]', bar).textContent = n;
      bar.style.display = n ? '' : 'none';
      U.qsa('tr[data-id]', scope).forEach((tr) => {
        const c = U.qs('.ck-row', tr);
        tr.classList.toggle('picked', !!(c && c.checked));
      });
    };
    U.bindCheckAll(scope);
    sync();
    scope.addEventListener('change', (e) => {
      if (e.target.classList.contains('ck-row') || e.target.id === 'ck-all') {
        if (e.target.id === 'ck-all') U.qsa('.ck-row', scope).forEach((c) => (c.checked = e.target.checked));
        sync();
      }
    });
    bar.addEventListener('click', async (e) => {
      if (!e.target.closest('[data-batch-del]')) return;
      const ids = U.pickedIds(scope);
      if (!ids.length) return U.toast('请先勾选要删除的记录', 'warn');
      const ok = await U.dangerConfirm(
        (message ? message(ids.length) : `即将删除 <b>${ids.length}</b> 条记录。`) +
          '被业务数据引用的记录会自动跳过并说明原因。',
        '批量删除',
        { okText: `删除 ${ids.length} 项` }
      );
      if (!ok) return;
      const r = await api.batchDelete(res, ids);
      U.toast(U.batchResult(r, unit), r.skipped_count ? 'warn' : 'success', 4600);
      if (r.skipped_count && U.skipReasons(r)) U.toast(U.skipReasons(r), 'warn', 6000);
      reload();
    });
  }

  /* ---------------- 通用表单弹窗 ---------------- */
  function entityForm({ title, fields, initial = {}, onSubmit, width = 620 }) {
    const html = fields
      .map((f) => {
        const v = initial[f.key];
        let input;
        if (f.type === 'select') {
          input = `<select id="ef-${f.key}">${U.options(f.options || [], {
            selected: v, placeholder: f.placeholder || '请选择',
          })}</select>`;
        } else if (f.type === 'checkbox') {
          input = `<label style="display:flex;align-items:center;gap:6px;height:30px"><input type="checkbox" id="ef-${f.key}" ${v ? 'checked' : ''}> ${U.esc(f.hint || '')}</label>`;
        } else if (f.type === 'number') {
          input = `<input type="number" step="0.01" id="ef-${f.key}" value="${v == null ? '' : v}">`;
        } else if (f.type === 'textarea') {
          const rows = f.rows || 2;
          input = `<textarea id="ef-${f.key}" rows="${rows}">${U.esc(v == null ? '' : v)}</textarea>`;
        } else if (f.type === 'date') {
          // v2.9.17：日期 input；浏览器 native picker
          input = `<input type="date" id="ef-${f.key}" value="${U.esc(v == null ? '' : v)}">`;
        } else {
          input = `<input type="text" id="ef-${f.key}" value="${U.esc(v == null ? '' : v)}">`;
        }
        return `<div class="form-item ${f.full ? 'full' : ''}">
          <label>${U.esc(f.label)}${f.required ? '<span class="req">*</span>' : ''}</label>${input}</div>`;
      })
      .join('');

    U.modal({
      title,
      width,
      body: `<div class="form-grid">${html}</div>`,
      footer: `<button class="btn" data-close>取消</button><button class="btn btn-primary" data-save>保存</button>`,
      onMount(m) {
        U.qs('[data-save]', m.el).onclick = async () => {
          const payload = {};
          for (const f of fields) {
            const el = U.qs(`#ef-${f.key}`, m.el);
            if (f.type === 'checkbox') payload[f.key] = el.checked;
            else if (f.type === 'number') payload[f.key] = el.value === '' ? 0 : Number(el.value);
            else if (f.type === 'select') payload[f.key] = el.value === '' ? null : (f.numeric === false ? el.value : Number(el.value));
            else payload[f.key] = el.value === '' ? null : el.value;
          }
          for (const f of fields) {
            if (f.required && (payload[f.key] === null || payload[f.key] === '')) {
              return U.toast(`请填写「${f.label}」`, 'warn');
            }
          }
          try {
            await onSubmit(payload);
            m.close();
          } catch (e) {}
        };
      },
    });
  }

  /* ---------------- 费用类型 ---------------- */
  async function tabCategory(host, meta) {
    const cats = await api.list('categories');
    const w = WB.can('master.category.write');
    host.innerHTML = `
      <div class="toolbar">
        <span class="muted" style="font-size:12.5px">共 ${cats.length} 个费用类型，可在此维护报销标准与限额</span>
        <div class="spacer"></div>
        ${w ? '<button class="btn btn-sm btn-primary" id="cat-new">+ 新增费用类型</button>' : ''}
      </div>
      ${w ? batchBarHtml('个费用类型', '已被明细或发票引用的会自动跳过') : ''}
      <div class="table-wrap">
        <table class="tbl">
          <thead><tr>${w ? '<th class="ck-col"><input type="checkbox" id="ck-all"></th>' : ''}<th>费用类型</th><th>编码</th><th>大类</th><th>需发票</th><th class="num">单笔限额</th><th class="num">单日限额</th><th class="num">月限额</th><th>启用</th><th style="width:1%">操作</th></tr></thead>
          <tbody>
            ${cats
              .map(
                (c) => `<tr data-id="${c.id}">
              ${w ? `<td class="ck-col"><input type="checkbox" class="ck-row" value="${c.id}"></td>` : ''}
              <td class="strong">${U.esc(c.name)}</td>
              <td class="mono muted">${U.esc(c.code || '—')}</td>
              <td><span class="chip">${U.esc(c.group_name || '未分类')}</span></td>
              <td>${c.requires_invoice ? '<span class="badge b-blue">需要</span>' : '<span class="badge b-gray">可免</span>'}</td>
              <td class="num">${c.single_limit ? U.money(c.single_limit) : '<span class="muted">不限</span>'}</td>
              <td class="num">${c.daily_limit ? U.money(c.daily_limit) : '<span class="muted">不限</span>'}</td>
              <td class="num">${c.monthly_limit ? U.money(c.monthly_limit) : '<span class="muted">不限</span>'}</td>
              <td>${c.active ? '<span class="badge b-green">启用</span>' : '<span class="badge b-gray">停用</span>'}</td>
              <td>${w
                  ? `<div class="row-actions">
                <button class="btn btn-xs" data-act="edit">编辑</button>
                <button class="btn btn-xs btn-danger" data-act="del">删除</button>
              </div>`
                  : RO_ACTIONS}</td>
            </tr>`
              )
              .join('')}
          </tbody>
        </table>
      </div>`;

    const groups = [...new Set(meta.categories.map((c) => c.group_name).filter(Boolean))];
    const fields = [
      { key: 'name', label: '费用类型名称', required: true },
      { key: 'code', label: '编码' },
      { key: 'group_name', label: '所属大类', type: 'select', options: groups.map((g) => ({ id: g, name: g })), numeric: false },
      { key: 'requires_invoice', label: '是否必须附发票', type: 'checkbox', hint: '需要提供发票' },
      { key: 'single_limit', label: '单笔限额(元)', type: 'number' },
      { key: 'daily_limit', label: '单人单日限额(元)', type: 'number' },
      { key: 'monthly_limit', label: '单人月度限额(元)', type: 'number' },
      { key: 'active', label: '启用', type: 'checkbox', hint: '启用该费用类型' },
      { key: 'remark', label: '备注', full: true },
    ];

    const reload = () => WB.rerender();
    if (w) {
      wireBatch(host, {
        res: 'categories', unit: '个', reload,
        message: (n) => `即将删除 <b>${n}</b> 个费用类型，删除后历史明细仍保留名称快照。`,
      });
      U.qs('#cat-new', host).onclick = () =>
        entityForm({
          title: '新增费用类型',
          fields,
          initial: { requires_invoice: true, active: true, single_limit: 0, daily_limit: 0, monthly_limit: 0 },
          onSubmit: async (p) => {
            await api.create('categories', p);
            U.toast('已新增费用类型', 'success');
            reload();
          },
        });
    }

    host.addEventListener('click', async (e) => {
      const b = e.target.closest('button[data-act]');
      if (!b) return;
      const id = Number(b.closest('tr').dataset.id);
      const row = cats.find((c) => c.id === id);
      if (b.dataset.act === 'edit') {
        entityForm({
          title: `编辑费用类型 · ${row.name}`,
          fields,
          initial: row,
          onSubmit: async (p) => {
            await api.update('categories', id, p);
            U.toast('已保存', 'success');
            reload();
          },
        });
      } else {
        const ok = await U.confirm(`确认删除费用类型 <b>${U.esc(row.name)}</b>？<br><span class="muted">若已被报销明细或发票引用，系统会拒绝删除。</span>`, {
          title: '删除费用类型', okText: '删除', danger: true,
        });
        if (!ok) return;
        await api.remove('categories', id);
        U.toast('已删除', 'success');
        reload();
      }
    });

    return { count: cats.length };
  }

  /* ---------------- 费用预算 ---------------- */
  async function tabBudget(host, meta) {
    const buds = await api.budgetExecution({ year: state.year });
    const rows = buds.sort((a, b) => (b.usage_rate || 0) - (a.usage_rate || 0));
    const totalBudget = rows.reduce((s, b) => s + b.amount, 0);
    const totalUsed = rows.reduce((s, b) => s + b.used, 0);
    const over = rows.filter((b) => b.usage_rate > 100).length;

    host.innerHTML = `
      <div class="kpi-grid" style="padding:14px 16px 0">
        <div class="kpi"><div class="kpi-label">年度预算总额</div><div class="kpi-value">${U.moneyShort(totalBudget)}</div><div class="kpi-sub">${rows.length} 条预算记录</div></div>
        <div class="kpi k-blue"><div class="kpi-label">已执行金额</div><div class="kpi-value">${U.moneyShort(totalUsed)}</div><div class="kpi-sub">仅统计已通过 / 已付款</div></div>
        <div class="kpi ${over ? 'k-red' : 'k-green'}"><div class="kpi-label">整体执行率</div><div class="kpi-value">${totalBudget ? ((totalUsed / totalBudget) * 100).toFixed(1) : 0}%</div><div class="kpi-sub">${over ? `<span class="up">${over} 条已超支</span>` : '暂无超支'}</div></div>
      </div>
      <div class="card-body flush" style="padding:14px 16px 0">
        <div class="chart sm" id="c-budget"></div>
      </div>
      <div class="toolbar">
        <div class="field"><label>年度</label>
          <select id="bd-year">${[U.year() + 1, U.year(), U.year() - 1].map((y) => `<option value="${y}" ${state.year === y ? 'selected' : ''}>${y} 年</option>`).join('')}</select>
        </div>
        <span class="muted" style="font-size:12.5px">执行率 = 该维度下已通过/已付款的报销明细合计 ÷ 预算金额</span>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="bd-export">导出执行情况</button>
        ${canWrite() ? '<button class="btn btn-sm btn-primary" id="bd-new">+ 新增预算</button>' : ''}
      </div>
      ${canWrite() ? batchBarHtml('条预算', '已被引用的预算会自动跳过') : ''}
      <div class="table-wrap">
        <table class="tbl">
          <thead><tr>${canWrite() ? '<th class="ck-col"><input type="checkbox" id="ck-all"></th>' : ''}<th>周期</th><th>维度</th><th class="num">预算金额</th><th class="num">已用金额</th><th class="num">可用余额</th><th style="width:200px">执行率</th><th style="width:1%">操作</th></tr></thead>
          <tbody>
            ${
              rows.length
                ? rows
                    .map((b) => {
                      const rate = b.usage_rate || 0;
                      const cls = rate > 100 ? 'bad' : rate >= 80 ? 'warn' : '';
                      const dims = [
                        b.department_name && `部门：${b.department_name}`,
                        b.project_name && `项目：${b.project_name}`,
                        b.category_name && `费用类型：${b.category_name}`,
                      ].filter(Boolean);
                      return `<tr data-id="${b.id}">
                  ${canWrite() ? `<td class="ck-col"><input type="checkbox" class="ck-row" value="${b.id}"></td>` : ''}
                  <td class="nowrap">${b.month ? `${b.year}-${String(b.month).padStart(2, '0')}` : `${b.year} 全年`}</td>
                  <td>${dims.length ? dims.map((d) => `<span class="chip">${U.esc(d)}</span>`).join(' ') : '<span class="muted">全域预算</span>'}</td>
                  <td class="num">${U.money(b.amount)}</td>
                  <td class="num amount">${U.money(b.used)}</td>
                  <td class="num" style="color:${b.amount - b.used < 0 ? '#f53f3f' : 'inherit'}">${U.money(b.amount - b.used)}</td>
                  <td><div style="display:flex;align-items:center;gap:8px">
                    <div class="progress ${cls}" style="flex:1"><i style="width:${Math.min(rate, 100)}%"></i></div>
                    <span style="font-size:12px;min-width:46px;color:${rate > 100 ? '#f53f3f' : '#59647a'}">${rate}%</span>
                  </div></td>
                  <td><div class="row-actions">
                    <button class="btn btn-xs" data-act="edit">编辑</button>
                    <button class="btn btn-xs btn-danger" data-act="del">删除</button>
                  </div></td>
                </tr>`;
                    })
                    .join('')
                : U.emptyRow(canWrite() ? 8 : 7, '该年度还没有预算记录')
            }
          </tbody>
        </table>
      </div>`;

    /* 部门预算执行图 */
    const byDept = {};
    rows.forEach((b) => {
      if (!b.department_id) return;
      const k = b.department_name;
      byDept[k] = byDept[k] || { name: k, amount: 0, used: 0 };
      byDept[k].amount += b.amount;
      byDept[k].used += b.used;
    });
    const deptRows = Object.values(byDept).sort((a, b) => b.amount - a.amount);
    C.set(U.qs('#c-budget', host), {
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(255,255,255,.97)', borderColor: '#e7ebf2', textStyle: { color: '#1b2430', fontSize: 12 },
        axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(22,119,255,.06)' } },
        formatter: (ps) => {
          const r = deptRows[ps[0].dataIndex];
          const rate = r.amount ? ((r.used / r.amount) * 100).toFixed(1) : 0;
          return `<b>${r.name}</b><br/>预算：${U.money(r.amount)}<br/>已用：${U.money(r.used)}<br/>执行率：<b>${rate}%</b>`;
        },
      },
      legend: { right: 10, top: 0, itemWidth: 10, itemHeight: 10, textStyle: { color: '#7a8699', fontSize: 11 } },
      grid: { left: 6, right: 10, top: 30, bottom: 4, containLabel: true },
      xAxis: {
        type: 'category', data: deptRows.map((r) => r.name.length > 6 ? r.name.slice(0, 6) + '…' : r.name),
        axisLine: { lineStyle: { color: '#e7ebf2' } }, axisTick: { show: false }, axisLabel: { color: '#7a8699', fontSize: 11 },
      },
      yAxis: {
        type: 'value', splitLine: { lineStyle: { color: '#eef1f6' } }, axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: '#7a8699', fontSize: 11, formatter: (v) => U.moneyShort(v) },
      },
      series: [
        { name: '预算', type: 'bar', barMaxWidth: 20, data: deptRows.map((r) => r.amount), itemStyle: { color: '#d6e4ff', borderRadius: [4, 4, 0, 0] } },
        { name: '已用', type: 'bar', barMaxWidth: 20, data: deptRows.map((r) => r.used), itemStyle: { color: '#1677ff', borderRadius: [4, 4, 0, 0] } },
      ],
    });

    const budgetFields = [
      { key: 'year', label: '年度', type: 'number', required: true },
      { key: 'month', label: '月份（留空为全年预算）', type: 'number' },
      { key: 'department_id', label: '部门', type: 'select', options: meta.departments },
      { key: 'project_id', label: '项目', type: 'select', options: meta.projects },
      { key: 'category_id', label: '费用类型', type: 'select', options: meta.categories },
      { key: 'amount', label: '预算金额(元)', type: 'number', required: true },
      { key: 'remark', label: '备注', full: true },
    ];

    U.qs('#bd-year', host).onchange = (e) => {
      state.year = Number(e.target.value);
      WB.rerender();
    };
    if (canWrite()) {
      wireBatch(host, {
        res: 'budgets', unit: '条', reload: () => WB.rerender(),
        message: (n) => `即将删除 <b>${n}</b> 条预算记录（${state.year} 年度）。`,
      });
      U.qs('#bd-new', host).onclick = () =>
        entityForm({
          title: '新增预算',
          fields: budgetFields,
          initial: { year: state.year, amount: 0 },
          onSubmit: async (p) => {
            await api.create('budgets', p);
            U.toast('已新增预算', 'success');
            WB.rerender();
          },
        });
    }
    U.qs('#bd-export', host).onclick = () => {
      const header = ['年度', '月份', '部门', '项目', '费用类型', '预算金额', '已用金额', '执行率(%)'];
      const lines = [header.join(',')];
      rows.forEach((b) =>
        lines.push([b.year, b.month || '全年', b.department_name || '', b.project_name || '', b.category_name || '', b.amount, b.used, b.usage_rate].join(','))
      );
      const blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `预算执行情况_${state.year}.csv`;
      a.click();
      U.toast('已导出', 'success');
    };
    host.addEventListener('click', async (e) => {
      const b = e.target.closest('button[data-act]');
      if (!b) return;
      const id = Number(b.closest('tr').dataset.id);
      const row = rows.find((x) => x.id === id);
      if (b.dataset.act === 'edit') {
        entityForm({
          title: '编辑预算',
          fields: budgetFields,
          initial: row,
          onSubmit: async (p) => {
            await api.update('budgets', id, p);
            U.toast('已保存', 'success');
            WB.rerender();
          },
        });
      } else {
        const ok = await U.confirm('确认删除该条预算？', { title: '删除预算', okText: '删除', danger: true });
        if (!ok) return;
        await api.remove('budgets', id);
        U.toast('已删除', 'success');
        WB.rerender();
      }
    });

    return { count: rows.length };
  }

  /* ---------------- 费用明细台账 ---------------- */
  async function tabItems(host, meta) {
    host.innerHTML = `
      <div class="toolbar">
        <input class="search-input" id="it-q" placeholder="摘要 / 单号 / 标题" value="${U.esc(state.q)}">
        <div class="field"><label>费用类型</label><select id="it-cat">${U.options(meta.categories, { selected: state.category_id, placeholder: '全部' })}</select></div>
        <div class="field"><label>部门</label><select id="it-dept">${U.options(meta.departments, { selected: state.department_id, placeholder: '全部' })}</select></div>
        <div class="field"><label>单据状态</label>
          <select id="it-status"><option value="">全部</option>
            ${['草稿', '待审批', '已通过', '已付款', '已驳回'].map((s) => `<option value="${s}" ${state.status === s ? 'selected' : ''}>${s}</option>`).join('')}
          </select></div>
        <div class="field"><label>费用发生日</label>
          <input type="date" id="it-from" value="${state.date_from}"> <span class="muted">~</span>
          <input type="date" id="it-to" value="${state.date_to}"></div>
        <button class="btn btn-sm" id="it-reset">重置</button>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="it-export">导出明细</button>
      </div>
      <div class="table-wrap" id="it-host">${U.loading(8)}</div>
      <div id="it-pager"></div>`;

    const listHost = U.qs('#it-host', host);
    const pagerHost = U.qs('#it-pager', host);

    async function load() {
      listHost.innerHTML = U.loading(8);
      const data = await api.get('/api/items', {
        q: state.q, category_id: state.category_id, department_id: state.department_id,
        status: state.status, date_from: state.date_from, date_to: state.date_to,
        sort: state.sort, page: state.page, page_size: state.page_size,
      });
      if (!data.items.length) {
        listHost.innerHTML = U.emptyRow(8, '没有符合条件的费用明细');
      } else {
        listHost.innerHTML = `<table class="tbl">
          <thead><tr><th>费用发生日</th><th>费用类型</th><th>摘要</th><th>报销单</th><th>申请人 / 部门</th><th class="num">税额</th><th class="num">金额</th><th>发票</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (i) => `<tr>
              <td class="nowrap">${U.date(i.occur_date)}</td>
              <td><div>${U.esc(i.category_name || '未分类')}</div>${U.badge(i.reimbursement_status)}</td>
              <td class="muted ellipsis" title="${U.esc(i.description || '')}">${U.esc(i.description || '—')}</td>
              <td><div class="mono">${U.esc(i.reimbursement_code || '—')}</div>${i.reimbursement_title ? `<div class="sub-line ellipsis" title="${U.esc(i.reimbursement_title)}">${U.esc(i.reimbursement_title)}</div>` : ''}</td>
              <td><div>${U.esc(i.applicant_name || '—')}</div>${i.department_name ? `<div class="sub-line">${U.esc(i.department_name)}</div>` : ''}</td>
              <td class="num muted">${U.money(i.tax_amount)}</td>
              <td class="num amount">${U.money(i.amount)}</td>
              <td>${i.invoice_count ? `<span class="badge b-green">${i.invoice_count} 张</span>` : '<span class="badge b-red">缺票</span>'}</td>
            </tr>`
              )
              .join('')}
          </tbody>
          <tfoot><tr><td colspan="6">本页合计 / 全部符合条件合计</td>
            <td class="num amount">${U.money(data.items.reduce((s, x) => s + x.amount, 0))} / ${U.money(data.total_amount || 0)}</td><td></td></tr></tfoot>
        </table>`;
      }
      pagerHost.innerHTML = U.pager(data.total, data.page, data.page_size);
    }

    const reload = () => {
      state.page = 1;
      load();
    };
    U.qs('#it-q', host).oninput = U.debounce((e) => {
      state.q = e.target.value.trim();
      reload();
    }, 400);
    [['#it-cat', 'category_id'], ['#it-dept', 'department_id'], ['#it-status', 'status']].forEach(([s, k]) => {
      U.qs(s, host).onchange = (e) => {
        state[k] = e.target.value;
        reload();
      };
    });
    [['#it-from', 'date_from'], ['#it-to', 'date_to']].forEach(([s, k]) => {
      U.qs(s, host).onchange = (e) => {
        state[k] = e.target.value;
        reload();
      };
    });
    U.qs('#it-reset', host).onclick = () => {
      Object.assign(state, { q: '', category_id: '', department_id: '', status: '', date_from: '', date_to: '', page: 1 });
      WB.rerender();
    };
    U.qs('#it-export', host).onclick = () => {
      const q = Object.entries({ date_from: state.date_from, date_to: state.date_to })
        .filter(([, v]) => v)
        .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
        .join('&');
      U.download(`/api/export/items.csv${q ? '?' + q : ''}`);
    };
    pagerHost.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-page]');
      if (!b || b.disabled) return;
      state.page = Number(b.dataset.page);
      load();
    });

    await load();
    return {};
  }

  /* ---------------- 基础数据 ---------------- */
  /* 部门（v2.9.17 起补齐 parent_id / cost_center / is_active / description） */
  const BASE_CONF = {
    department: {
      res: 'departments', title: '部门', label: 'name',
      fields: [
        { key: 'name', label: '部门名称', required: true, full: true },
        { key: 'code', label: '部门编码' },
        { key: 'parent_id', label: '上级部门', type: 'select', options: () => WB.meta.departments, hint: '不选则为顶级部门' },
        { key: 'manager', label: '负责人', hint: '填员工姓名或工号（自由文本）' },
        { key: 'cost_center', label: '成本中心', hint: '财务预算/费用归集用' },
        { key: 'is_active', label: '启用', type: 'checkbox', hint: '停用后不在新增报销单的下拉里出现', full: true },
        { key: 'description', label: '部门描述', type: 'textarea', full: true, rows: 2 },
        { key: 'remark', label: '备注', full: true, type: 'textarea', rows: 2 },
      ],
      cols: ['部门名称', '编码', '上级', '负责人', '成本中心', '人员数', '状态'],
      row: (d) => [
        U.esc(d.name),
        `<span class="mono muted">${U.esc(d.code || '—')}</span>`,
        U.esc(d.parent_name || '—'),
        U.esc(d.manager || '—'),
        `<span class="mono muted">${U.esc(d.cost_center || '—')}</span>`,
        `<span class="badge b-blue">${d.employee_count ?? 0}</span>`,
        d.is_active ? '<span class="badge b-green">启用</span>' : '<span class="badge b-gray">停用</span>',
      ],
    },
    customer: {
      res: 'customers', title: '客户', label: 'name',
      fields: [
        { key: 'name', label: '客户名称', required: true, full: true },
        { key: 'code', label: '客户编码' },
        { key: 'industry', label: '所属行业' },
        { key: 'contact', label: '联系人' },
        { key: 'phone', label: '联系电话' },
        { key: 'tax_no', label: '税号', hint: '增值税开票用' },
        { key: 'address', label: '通讯地址', type: 'textarea', full: true, rows: 2 },
        { key: 'website', label: '官网' },
        { key: 'bank_info', label: '收款银行', full: true, hint: '格式：XX银行 XX支行 6228... ' },
        { key: 'remark', label: '备注', full: true, type: 'textarea', rows: 2 },
      ],
      cols: ['名称', '编码', '行业', '联系人', '电话'],
      row: (c) => [U.esc(c.name), `<span class="mono muted">${U.esc(c.code || '—')}</span>`, U.esc(c.industry || '—'), U.esc(c.contact || '—'), U.esc(c.phone || '—')],
    },
    project: {
      res: 'projects', title: '项目', label: 'name',
      fields: [
        { key: 'name', label: '项目名称', required: true, full: true },
        { key: 'code', label: '项目编码' },
        { key: 'customer_id', label: '所属客户', type: 'select', options: () => WB.meta.customers },
        { key: 'manager', label: '项目负责人' },
        { key: 'stage', label: '当前阶段' },
        { key: 'status', label: '状态', type: 'select', options: () => [{ id: '进行中', name: '进行中' }, { id: '已结束', name: '已结束' }], numeric: false },
        { key: 'start_date', label: '开始日期', type: 'date' },
        { key: 'end_date', label: '结束日期', type: 'date' },
        { key: 'remark', label: '项目说明', type: 'textarea', full: true, rows: 2 },
      ],
      cols: ['项目名称', '编码', '客户', '负责人', '阶段', '状态'],
      row: (p) => [
        U.esc(p.name), `<span class="mono muted">${U.esc(p.code || '—')}</span>`,
        U.esc(p.customer_name || '—'), U.esc(p.manager || '—'),
        `<span class="chip">${U.esc(p.stage || '—')}</span>`, U.badge(p.status),
      ],
    },
    employee: {
      res: 'employees', title: '员工', label: 'name',
      fields: [
        { key: 'name', label: '姓名', required: true },
        { key: 'employee_no', label: '工号', required: true },
        { key: 'department_id', label: '所属部门', type: 'select', options: () => WB.meta.departments, required: true },
        { key: 'position', label: '职位' },
        { key: 'level', label: '职级' },
        { key: 'gender', label: '性别', type: 'select', options: () => [{ id: '男', name: '男' }, { id: '女', name: '女' }], numeric: false },
        { key: 'birthday', label: '生日', type: 'date' },
        { key: 'hire_date', label: '入职日期', type: 'date' },
        { key: 'resign_date', label: '离职日期', type: 'date' },
        { key: 'email', label: '邮箱' },
        { key: 'phone', label: '手机号' },
        { key: 'id_card', label: '身份证号', hint: '列表显示为打码值，详情 admin 可查完整' },
        { key: 'bank_account', label: '收款账号' },
        { key: 'address', label: '通讯地址', type: 'textarea', full: true, rows: 2 },
        { key: 'emergency_contact', label: '紧急联系人', full: true, hint: '格式：姓名 / 电话' },
        { key: 'active', label: '在职', type: 'checkbox', hint: '在职（可选为申请人）' },
      ],
      cols: ['姓名', '工号', '部门', '职位', '职级', '手机号', '状态'],
      row: (e) => [
        U.esc(e.name), `<span class="mono muted">${U.esc(e.employee_no)}</span>`,
        U.esc(e.department_name || '—'), U.esc(e.position || '—'), U.esc(e.level || '—'),
        U.esc(e.phone || '—'), e.active ? '<span class="badge b-green">在职</span>' : '<span class="badge b-gray">离职</span>',
      ],
    },
  };

  async function tabBase(host) {
    const conf = BASE_CONF[state.base];
    const data = await api.list(conf.res);
    const fields = conf.fields.map((f) => (f.options ? { ...f, options: f.options() } : f));
    const w = canWrite();

    host.innerHTML = `
      <div class="toolbar">
        <div class="tabs" style="border:none;padding:0;background:transparent">
          ${Object.keys(BASE_CONF)
            .map((k) => `<div class="tab ${state.base === k ? 'active' : ''}" data-base="${k}">${BASE_CONF[k].title}</div>`)
            .join('')}
        </div>
        <span class="muted" style="font-size:12.5px">共 ${data.length} 条</span>
        <div class="spacer"></div>
        ${w ? `<button class="btn btn-sm btn-primary" id="bs-new">+ 新增${conf.title}</button>` : ''}
      </div>
      ${w ? batchBarHtml(`条${conf.title}`, '已被业务数据引用的会自动跳过') : ''}
      <div class="table-wrap">
        <table class="tbl">
          <thead><tr>${w ? '<th class="ck-col"><input type="checkbox" id="ck-all"></th>' : ''}${conf.cols.map((c) => `<th>${U.esc(c)}</th>`).join('')}<th style="width:1%">操作</th></tr></thead>
          <tbody>
            ${data
              .map(
                (o) => `<tr data-id="${o.id}">
              ${w ? `<td class="ck-col"><input type="checkbox" class="ck-row" value="${o.id}"></td>` : ''}
              ${conf.row(o).map((cell) => `<td>${cell}</td>`).join('')}
              <td>${w
                  ? `<div class="row-actions">
                <button class="btn btn-xs" data-act="edit">编辑</button>
                <button class="btn btn-xs btn-danger" data-act="del">删除</button>
              </div>`
                  : RO_ACTIONS}</td>
            </tr>`
              )
              .join('')}
          </tbody>
        </table>
      </div>`;

    host.querySelector('.tabs').addEventListener('click', (e) => {
      const t = e.target.closest('[data-base]');
      if (!t) return;
      state.base = t.dataset.base;
      WB.rerender();
    });

    if (w) {
      wireBatch(host, {
        res: conf.res, unit: `条${conf.title}`, reload: () => WB.rerender(),
        message: (n) => `即将删除 <b>${n}</b> 条${conf.title}记录。`,
      });
      U.qs('#bs-new', host).onclick = () =>
        entityForm({
          title: `新增${conf.title}`,
          fields,
          initial: state.base === 'employee' ? { active: true } : state.base === 'project' ? { status: '进行中' } : {},
          onSubmit: async (p) => {
            await api.create(conf.res, p);
            U.toast('已新增', 'success');
            await WB.refreshMeta();
            WB.rerender();
          },
        });
    }

    host.addEventListener('click', async (e) => {
      const b = e.target.closest('button[data-act]');
      if (!b) return;
      const id = Number(b.closest('tr').dataset.id);
      const row = data.find((x) => x.id === id);
      if (b.dataset.act === 'edit') {
        entityForm({
          title: `编辑${conf.title} · ${row[conf.label]}`,
          fields,
          initial: row,
          onSubmit: async (p) => {
            await api.update(conf.res, id, p);
            U.toast('已保存', 'success');
            await WB.refreshMeta();
            WB.rerender();
          },
        });
      } else {
        const ok = await U.confirm(`确认删除 <b>${U.esc(row[conf.label])}</b>？<span class="muted">被业务数据引用的记录会被系统拒绝删除。</span>`, {
          title: `删除${conf.title}`, okText: '删除', danger: true,
        });
        if (!ok) return;
        await api.remove(conf.res, id);
        U.toast('已删除', 'success');
        await WB.refreshMeta();
        WB.rerender();
      }
    });

    return { count: data.length };
  }

  /* ---------------- 主视图 ---------------- */
  WB.views.expenses = async function (root) {
    const meta = WB.meta;
    const TABS = [
      ['category', '费用类型'],
      ['budget', '费用预算'],
      ['items', '费用明细台账'],
      ['base', '基础数据'],
    ].filter(([k]) => tabAllowed(k));

    // 权限变小后当前页签可能已不可见，回落到第一个可见页签
    if (!TABS.some(([k]) => k === state.tab)) state.tab = TABS[0][0];

    root.innerHTML = `
      <div class="card">
        <div class="tabs" id="ex-tabs">
          ${TABS.map(([k, n]) => `<div class="tab ${state.tab === k ? 'active' : ''}" data-tab="${k}">${n}</div>`).join('')}
        </div>
        <div id="ex-body"></div>
      </div>`;

    const host = U.qs('#ex-body', root);
    async function load() {
      host.innerHTML = `<div style="padding:40px"><div class="empty"><span class="spin"></span>加载中…</div></div>`;
      if (state.tab === 'category') await tabCategory(host, meta);
      else if (state.tab === 'budget') await tabBudget(host, meta);
      else if (state.tab === 'items') await tabItems(host, meta);
      else await tabBase(host);
    }

    U.qs('#ex-tabs', root).addEventListener('click', (e) => {
      const t = e.target.closest('.tab');
      if (!t) return;
      state.tab = t.dataset.tab;
      U.qsa('.tab', root).forEach((x) => x.classList.toggle('active', x === t));
      load();
    });

    await load();

    return {
      title: '费用管理',
      sub: '费用类型标准、预算编制与费用明细台账',
      toolbar: `
        <span class="muted" style="font-size:12.5px">费用类型用于定义报销口径与限额；预算用于控制部门/项目/科目的费用额度。</span>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="ex-refresh">刷新</button>
        ${canWrite() ? '<button class="btn btn-sm" id="ex-add">+ 新增</button>' : ''}`,
      onToolbar(tb) {
        U.qs('#ex-refresh', tb).onclick = () => WB.rerender();
        const add = U.qs('#ex-add', tb);
        if (add) add.onclick = () => {
          const b = U.qs('#ex-body [id$="-new"]');
          if (b) b.click();
          else U.toast('请先切换到可新增的子页签', 'warn');
        };
      },
    };
  };
})();
