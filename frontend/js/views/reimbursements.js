/* 视图：报销单管理 */
window.WB = window.WB || {};
WB.views = WB.views || {};

(function () {
  const U = WB.util;
  const api = WB.api;

  const state = {
    q: '', status: '', department_id: '', applicant_id: '',
    date_from: '', date_to: '', amount_min: '', amount_max: '',
    sort: 'id_desc', page: 1, page_size: 20,
  };

  const FILTER_KEYS = ['q', 'status', 'department_id', 'applicant_id', 'date_from', 'date_to', 'amount_min', 'amount_max', 'sort'];

  function queryParams(withPage = true) {
    const p = {};
    FILTER_KEYS.forEach((k) => (p[k] = state[k]));
    if (withPage) {
      p.page = state.page;
      p.page_size = state.page_size;
    }
    return p;
  }

  function actionsFor(row) {
    const a = [];
    const canApprove = WB.can('reimb.approve');
    const canPay = WB.can('reimb.pay');
    if (row.status === '草稿' || row.status === '已驳回') {
      a.push(['edit', '编辑', 'btn']);
      a.push(['submit', '提交', 'btn btn-primary']);
    }
    if (row.status === '待审批') {
      // 审批动作只对审批人/管理员开放；申请人自己只保留撤回
      if (canApprove) {
        a.push(['approve', '通过', 'btn btn-success']);
        a.push(['reject', '驳回', 'btn btn-danger']);
      }
      a.push(['withdraw', '撤回', 'btn']);
    }
    if (row.status === '已通过' && canPay) a.push(['pay', '付款', 'btn btn-primary']);
    if (row.status === '已付款' && canPay) a.push(['unpay', '撤销付款', 'btn']);
    a.push(['detail', '查看', 'btn btn-ghost']);
    return a;
  }

  /* ---------------- 明细编辑器 ---------------- */
  function itemEditor(container, cats, items) {
    const rows = items.length ? items : [blank()];
    function blank() {
      return { id: null, category_id: cats[0] ? cats[0].id : null, occur_date: U.today(), amount: '', tax_amount: '', description: '' };
    }

    function draw() {
      container.innerHTML = `
        <div class="items-editor">
          <table>
            <thead><tr>
              <th style="width:23%">费用类型</th>
              <th style="width:14%">发生日期</th>
              <th style="width:14%">金额(元)</th>
              <th style="width:12%">税额(元)</th>
              <th>摘要</th>
              <th style="width:36px"></th>
            </tr></thead>
            <tbody>
              ${rows
                .map(
                  (it, i) => `<tr data-i="${i}">
                <td><select class="it-cat">${U.options(cats, { selected: it.category_id })}</select></td>
                <td><input type="date" class="it-date" value="${U.esc(it.occur_date || '')}"></td>
                <td><input type="number" step="0.01" min="0" class="it-amt num" value="${it.amount === '' || it.amount == null ? '' : it.amount}"></td>
                <td><input type="number" step="0.01" min="0" class="it-tax" value="${it.tax_amount === '' || it.tax_amount == null ? '' : it.tax_amount}"></td>
                <td><input type="text" class="it-desc" value="${U.esc(it.description || '')}" placeholder="费用说明"></td>
                <td><button type="button" class="del-row" data-del="${i}" title="删除该行">&times;</button></td>
              </tr>`
                )
                .join('')}
            </tbody>
            <tfoot><tr>
              <td colspan="2"><button type="button" class="btn btn-sm" data-add>+ 添加明细行</button></td>
              <td class="num strong" id="it-total">¥0.00</td>
              <td colspan="3" class="muted">合计金额</td>
            </tr></tfoot>
          </table>
        </div>`;

      U.qs('[data-add]', container).onclick = () => {
        collect();
        rows.push(blank());
        draw();
      };
      U.qsa('[data-del]', container).forEach((b) => {
        b.onclick = () => {
          collect();
          rows.splice(Number(b.dataset.del), 1);
          if (!rows.length) rows.push(blank());
          draw();
        };
      });
      U.qsa('.it-amt', container).forEach((el) => (el.oninput = refreshTotal));
      refreshTotal();
    }

    function collect() {
      U.qsa('tbody tr', container).forEach((tr, i) => {
        const g = (sel) => U.qs(sel, tr);
        rows[i] = {
          id: rows[i] ? rows[i].id : null,
          category_id: g('.it-cat').value ? Number(g('.it-cat').value) : null,
          occur_date: g('.it-date').value || null,
          amount: g('.it-amt').value === '' ? 0 : Number(g('.it-amt').value),
          tax_amount: g('.it-tax').value === '' ? 0 : Number(g('.it-tax').value),
          description: g('.it-desc').value || null,
        };
      });
      return rows;
    }

    function refreshTotal() {
      const sum = U.qsa('.it-amt', container).reduce((s, el) => s + (Number(el.value) || 0), 0);
      const t = U.qs('#it-total', container);
      if (t) t.textContent = U.money(sum);
    }

    draw();
    return {
      get() {
        return collect().filter((x) => Number(x.amount) > 0);
      },
    };
  }

  /* ---------------- 弹窗 ---------------- */
  function openEditor(row, meta, onDone) {
    const isNew = !row;
    const empOptions = meta.employees.filter((e) => e.active);
    // 新建时默认把申请人选成登录账号绑定的员工——绝大多数人报的就是自己的账，
    // 默认选上省一步；下拉仍可改成他人（财务/管理员代建单的场景不受影响）。
    // 账号没绑员工的，退回「请选择」，不猜。
    const me = (WB.auth && WB.auth.user) || {};
    const myEmp = me.employee_id
      ? empOptions.find((e) => String(e.id) === String(me.employee_id))
      : null;
    const d = row || {
      title: '', applicant_id: myEmp ? myEmp.id : null,
      department_id: myEmp ? (myEmp.department_id || null) : null,
      customer_id: null,
      project_id: null, purpose: '', remark: '', items: [],
    };
    const m = U.modal({
      title: isNew ? '新建报销单' + (d.code ? '' : '') : `编辑报销单 ${d.code}`,
      width: 880,
      body: `
        <div class="form-section">基本信息</div>
        <div class="form-grid">
          <div class="form-item full">
            <label>报销标题<span class="req">*</span></label>
            <input id="f-title" value="${U.esc(d.title)}" placeholder="例：前海数字云平台项目差旅费用报销">
          </div>
          <div class="form-item"><label>申请人<span class="req">*</span></label>
            <select id="f-applicant">${U.options(empOptions, { selected: d.applicant_id, placeholder: '请选择' })}</select></div>
          <div class="form-item"><label>所属部门</label>
            <select id="f-form-dept">${U.options(meta.departments, { selected: d.department_id, placeholder: '请选择' })}</select></div>
          <div class="form-item"><label>关联客户</label>
            <select id="f-cust">${U.options(meta.customers, { selected: d.customer_id, placeholder: '不关联' })}</select></div>
          <div class="form-item"><label>关联项目</label>
            <select id="f-proj">${U.options(meta.projects, { selected: d.project_id, placeholder: '不关联' })}</select></div>
          <div class="form-item full"><label>报销事由</label>
            <input id="f-purpose" value="${U.esc(d.purpose || '')}" placeholder="简述业务背景"></div>
          <div class="form-item full"><label>备注</label>
            <input id="f-remark" value="${U.esc(d.remark || '')}"></div>
        </div>
        <div class="form-section" style="margin-top:16px">费用明细<span class="muted" style="font-weight:400;font-size:11.5px"> · 金额为 0 的行不会被保存</span></div>
        <div id="items-host"></div>`,
      footer: `<button class="btn" data-close>取消</button>
               <button class="btn btn-primary" data-save>${isNew ? '创建' : '保存'}</button>`,
      onMount(apiMod) {
        const editor = itemEditor(U.qs('#items-host', apiMod.el), meta.categories, (d.items || []).map((x) => ({ ...x })));

        U.qs('#f-applicant', apiMod.el).onchange = (e) => {
          const emp = meta.employees.find((x) => String(x.id) === e.target.value);
          if (emp && emp.department_id) U.qs('#f-form-dept', apiMod.el).value = emp.department_id;
        };
        U.qs('#f-proj', apiMod.el).onchange = (e) => {
          const p = meta.projects.find((x) => String(x.id) === e.target.value);
          if (p && p.customer_id) U.qs('#f-cust', apiMod.el).value = p.customer_id;
        };

        U.qs('[data-save]', apiMod.el).onclick = async () => {
          const el = apiMod.el;
          const payload = {
            title: U.qs('#f-title', el).value.trim(),
            applicant_id: U.qs('#f-applicant', el).value ? Number(U.qs('#f-applicant', el).value) : null,
            department_id: U.qs('#f-form-dept', el).value ? Number(U.qs('#f-form-dept', el).value) : null,
            customer_id: U.qs('#f-cust', el).value ? Number(U.qs('#f-cust', el).value) : null,
            project_id: U.qs('#f-proj', el).value ? Number(U.qs('#f-proj', el).value) : null,
            purpose: U.qs('#f-purpose', el).value || null,
            remark: U.qs('#f-remark', el).value || null,
            items: editor.get(),
          };
          if (!payload.title) return U.toast('请填写报销标题', 'warn');
          if (!payload.applicant_id) return U.toast('请选择申请人', 'warn');
          if (!payload.items.length) return U.toast('请至少填写一条有效费用明细', 'warn');
          try {
            if (isNew) await api.createReimbursement(payload);
            else await api.updateReimbursement(d.id, payload);
            U.toast(isNew ? '报销单已创建' : '已保存', 'success');
            apiMod.close();
            onDone();
          } catch (e) {}
        };
      },
    });
    return m;
  }

  /* ---------------- AI 体检（只读） ----------------
   * 分析结果**不会**自动改变单据状态，也不写库；审批意见只是参考，
   * 最终由人点「审批通过 / 驳回」。所以这里连按钮都不复用 data-act。
   */
  const LEVEL_TONE = { 高: 'b-red', 中: 'b-orange', 低: 'b-green' };

  function bindAiInsight(scope, d) {
    const box = U.qs('#ai-insight-body', scope);
    if (!box) return;

    const loading = (text) => {
      box.innerHTML = `<div class="empty" style="padding:16px"><span class="spin"></span>${U.esc(text)}</div>`;
    };
    const fail = (e) => {
      box.innerHTML = `<div class="ai-insight-err">${U.esc(e.message || e)}</div>`;
      U.toast(e.message || 'AI 分析失败', 'error');
    };
    const list = (title, arr) =>
      arr && arr.length
        ? `<div class="ai-insight-block"><b>${U.esc(title)}</b><ul>${arr
            .map((x) => `<li>${U.esc(x)}</li>`)
            .join('')}</ul></div>`
        : '';

    const anBtn = U.qs('#ai-an', scope);
    if (anBtn) {
      anBtn.onclick = async () => {
        anBtn.disabled = true;
        loading('正在分析这张单子…');
        try {
          const r = await api.aiAnalyze(d.id);
          box.innerHTML = `
            <div class="ai-insight-row">
              <span class="badge ${LEVEL_TONE[r.level] || 'b-gray'}">风险 ${U.esc(r.level || '—')}</span>
              <span class="muted">模型 ${U.esc(r.model || '-')}</span>
            </div>
            <p class="ai-insight-sum">${U.esc(r.summary || '')}</p>
            ${list('发现的问题', r.findings)}
            ${list('建议动作', r.suggestions)}`;
        } catch (e) {
          fail(e);
        } finally {
          anBtn.disabled = false;
        }
      };
    }

    const apBtn = U.qs('#ai-ap', scope);
    if (apBtn) {
      apBtn.onclick = async () => {
        apBtn.disabled = true;
        loading('正在生成参考意见…');
        try {
          const r = await api.aiApproval(d.id);
          const conf = Math.round((r.confidence || 0) * 100);
          box.innerHTML = `
            <div class="ai-insight-row">
              <span class="badge ${r.recommend === '通过' ? 'b-green' : r.recommend === '驳回' ? 'b-red' : 'b-orange'}">
                建议${U.esc(r.recommend || '—')}</span>
              <span class="muted">模型置信度 ${conf}%</span>
            </div>
            ${list('依据', r.reasons)}
            ${list('风险点', r.risks)}
            ${list('建议向申请人确认', r.questions)}
            <div class="ai-insight-note">${U.esc(r.disclaimer || '')}</div>`;
        } catch (e) {
          fail(e);
        } finally {
          apBtn.disabled = false;
        }
      };
    }
  }

  async function openDetail(id, meta, reload) {
    const d = await api.reimbursement(id);
    const canPay = d.status === '已通过' && WB.can('reimb.pay');
    const canApprove = d.status === '待审批' && WB.can('reimb.approve');
    const m = U.modal({
      title: `报销单 ${d.code}`,
      width: 900,
      body: `
        <div class="desc-grid rd-grid" style="margin-bottom:14px">
          <div class="desc-item rd-status"><dt>状态</dt><dd>${U.badge(d.status)}</dd></div>
          <div class="desc-item rd-amount"><dt>报销金额</dt><dd class="amount">${U.money(d.total_amount)}</dd></div>
          <div class="desc-item"><dt>申请人 / 部门</dt><dd>${U.esc(d.applicant_name || '—')} · ${U.esc(d.department_name || '—')}</dd></div>
          <div class="desc-item"><dt>客户</dt><dd>${U.esc(d.customer_name || '—')}</dd></div>
          <div class="desc-item"><dt>项目</dt><dd>${U.esc(d.project_name || '—')}</dd></div>
          <div class="desc-item"><dt>费用期间</dt><dd>${U.date(d.occur_start)} ~ ${U.date(d.occur_end)}</dd></div>
          <div class="desc-item"><dt>提交时间</dt><dd>${U.datetime(d.submit_at)}</dd></div>
          <div class="desc-item"><dt>审批时间</dt><dd>${U.datetime(d.approve_at)} · ${U.esc(d.approver || '—')}</dd></div>
          <div class="desc-item"><dt>付款时间</dt><dd>${U.datetime(d.pay_at)}</dd></div>
          <div class="desc-item"><dt>关联发票</dt><dd>${d.invoices.length} 张</dd></div>
          <div class="desc-item wide"><dt>标题</dt><dd>${U.esc(d.title)}</dd></div>
          <div class="desc-item wide"><dt>事由</dt><dd>${U.esc(d.purpose || '—')}</dd></div>
          ${d.reject_reason ? `<div class="desc-item wide"><dt>驳回原因</dt><dd style="color:#f53f3f">${U.esc(d.reject_reason)}</dd></div>` : ''}
          ${d.remark ? `<div class="desc-item wide"><dt>备注</dt><dd>${U.esc(d.remark)}</dd></div>` : ''}
        </div>

        <div class="ai-insight" id="ai-insight">
          <div class="ai-insight-head">
            <b>AI 体检</b>
            <span class="muted">只读分析，不会改动这张单子</span>
            <span class="spacer"></span>
            <button class="btn btn-xs" id="ai-an">风险分析</button>
            ${canApprove ? '<button class="btn btn-xs btn-primary" id="ai-ap">审批参考意见</button>' : ''}
          </div>
          <div class="ai-insight-body" id="ai-insight-body">
            <span class="ai-empty-hint">点上面的按钮，让模型结合费用明细、发票与系统已算出的异常提示读一遍。</span>
          </div>
        </div>

        <div class="form-section">费用明细（${d.items.length} 条）</div>
        <div class="table-wrap" style="border:1px solid var(--border);border-radius:8px;overflow:hidden">
          <table class="tbl">
            <thead><tr><th>费用类型</th><th>发生日期</th><th>摘要</th><th class="num">税额</th><th class="num">金额</th><th class="num">发票</th></tr></thead>
            <tbody>
              ${d.items
                .map(
                  (i) => `<tr>
                <td>${U.esc(i.category_name || '未分类')}</td>
                <td class="nowrap">${U.date(i.occur_date)}</td>
                <td class="muted">${U.esc(i.description || '—')}</td>
                <td class="num muted">${U.money(i.tax_amount)}</td>
                <td class="num amount">${U.money(i.amount)}</td>
                <td class="num">${i.invoice_count ? `<span class="badge b-green">${i.invoice_count}</span>` : '<span class="badge b-red">缺</span>'}</td>
              </tr>`
                )
                .join('') || '<tr><td colspan="6"><div class="empty">无明细</div></td></tr>'}
            </tbody>
            <tfoot><tr><td colspan="4">合计</td><td class="num amount">${U.money(d.total_amount)}</td><td></td></tr></tfoot>
          </table>
        </div>

        ${
          d.invoices.length
            ? `<div class="form-section" style="margin-top:16px">关联发票（${d.invoices.length} 张）</div>
        <div class="table-wrap" style="border:1px solid var(--border);border-radius:8px;overflow:hidden">
          <table class="tbl">
            <thead><tr><th>发票号码</th><th>类型</th><th>开票日期</th><th>销售方</th><th class="num">金额</th><th>查验</th></tr></thead>
            <tbody>${d.invoices
              .map(
                (v) => `<tr>
              <td class="mono">${U.esc(v.invoice_no)}</td>
              <td class="muted">${U.esc(v.invoice_type)}</td>
              <td class="nowrap">${U.date(v.invoice_date)}</td>
              <td class="ellipsis muted">${U.esc(v.seller_name || '—')}</td>
              <td class="num amount">${U.money(v.amount)}</td>
              <td>${U.badge(v.check_status)}</td>
            </tr>`
              )
              .join('')}</tbody>
          </table>
        </div>`
            : ''
        }

        <div class="form-section" style="margin-top:16px">流转记录</div>
        <div class="timeline">
          ${d.logs
            .map(
              (l) => `<div class="tl-item a-${l.action === '驳回' ? 'reject' : l.action === '通过' ? 'approve' : l.action === '付款' ? 'pay' : 'x'}">
            <div class="tl-head"><span>${U.esc(l.action)}</span>
              ${l.from_status ? `<span class="chip">${U.esc(l.from_status)} → ${U.esc(l.to_status)}</span>` : ''}
              <span class="tl-meta">${U.esc(l.operator || '系统')}</span>
              <span class="tl-meta" style="margin-left:auto">${U.datetime(l.created_at)}</span>
            </div>
            ${l.comment ? `<div class="tl-comment">${U.esc(l.comment)}</div>` : ''}
          </div>`
            )
            .join('')}
        </div>`,
      footer: `
        ${d.status === '已驳回' ? `<button class="btn" data-act="edit">编辑重提</button>` : ''}
        <button class="btn" data-act="print">打印</button>
        ${canApprove ? `<button class="btn btn-danger" data-act="reject">驳回</button>
          <button class="btn btn-success" data-act="approve">审批通过</button>` : ''}
        ${canPay ? `<button class="btn btn-primary" data-act="pay">确认付款</button>` : ''}
        ${d.status === '草稿' ? `<button class="btn btn-primary" data-act="submit">提交审批</button>` : ''}
        <button class="btn" data-close>关闭</button>`,
      onMount(apiMod) {
        bindAiInsight(apiMod.el, d);

        U.qsa('[data-act]', apiMod.el).forEach((b) => {
          b.onclick = async () => {
            const act = b.dataset.act;
            if (act === 'edit') {
              apiMod.close();
              return openEditor(d, meta, reload);
            }
            if (act === 'print') return WB.print.reimbursement(d);
            if (act === 'reject') {
              const comment = await askComment('驳回原因', '该笔费用超出部门月度预算，请补充审批说明');
              if (comment === null) return;
              await doAction(d.id, 'reject', { operator: WB.user, comment }, '已驳回');
            } else {
              await doAction(d.id, act, { operator: WB.user }, null);
            }
            apiMod.close();
            reload();
          };
        });
      },
    });
    return m;
  }

  function askComment(title, placeholder) {
    return new Promise((resolve) => {
      const m = U.modal({
        title,
        width: 480,
        body: `<textarea id="cmt" placeholder="${U.esc(placeholder || '')}"></textarea>`,
        footer: `<button class="btn" data-close>取消</button><button class="btn btn-primary" data-ok>确定</button>`,
        onMount(mm) {
          U.qs('[data-ok]', mm.el).onclick = () => {
            const v = U.qs('#cmt', mm.el).value.trim();
            if (!v) return U.toast('请填写内容', 'warn');
            resolve(v);
            mm.close();
          };
          mm.el.addEventListener('click', (e) => {
            if (e.target === mm.el) resolve(null);
          });
        },
      });
      const orig = m.close;
      m.close = function () {
        orig.call(m);
        setTimeout(() => resolve(null), 0);
      };
      setTimeout(() => U.qs('#cmt', m.el) && U.qs('#cmt', m.el).focus(), 60);
    });
  }

  async function doAction(id, act, body, okMsg) {
    try {
      await api.action(id, act, body);
      U.toast(okMsg || '操作成功', 'success');
    } catch (e) {}
  }

  /* ---------------- 主渲染 ---------------- */
  WB.views.reimbursements = async function (root) {
    const meta = WB.meta;
    const focus = WB.viewParams && WB.viewParams.focusId;
    WB.viewParams = null;

    root.innerHTML = `
      <div class="card">
        <div style="padding:12px 16px 0" id="batch-wrap"></div>
        <div class="table-wrap" id="host">${U.loading(9)}</div>
        <div id="pager-host"></div>
      </div>`;

    const host = U.qs('#host', root);
    const pagerHost = U.qs('#pager-host', root);
    const batchWrap = U.qs('#batch-wrap', root);

    batchWrap.innerHTML = `
      <div class="batch-bar" id="rb-bar" style="display:none">
        已选 <b id="rb-picked">0</b> 张报销单
        <span class="muted">只有草稿与已驳回的单据可删；待审批及以上会跳过并给出原因</span>
        <div class="spacer"></div>
        <button class="btn btn-sm btn-danger" id="rb-del">批量删除</button>
        ${
          WB.can('admin.users')
            ? '<button class="btn btn-sm btn-danger" id="rb-purge">清空全部报销单</button>'
            : ''
        }
      </div>`;

    function syncBatch() {
      const ids = U.pickedIds(host);
      U.qs('#rb-picked').textContent = ids.length;
      U.qs('#rb-bar').style.display = ids.length ? '' : 'none';
      U.qsa('tr[data-id]', host).forEach((tr) => {
        const c = U.qs('.ck-row', tr);
        tr.classList.toggle('picked', !!(c && c.checked));
      });
    }

    async function batchDelete() {
      const ids = U.pickedIds(host);
      if (!ids.length) return U.toast('请先勾选要删除的报销单', 'warn');
      const ok = await U.dangerConfirm(
        `即将删除 <b>${ids.length}</b> 张报销单。已关联的发票会自动解绑（发票本身保留），` +
          `不可删除的单据会跳过并说明原因。`,
        '删除报销单',
        { okText: `删除 ${ids.length} 张` }
      );
      if (!ok) return;
      const r = await api.batchDeleteReimbursements(ids);
      U.toast(U.batchResult(r, '张'), r.skipped_count ? 'warn' : 'success', 4600);
      if (r.skipped_count) U.toast(U.skipReasons(r), 'warn', 6000);
      await load();
    }

    async function purgeAll() {
      const ok = await U.dangerConfirm(
        '即将删除<b>全部报销单</b>（不限状态、不限部门），已关联发票会自动解绑。此操作不可恢复。',
        '清空全部报销单',
        { okText: '清空全部报销单' }
      );
      if (!ok) return;
      const r = await api.purgeReimbursements('清空全部报销单');
      U.toast(`已清空 ${r.deleted || 0} 张报销单`, 'success', 4200);
      await load();
    }

    async function load() {
      host.innerHTML = U.loading(9);
      const data = await api.reimbursements(queryParams());
      if (!data.items.length) {
        host.innerHTML = U.emptyRow(10, '没有符合条件的报销单');
      } else {
        host.innerHTML = `<table class="tbl">
          <thead><tr>
            <th class="ck-col"><input type="checkbox" id="ck-all"></th>
            <th>单号 / 标题</th><th>申请人</th><th>客户 / 项目</th><th>费用期间</th>
            <th class="num">金额</th><th class="num">发票</th><th>状态</th><th>提交时间</th><th style="width:1%">操作</th>
          </tr></thead>
          <tbody>
            ${data.items
              .map(
                (r) => `<tr class="clickable" data-id="${r.id}">
              <td class="ck-col"><input type="checkbox" class="ck-row" value="${r.id}"></td>
              <td class="clickable" data-open><div class="mono strong">${U.esc(r.code)}</div><div class="sub-line ellipsis" title="${U.esc(r.title)}">${U.esc(r.title)}</div></td>
              <td class="clickable" data-open><div>${U.esc(r.applicant_name || '—')}</div>${r.department_name ? `<div class="sub-line">${U.esc(r.department_name)}</div>` : ''}</td>
              <td class="clickable" data-open><div class="ellipsis" title="${U.esc(r.customer_name || '')}">${U.esc(r.customer_name || '—')}</div>${r.project_name ? `<div class="sub-line ellipsis" title="${U.esc(r.project_name)}">${U.esc(r.project_name)}</div>` : ''}</td>
              <td class="nowrap sub-line clickable" data-open>${U.date(r.occur_start)}<br>${U.date(r.occur_end)}</td>
              <td class="num amount clickable" data-open>${U.money(r.total_amount)}</td>
              <td class="num clickable" data-open>${r.invoice_count ? `<span class="badge b-blue">${r.invoice_count}</span>` : '<span class="muted">—</span>'}</td>
              <td class="clickable" data-open>${U.badge(r.status)}</td>
              <td class="sub-line nowrap clickable" data-open>${U.datetime(r.submit_at)}</td>
              <td><div class="row-actions">${actionsFor(r)
                .map(([act, label, cls]) => `<button class="${cls} btn-xs" data-act="${act}" data-id="${r.id}">${label}</button>`)
                .join('')}</div></td>            </tr>`
              )
              .join('')}
          </tbody>
        </table>`;
      }
      U.bindCheckAll(host);
      syncBatch();
      pagerHost.innerHTML = U.pager(data.total, data.page, data.page_size);
    }

    host.addEventListener('change', (e) => {
      if (e.target.classList.contains('ck-row') || e.target.id === 'ck-all') {
        if (e.target.id === 'ck-all') {
          U.qsa('.ck-row', host).forEach((c) => (c.checked = e.target.checked));
        }
        syncBatch();
      }
    });

    host.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-act]');
      if (btn) {
        e.stopPropagation();
        const id = Number(btn.dataset.id);
        const act = btn.dataset.act;
        if (act === 'detail') return openDetail(id, meta, load);
        if (act === 'edit') {
          const d = await api.reimbursement(id);
          return openEditor(d, meta, load);
        }
        if (act === 'reject') {
          const c = await askComment('驳回原因', '发票信息与明细金额不一致，请核对后重新提交');
          if (c === null) return;
          await doAction(id, 'reject', { operator: WB.user, comment: c }, '已驳回');
          return load();
        }
        const okText = { submit: '已提交审批', approve: '审批通过', withdraw: '已撤回到草稿', pay: '已完成付款', unpay: '已撤销付款' }[act];
        await doAction(id, act, { operator: WB.user, approver: act === 'approve' ? WB.user : undefined }, okText);
        return load();
      }
      // 勾选框不触发详情（批量按钮在卡片头部，另绑）
      if (e.target.closest('.ck-col')) return;
      const tr = e.target.closest('tr[data-id]');
      if (tr) openDetail(Number(tr.dataset.id), meta, load);
    });

    batchWrap.addEventListener('click', (e) => {
      if (e.target.closest('#rb-del')) return batchDelete();
      if (e.target.closest('#rb-purge')) return purgeAll();
    });

    pagerHost.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-page]');
      if (!b || b.disabled) return;
      state.page = Number(b.dataset.page);
      load();
      root.parentElement.scrollTop = 0;
    });

    await load();
    if (focus) openDetail(Number(focus), meta, load);

    return {
      title: '报销单管理',
      sub: '单据创建、明细维护与审批流转',
      toolbar: `
        <input class="search-input" id="f-q" placeholder="单号 / 标题 / 事由 / 申请人" value="${U.esc(state.q)}">
        <div class="field"><label>状态</label>
          <select id="f-status">
            <option value="">全部</option>
            ${['草稿', '待审批', '已通过', '已付款', '已驳回']
              .map((s) => `<option value="${s}" ${state.status === s ? 'selected' : ''}>${s}</option>`)
              .join('')}
          </select>
        </div>
        <div class="field"><label>部门</label>
          <select id="f-dept">${U.options(meta.departments, { selected: state.department_id, placeholder: '全部' })}</select>
        </div>
        <div class="field"><label>申请人</label>
          <select id="f-emp">${U.options(meta.employees, { selected: state.applicant_id, placeholder: '全部' })}</select>
        </div>
        <div class="field"><label>费用期间</label>
          <input type="date" id="f-from" value="${state.date_from}"> <span class="muted">~</span>
          <input type="date" id="f-to" value="${state.date_to}">
        </div>
        <div class="field"><label>金额</label>
          <input type="number" id="f-amin" style="width:84px" placeholder="最小" value="${state.amount_min}">
          <span class="muted">~</span>
          <input type="number" id="f-amax" style="width:84px" placeholder="最大" value="${state.amount_max}">
        </div>
        <div class="field"><label>排序</label>
          <select id="f-sort">
            <option value="id_desc" ${state.sort === 'id_desc' ? 'selected' : ''}>最近创建</option>
            <option value="amount_desc" ${state.sort === 'amount_desc' ? 'selected' : ''}>金额从高到低</option>
            <option value="amount_asc" ${state.sort === 'amount_asc' ? 'selected' : ''}>金额从低到高</option>
            <option value="date_desc" ${state.sort === 'date_desc' ? 'selected' : ''}>发生日期新→旧</option>
            <option value="date_asc" ${state.sort === 'date_asc' ? 'selected' : ''}>发生日期旧→新</option>
          </select>
        </div>
        <button class="btn btn-sm" id="f-reset">重置</button>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="btn-export">导出 CSV</button>
        <button class="btn btn-sm btn-primary" id="btn-new">+ 新建报销单</button>`,
      onToolbar(tb) {
        const reload = () => {
          state.page = 1;
          load();
        };
        U.qs('#f-q', tb).oninput = U.debounce((e) => {
          state.q = e.target.value.trim();
          reload();
        }, 400);
        [['#f-status', 'status'], ['#f-dept', 'department_id'], ['#f-emp', 'applicant_id'], ['#f-sort', 'sort']].forEach(
          ([sel, key]) => {
            U.qs(sel, tb).onchange = (e) => {
              state[key] = e.target.value;
              reload();
            };
          }
        );
        [['#f-from', 'date_from'], ['#f-to', 'date_to'], ['#f-amin', 'amount_min'], ['#f-amax', 'amount_max']].forEach(
          ([sel, key]) => {
            U.qs(sel, tb).onchange = (e) => {
              state[key] = e.target.value;
              reload();
            };
          }
        );
        U.qs('#f-reset', tb).onclick = () => {
          FILTER_KEYS.forEach((k) => (state[k] = k === 'sort' ? 'id_desc' : ''));
          state.page = 1;
          WB.rerender();
        };
        U.qs('#btn-new', tb).onclick = () => openEditor(null, meta, load);
        U.qs('#btn-export', tb).onclick = () => {
          const p = queryParams(false);
          const q = Object.entries(p)
            .filter(([, v]) => v !== '' && v !== null && v !== undefined)
            .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
            .join('&');
          U.download(`/api/export/reimbursements.csv${q ? '?' + q : ''}`);
        };
      },
    };
  };

  /* v2.8.0：供「扫码核验」等外部入口直接打开报销单详情弹窗 */
  WB.openReimbursement = (id) => openDetail(id, WB.meta);
})();
