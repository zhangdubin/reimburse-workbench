/* 视图：异常与预警 */
window.WB = window.WB || {};
WB.views = WB.views || {};

(function () {
  const U = WB.util;
  const api = WB.api;

  const state = { tab: 'over_limit', large_amount: 20000, overdue_days: 7 };

  WB.views.alerts = async function (root) {
    let data;

    async function fetchData() {
      data = await api.alerts({
        large_amount: state.large_amount || 20000,
        overdue_days: state.overdue_days || 7,
      });
    }

    const TABS = [
      ['over_limit', '超限额明细', 'over_limit_count'],
      ['missing_invoice', '缺发票明细', 'missing_invoice_count'],
      ['bad_invoices', '问题发票', 'bad_invoice_count'],
      ['large_amount', '大额报销单', 'large_amount_count'],
      ['overdue', '超期未审批', 'overdue_count'],
    ];

    function kpiHtml(s) {
      const cards = [
        { cls: 'k-orange', label: '超限额明细', value: s.over_limit_count, sub: '单笔金额超过费用类型限额' },
        { cls: 'k-orange', label: '缺发票明细', value: s.missing_invoice_count, sub: '需发票但未关联任何发票' },
        { cls: 'k-red', label: '问题发票', value: s.bad_invoice_count, sub: '重复号码或校验不通过' },
        { cls: 'k-purple', label: '大额报销单', value: s.large_amount_count, sub: `金额 ≥ ${U.money(state.large_amount)}` },
        { cls: 'k-red', label: '超期未审批', value: s.overdue_count, sub: `提交超过 ${state.overdue_days} 天仍未审批` },
      ];
      return cards
        .map(
          (c) => `<div class="kpi ${c.cls}">
        <div class="kpi-label">${c.label}</div>
        <div class="kpi-value">${U.num(c.value)}</div>
        <div class="kpi-sub">${c.sub}</div></div>`
        )
        .join('');
    }

    const emptyRow = (n, t) => U.emptyRow(n, t);

    function renderTab() {
      const d = data;
      if (state.tab === 'over_limit') {
        return d.over_limit.length
          ? `<table class="tbl">
          <thead><tr><th>报销单号</th><th>费用类型</th><th>申请人</th><th>发生日期</th><th class="num">限额</th><th class="num">实际金额</th><th class="num">超出</th></tr></thead>
          <tbody>${d.over_limit
            .map(
              (x) => `<tr class="clickable" data-rid="${x.reimbursement_id}">
            <td class="mono strong">${U.esc(x.reimbursement_code)}</td>
            <td>${U.esc(x.category)}</td>
            <td>${U.esc(x.applicant || '—')}</td>
            <td class="nowrap">${U.date(x.occur_date)}</td>
            <td class="num muted">${U.money(x.limit)}</td>
            <td class="num amount">${U.money(x.amount)}</td>
            <td class="num" style="color:#f53f3f;font-weight:600">+${U.money(x.excess)}</td>
          </tr>`
            )
            .join('')}</tbody></table>`
          : emptyRow(7, '没有超出限额的费用明细');
      }
      if (state.tab === 'missing_invoice') {
        return d.missing_invoice.length
          ? `<table class="tbl">
          <thead><tr><th>报销单号</th><th>费用类型</th><th>摘要</th><th>发生日期</th><th class="num">金额</th><th>发票</th></tr></thead>
          <tbody>${d.missing_invoice
            .map(
              (x) => `<tr class="clickable" data-rid="${x.reimbursement_id}">
            <td class="mono strong">${U.esc(x.reimbursement_code)}</td>
            <td>${U.esc(x.category)}</td>
            <td class="muted ellipsis">${U.esc(x.description || '—')}</td>
            <td class="nowrap">${U.date(x.occur_date)}</td>
            <td class="num amount">${U.money(x.amount)}</td>
            <td><span class="badge b-red">缺票</span></td>
          </tr>`
            )
            .join('')}</tbody></table>`
          : emptyRow(6, '所有应附票的费用明细都已关联发票');
      }
      if (state.tab === 'bad_invoices') {
        return d.bad_invoices.length
          ? `<table class="tbl">
          <thead><tr><th>发票号码</th><th>发票代码</th><th>销售方</th><th class="num">金额</th><th>关联单号</th><th>校验结果</th></tr></thead>
          <tbody>${d.bad_invoices
            .map(
              (v) => `<tr>
            <td class="mono strong">${U.esc(v.invoice_no)}</td>
            <td class="mono muted">${U.esc(v.invoice_code || '—')}</td>
            <td class="ellipsis">${U.esc(v.seller_name || '—')}</td>
            <td class="num amount">${U.money(v.amount)}</td>
            <td>${v.reimbursement_code ? `<span class="chip">${U.esc(v.reimbursement_code)}</span>` : '<span class="muted">未关联</span>'}</td>
            <td style="color:#f53f3f">${U.esc(v.check_result || '校验异常')}</td>
          </tr>`
            )
            .join('')}</tbody></table>`
          : emptyRow(6, '未发现校验异常的发票');
      }
      if (state.tab === 'large_amount') {
        return d.large_amount.length
          ? `<table class="tbl">
          <thead><tr><th>单号</th><th>标题</th><th>申请人 / 部门</th><th>发生日期</th><th class="num">金额</th><th>状态</th></tr></thead>
          <tbody>${d.large_amount
            .map(
              (r) => `<tr class="clickable" data-rid="${r.id}">
            <td class="mono strong">${U.esc(r.code)}</td>
            <td class="ellipsis" title="${U.esc(r.title)}">${U.esc(r.title)}</td>
            <td><div>${U.esc(r.applicant || '—')}</div>${r.department ? `<div class="sub-line">${U.esc(r.department)}</div>` : ''}</td>
            <td class="nowrap">${U.date(r.occur_start)}</td>
            <td class="num amount">${U.money(r.amount)}</td>
            <td>${U.badge(r.status)}</td>
          </tr>`
            )
            .join('')}</tbody></table>`
          : emptyRow(6, '没有大额报销单');
      }
      return d.overdue.length
        ? `<table class="tbl">
        <thead><tr><th>单号</th><th>标题</th><th>申请人</th><th>提交时间</th><th class="num">已等待</th><th class="num">金额</th></tr></thead>
        <tbody>${d.overdue
          .map(
            (r) => `<tr class="clickable" data-rid="${r.id}">
          <td class="mono strong">${U.esc(r.code)}</td>
          <td class="ellipsis" title="${U.esc(r.title)}">${U.esc(r.title)}</td>
          <td>${U.esc(r.applicant || '—')}</td>
          <td class="nowrap">${U.datetime(r.submit_at)}</td>
          <td class="num" style="color:#f53f3f;font-weight:600">${r.days} 天</td>
          <td class="num amount">${U.money(r.amount)}</td>
        </tr>`
          )
          .join('')}</tbody></table>`
        : emptyRow(6, '没有超期未审批的单据');
    }

    function render() {
      root.innerHTML = `
        <div class="kpi-grid">${kpiHtml(data.summary)}</div>
        <div class="card">
          <div class="tabs" id="al-tabs">
            ${TABS.map(
              ([k, n, c]) =>
                `<div class="tab ${state.tab === k ? 'active' : ''}" data-tab="${k}">${n} <span class="cnt">${data.summary[c]}</span></div>`
            ).join('')}
          </div>
          <div class="table-wrap">${renderTab()}</div>
        </div>`;

      U.qs('#al-tabs', root).addEventListener('click', (e) => {
        const t = e.target.closest('.tab');
        if (!t) return;
        state.tab = t.dataset.tab;
        U.qsa('.tab', root).forEach((x) => x.classList.toggle('active', x === t));
        U.qs('.table-wrap', root).innerHTML = renderTab();
      });

      U.qs('.table-wrap', root).addEventListener('click', (e) => {
        const tr = e.target.closest('tr[data-rid]');
        if (!tr) return;
        WB.go('reimbursements', { focusId: Number(tr.dataset.rid) });
      });
    }

    await fetchData();
    render();

    return {
      title: '异常与预警',
      sub: '费用合规风险扫描',
      toolbar: `
        <div class="field"><label>大额单阈值</label>
          <input type="number" id="f-large" style="width:110px" value="${state.large_amount}"></div>
        <div class="field"><label>超期天数</label>
          <input type="number" id="f-days" style="width:80px" value="${state.overdue_days}"></div>
        <button class="btn btn-sm btn-primary" id="btn-scan">重新扫描</button>
        <div class="spacer"></div>
        <span class="muted" style="font-size:12px">扫描口径：单笔限额 / 发票缺失 / 重复发票 / 大额 / 超期</span>`,
      onToolbar(tb) {
        U.qs('#btn-scan', tb).onclick = async () => {
          state.large_amount = Number(U.qs('#f-large', tb).value) || 20000;
          state.overdue_days = Number(U.qs('#f-days', tb).value) || 7;
          await fetchData();
          render();
          U.toast('扫描完成', 'success');
        };
      },
    };
  };
})();
