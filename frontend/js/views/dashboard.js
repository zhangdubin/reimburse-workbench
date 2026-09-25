/* 视图：统计看板 */
window.WB = window.WB || {};
WB.views = WB.views || {};

(function () {
  const U = WB.util;
  const api = WB.api;
  const C = WB.charts;

  const state = { range: '6', department_id: '' };

  function rangeDates(key) {
    const now = new Date();
    const y = now.getFullYear();
    const pad = (n) => String(n).padStart(2, '0');
    if (key === 'all') return { from: null, to: null, months: 12 };
    if (key === 'year') return { from: `${y}-01-01`, to: null, months: now.getMonth() + 1 };
    const n = Number(key);
    const d = new Date(y, now.getMonth() - (n - 1), 1);
    return { from: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-01`, to: null, months: n };
  }

  function kpiHtml(o) {
    const paidRatio = o.total_amount ? ((o.paid_amount / o.total_amount) * 100).toFixed(1) : 0;
    const mom =
      o.mom === null || o.mom === undefined
        ? '<span class="muted">无同期数据</span>'
        : `<span class="${o.mom >= 0 ? 'up' : 'down'}" title="${U.esc(o.mom_basis || '')}">${o.mom >= 0 ? '▲' : '▼'} ${Math.abs(o.mom)}%</span> 环比上月同期`;
    const cards = [
      { cls: '', label: '报销总额', value: U.moneyShort(o.total_amount), sub: `共 ${U.num(o.total_count)} 单 · 均单 ${U.moneyShort(o.avg_amount)}` },
      { cls: 'k-green', label: '已付款金额', value: U.moneyShort(o.paid_amount), sub: `占总额 ${paidRatio}%` },
      {
        cls: 'k-orange', label: '待审批金额', value: U.moneyShort(o.pending_amount),
        sub: `${o.pending_count} 单待审${o.overdue_count ? ` · <span class="up">${o.overdue_count} 单超7天</span>` : ''}`,
      },
      { cls: 'k-purple', label: '本月费用', value: U.moneyShort(o.month_amount), sub: `${mom} · 本月 ${o.month_count} 单` },      { cls: 'k-blue', label: '发票金额', value: U.moneyShort(o.invoice_amount), sub: `已登记 ${U.num(o.invoice_count)} 张` },
      { cls: '', label: '平均单笔', value: U.moneyShort(o.avg_amount), sub: '单均报销金额' },
    ];
    return cards
      .map(
        (c) => `<div class="kpi ${c.cls}">
          <div class="kpi-label">${c.label}</div>
          <div class="kpi-value">${c.value}</div>
          <div class="kpi-sub">${c.sub}</div>
        </div>`
      )
      .join('');
  }

  WB.views.dashboard = async function (root) {
    const meta = WB.meta;
    const r = rangeDates(state.range);
    const params = { date_from: r.from, date_to: r.to, department_id: state.department_id || null };

    root.innerHTML = `
      <div class="kpi-grid" id="kpi"></div>
      <div class="chart-grid g2">
        <div class="card">
          <div class="card-head"><h3>费用走势</h3><span class="hint">按月归集费用发生额 · 虚线柱为本月进行中</span>
            <div class="right"><span class="chip" id="trend-chip"></span></div>
          </div>
          <div class="card-body"><div class="chart lg" id="c-trend"></div></div>
        </div>
        <div class="card">
          <div class="card-head"><h3>费用大类构成</h3><span class="hint">一级费用科目</span></div>
          <div class="card-body"><div class="chart lg" id="c-group"></div></div>
        </div>
      </div>
      <div class="chart-grid g2">
        <div class="card">
          <div class="card-head"><h3>费用类型占比</h3><span class="hint">Top 12</span></div>
          <div class="card-body"><div class="chart" id="c-cat"></div></div>
        </div>
        <div class="card">
          <div class="card-head"><h3>部门月度费用</h3><span class="hint">近 6 个月堆叠</span></div>
          <div class="card-body"><div class="chart" id="c-deptmonth"></div></div>
        </div>
      </div>
      <div class="chart-grid g3">
        <div class="card">
          <div class="card-head"><h3>单据状态分布</h3></div>
          <div class="card-body"><div class="chart sm" id="c-status"></div></div>
        </div>
        <div class="card">
          <div class="card-head"><h3>部门费用排名</h3></div>
          <div class="card-body"><div class="chart sm" id="c-dept"></div></div>
        </div>
        <div class="card">
          <div class="card-head"><h3>人员费用排名</h3><span class="hint">Top 10</span></div>
          <div class="card-body"><div class="chart sm" id="c-emp"></div></div>
        </div>
      </div>
      <div class="chart-grid g2">
        <div class="card">
          <div class="card-head"><h3>客户费用排名</h3><span class="hint">Top 10</span></div>
          <div class="card-body"><div class="chart" id="c-cust"></div></div>
        </div>
        <div class="card">
          <div class="card-head"><h3>项目费用排名</h3><span class="hint">Top 10</span></div>
          <div class="card-body"><div class="chart" id="c-proj"></div></div>
        </div>
      </div>`;

    const [ov, trend, groups, cats, monthDept, depts, emps, custs, projs] = await Promise.all([
      api.overview(params),
      api.trend({ months: r.months, department_id: state.department_id || null }),
      api.byGroup(params),
      api.byCategory(params),
      api.byMonthDept({ months: 6 }),
      api.byDepartment(params),
      api.byEmployee(Object.assign({ limit: 10 }, params)),
      api.byCustomer(Object.assign({ limit: 10 }, params)),
      api.byProject(Object.assign({ limit: 10 }, params)),
    ]);

    U.qs('#kpi', root).innerHTML = kpiHtml(ov);
    U.qs('#trend-chip', root).textContent = `已批 ${U.moneyShort(
      trend.reduce((s, x) => s + x.approved_amount, 0)
    )}`;

    C.trend(U.qs('#c-trend', root), trend);
    C.groupBar(U.qs('#c-group', root), groups);
    C.donut(U.qs('#c-cat', root), cats.slice(0, 12));
    C.stacked(U.qs('#c-deptmonth', root), monthDept.months, monthDept.series);
    C.ring(U.qs('#c-status', root), ov.status_dist, WB.statusColor);
    C.hbar(U.qs('#c-dept', root), depts, { topN: 8 });
    C.hbar(U.qs('#c-emp', root), emps, { topN: 10, color: '#00b42a' });
    C.hbar(U.qs('#c-cust', root), custs, { topN: 10, color: '#ff7d00' });
    C.hbar(U.qs('#c-proj', root), projs, { topN: 10, color: '#722ed1' });

    return {
      title: '统计看板',
      sub: '销售费用全景视图',
      toolbar: `
        <div class="field"><label>时间范围</label>
          <select id="f-range" class="f-range">
            <option value="3" ${state.range === '3' ? 'selected' : ''}>近 3 个月</option>
            <option value="6" ${state.range === '6' ? 'selected' : ''}>近 6 个月</option>
            <option value="12" ${state.range === '12' ? 'selected' : ''}>近 12 个月</option>
            <option value="year" ${state.range === 'year' ? 'selected' : ''}>本年度</option>
            <option value="all" ${state.range === 'all' ? 'selected' : ''}>全部</option>
          </select>
        </div>
        <div class="field"><label>部门</label>
          <select id="f-dept" class="f-dept">${U.options(meta.departments, { selected: state.department_id, placeholder: '全部部门' })}</select>
        </div>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="btn-refresh">刷新</button>`,
      onToolbar(tb, rerender) {
        U.qs('#f-range', tb).onchange = (e) => {
          state.range = e.target.value;
          rerender();
        };
        U.qs('#f-dept', tb).onchange = (e) => {
          state.department_id = e.target.value;
          rerender();
        };
        U.qs('#btn-refresh', tb).onclick = rerender;
      },
    };
  };
})();
