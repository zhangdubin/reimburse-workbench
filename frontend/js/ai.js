/* AI 智能助手浮窗
 *
 * 边界（很重要，改动前先读）
 * ------------------------
 * 1. **AI 不直接写库**。后端只回结构化动作方案（type + params + warnings），
 *    由本模块渲染成确认卡片，用户点「执行」后调用**既有业务接口**落库。
 *    权限校验、状态机、审计日志因此仍然只有一套实现——这是刻意的设计，
 *    别为了「让 AI 一步到位」在后端开出直写业务的接口。
 * 2. 参数在后端 `ai_tools.prepare_write` 里已经规范化：名称→id、必填补齐、
 *    状态与角色校验。前端只做「送进接口」这一件事，不重复实现业务规则。
 * 3. 对话走 SSE 流式，事件有四种：`progress`（正在查什么数据）、`delta`（正文增量）、
 *    `done`（收尾，带最终动作）、`error`。```tool / ```action 代码块永远不会出现
 *    在正文里——后端已经剥掉了。
 * 4. 助手可以连续调用只读工具（最多 4 轮）再回答，所以文字可能晚几十秒才出现，
 *    `progress` 事件就是「它还在干活」的信号，别把它当噪音删掉。
 * 5. 上下文只送「当前页面 key」与可选 focus；后端按登录用户可见范围自行拼装数据，
 *    前端不承诺也拿不到越权数据。
 * 6. 历史只存 sessionStorage（关标签页即清），不落 localStorage——
 *    对话里可能带金额与公司名，别长期留在磁盘上。
 *
 * 动作清单的双向约定
 * ----------------
 * 后端 `ai_tools.WRITE_ACTIONS` 是唯一权威；`/api/ai/actions` 把当前角色可用的
 * 动作（含标签与 danger）下发到 `state.catalog`。本文件的 `EXECUTORS` 必须覆盖
 * 全部动作类型——漏了会走到「前端没有实现这个执行器」的兜底分支。
 * `backend/ai_test.py` 里有一条交叉断言专门盯这件事。
 */
window.WB = window.WB || {};

(function () {
  const U = WB.util;
  const api = WB.api;

  const HKEY = 'wb.ai.history';
  const MAX_TURNS = 20;

  /* 助手图标 */
  const ICON_SPARK =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M18 15.5l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9z"/></svg>';
  const ICON_MIN =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M5 12h14"/></svg>';
  const ICON_CLOSE =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
  const ICON_RESET =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/></svg>';
  const ICON_BOLT =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L4 14h6l-1 8 9-12h-6z"/></svg>';

  const state = {
    status: null,
    open: false,
    busy: false,
    msgs: [],
    focus: null,
    /** type -> 后端给的动作标签（含 danger / note），用于渲染确认卡片 */
    catalog: {},
  };

  /* 页面 key -> 人话，只用于提示条 */
  const PAGE_LABEL = {
    dashboard: '统计看板', reimbursements: '报销单管理', invoices: '发票管理',
    inbox: '发票收件箱', expenses: '费用管理', alerts: '异常与预警',
    users: '用户管理', settings: '系统参数', audit: '操作审计', ai_settings: 'AI 设置',
  };

  const QUICK = [
    '这个页面能做什么？',
    '本月费用比上月同期怎么样？',
    '有哪些异常发票需要处理？',
    '帮我起草一张差旅报销单',
  ];

  /* ---------------------------------------------------------------- 轻量渲染
   * 模型爱输出 Markdown。不引第三方库（内网零外链是硬约束），
   * 只支持标题/列表/加粗/行内代码这几种够用的语法。
   */
  function inline(s) {
    return U.esc(s)
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`]+)`/g, '<code>$1</code>');
  }

  function md(text) {
    const src = String(text || '');
    const out = [];
    const lines = src.split('\n');
    let para = [];

    const flush = () => {
      if (para.length) {
        out.push(`<div>${para.join('<br>')}</div>`);
        para = [];
      }
    };

    for (const raw of lines) {
      const line = raw.replace(/\s+$/, '');
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        flush();
        out.push(`<span class="ai-h">${inline(h[2])}</span>`);
        continue;
      }
      const ul = line.match(/^\s*[-*·]\s+(.*)$/);
      if (ul) {
        flush();
        out.push(`<div class="ai-li"><em>•</em><span>${inline(ul[1])}</span></div>`);
        continue;
      }
      const ol = line.match(/^\s*(\d+)[.、)]\s+(.*)$/);
      if (ol) {
        flush();
        out.push(`<div class="ai-li"><em>${ol[1]}.</em><span>${inline(ol[2])}</span></div>`);
        continue;
      }
      if (!line.trim()) {
        flush();
        continue;
      }
      para.push(inline(line));
    }
    flush();
    return out.join('') || '<span class="muted">（空回复）</span>';
  }

  /* ---------------------------------------------------------------- 历史 */
  function loadHistory() {
    try {
      const raw = JSON.parse(sessionStorage.getItem(HKEY) || '[]');
      return Array.isArray(raw) ? raw.slice(-MAX_TURNS * 2) : [];
    } catch (_) {
      return [];
    }
  }

  function saveHistory() {
    try {
      const clean = state.msgs
        .filter((m) => (m.role === 'user' || m.role === 'assistant') && m.text)
        .map((m) => ({ role: m.role, text: m.text }));
      sessionStorage.setItem(HKEY, JSON.stringify(clean.slice(-MAX_TURNS * 2)));
    } catch (_) {
      /* 配额满了就算了，历史不是关键路径 */
    }
  }

  /* ---------------------------------------------------------------- 动作执行
   * 每个动作都映射到一个**既有**业务接口（见 js/api.js）。参数已经过后端
   * `ai_tools.prepare_write` 规范化——名称解析成 id、必填补齐、状态校验完毕，
   * 所以这里只负责把参数送进接口，**不重复做业务判断**：
   * 规则一旦前后端各写一份，迟早会分叉。
   *
   * 新增动作时：`backend/app/ai_tools.py` 注册 → 这里加同名执行器 → 两处都不能漏。
   * `/api/ai/actions` 返回的动作清单里带 `executor: "frontend"`，就是提示这件事。
   */

  /** 去掉后端内部字段（`_amount` 这类只用于展示的），再交给接口 */
  function payload(p) {
    const out = {};
    Object.entries(p || {}).forEach(([k, v]) => {
      if (!k.startsWith('_')) out[k] = v;
    });
    return out;
  }

  function normItems(rows) {
    return (rows || []).map((it) => ({
      id: it.id ?? null,
      category_id: it.category_id ?? null,
      occur_date: it.occur_date || U.today(),
      amount: Number(it.amount || 0),
      tax_amount: Number(it.tax_amount || 0),
      description: it.description || '',
    }));
  }

  /** 主数据 PUT 是全量替换：先取回原记录再合并，否则会把没提到的字段清成默认值 */
  function merge(cur, patch) {
    const out = Object.assign({}, cur);
    Object.entries(patch).forEach(([k, v]) => {
      if (v !== null && v !== undefined && k !== 'id') out[k] = v;
    });
    return out;
  }

  async function saveMaster(res, p, label) {
    if (p.id) {
      const cur = await api.get(`/api/${res}/${p.id}`);
      const r = await api.update(res, p.id, merge(cur, payload(p)));
      return `已更新${label}「${r.name || p.name}」`;
    }
    const r = await api.create(res, payload(p));
    return `已新建${label}「${r.name || p.name}」`;
  }

  const EXECUTORS = {
    /* ---------------- 报销单 ---------------- */
    async create_reimbursement(p) {
      const r = await api.createReimbursement({
        title: p.title,
        purpose: p.purpose ?? null,
        applicant_id: p.applicant_id ?? null,
        department_id: p.department_id ?? null,
        customer_id: p.customer_id ?? null,
        project_id: p.project_id ?? null,
        occur_start: p.occur_start || null,
        occur_end: p.occur_end || null,
        remark: p.remark ?? null,
        items: normItems(p.items),
      });
      // 发票关联单独走 link，建单接口不接收发票 id
      const ids = p.invoice_ids || [];
      for (const iid of ids) await api.linkInvoice(iid, { reimbursement_id: r.id });
      const tail = ids.length ? `，并关联了 ${ids.length} 张发票` : '';
      return `已创建 ${r.code}（${U.money(r.total_amount)}），状态「${r.status}」${tail}`;
    },

    async update_reimbursement(p) {
      const r = await api.updateReimbursement(p.id, {
        title: p.title,
        purpose: p.purpose ?? null,
        applicant_id: p.applicant_id ?? null,
        department_id: p.department_id ?? null,
        customer_id: p.customer_id ?? null,
        project_id: p.project_id ?? null,
        occur_start: p.occur_start || null,
        occur_end: p.occur_end || null,
        remark: p.remark ?? null,
        items: normItems(p.items),
      });
      return `已更新 ${r.code}（${U.money(r.total_amount)}）`;
    },

    async submit_reimbursement(p) {
      const r = await api.action(p.reimbursement_id, 'submit', { comment: p.comment || '' });
      return `已提交 ${r.code}，当前状态「${r.status}」`;
    },

    async withdraw_reimbursement(p) {
      const r = await api.action(p.reimbursement_id, 'withdraw', { comment: p.comment || '' });
      return `已撤回 ${r.code}，当前状态「${r.status}」`;
    },

    async approve_reimbursement(p) {
      const r = await api.action(p.reimbursement_id, 'approve', { comment: p.comment || '' });
      return `已审批 ${r.code}，当前状态「${r.status}」`;
    },

    async reject_reimbursement(p) {
      const r = await api.action(p.reimbursement_id, 'reject', { comment: p.comment || '' });
      return `已驳回 ${r.code}，当前状态「${r.status}」`;
    },

    async pay_reimbursement(p) {
      const r = await api.action(p.reimbursement_id, 'pay', { comment: p.comment || '' });
      return `已登记付款 ${r.code}，当前状态「${r.status}」`;
    },

    async unpay_reimbursement(p) {
      const r = await api.action(p.reimbursement_id, 'unpay', { comment: p.comment || '' });
      return `已撤销付款 ${r.code}，当前状态「${r.status}」`;
    },

    async delete_reimbursement(p) {
      await api.deleteReimbursement(p.reimbursement_id);
      return `已删除报销单 ${p.code || '#' + p.reimbursement_id}`;
    },

    /* ---------------- 发票 ---------------- */
    async create_invoice(p) {
      const r = await api.createInvoice(payload(p));
      return `已登记发票 ${r.invoice_no}（${U.money(r.amount)}），状态「${r.check_status}」`;
    },

    async update_invoice(p) {
      const body = payload(p);
      delete body.id;
      const r = await api.updateInvoice(p.id, body);
      return `已更新发票 ${r.invoice_no}（${U.money(r.amount)}）`;
    },

    async categorize_invoices(p) {
      const ids = p.invoice_ids || [];
      for (const iid of ids) {
        // 发票 PUT 同样是全量替换，缺字段会被清空，必须合并
        const cur = await api.invoice(iid);
        await api.updateInvoice(iid, Object.assign({}, cur, {
          category_id: p.category_id,
          reimbursement_id: cur.reimbursement_id ?? null,
        }));
      }
      return `已把 ${ids.length} 张发票归到「${p.category_name || p.category_id}」`;
    },

    async link_invoices(p) {
      const ids = p.invoice_ids || [];
      for (const iid of ids) {
        await api.linkInvoice(iid, { reimbursement_id: p.reimbursement_id });
      }
      return `已把 ${ids.length} 张发票关联到 ${p.code || '#' + p.reimbursement_id}`;
    },

    async unlink_invoices(p) {
      const ids = p.invoice_ids || [];
      for (const iid of ids) await api.linkInvoice(iid, {});
      return `已把 ${ids.length} 张发票从报销单上取下`;
    },

    async check_invoices(p) {
      const ids = p.invoice_ids || [];
      const r = await api.batchCheck(ids.length ? ids : null);
      return `查验完成：通过 ${r.ok || 0}，有问题 ${r.bad || 0}`;
    },

    async delete_invoice(p) {
      await api.deleteInvoice(p.invoice_id);
      return `已删除发票 ${p.invoice_no}`;
    },

    /* ---------------- 主数据 ---------------- */
    create_customer: (p) => saveMaster('customers', p, '客户'),
    update_customer: (p) => saveMaster('customers', p, '客户'),
    create_project: (p) => saveMaster('projects', p, '项目'),
    update_project: (p) => saveMaster('projects', p, '项目'),
    create_department: (p) => saveMaster('departments', p, '部门'),
    update_department: (p) => saveMaster('departments', p, '部门'),
    create_employee: (p) => saveMaster('employees', p, '员工'),
    update_employee: (p) => saveMaster('employees', p, '员工'),
    create_category: (p) => saveMaster('categories', p, '费用类型'),
    update_category: (p) => saveMaster('categories', p, '费用类型'),

    async set_budget(p) {
      const body = payload(p);
      delete body.id;
      if (p.id) {
        const r = await api.update('budgets', p.id, body);
        return `已更新预算：${p.year} 年 ${U.money(r.amount)}`;
      }
      const r = await api.create('budgets', body);
      return `已设置预算：${p.year} 年 ${U.money(r.amount)}`;
    },

    async delete_master(p) {
      await api.remove(p.entity, p.id);
      return `已删除${p.entity}「${p.name}」`;
    },

    /* ---------------- 账号 / 参数 ---------------- */
    async create_user(p) {
      const r = await api.createUser(payload(p));
      return `已创建账号 ${r.username || p.username}（${p.role}）`;
    },

    async update_user(p) {
      const body = payload(p);
      delete body.id;
      const r = await api.updateUser(p.id, body);
      return `已更新账号 ${r.username || p.username}`;
    },

    async reset_password(p) {
      await api.resetUserPassword(p.id, p.new_password);
      return `已重置 ${p.username} 的口令，他用新口令登录后需再改一次`;
    },

    async set_setting(p) {
      await api.saveSetting(p.key, p.value);
      return `已把 ${p.key} 改为 ${p.value}`;
    },

    /* ---------------- 收票邮箱 ---------------- */
    async create_mail_account(p) {
      const r = await api.createMailAccount(payload(p));
      return `已新增收票邮箱「${r.name || p.name}」，建议测一次连接`;
    },

    async sync_mail(p) {
      if (p.account_id) {
        const r = await api.syncMailAccount(p.account_id, {});
        return `收票完成：新收到 ${r.saved ?? r.new ?? 0} 张`;
      }
      const r = await api.syncAllMail();
      return `收票完成：新收到 ${r.saved ?? r.new ?? 0} 张`;
    },

    /* ---------------- 批量 ---------------- */
    async batch_check_invoices(p) {
      const ids = p.invoice_ids || [];
      const r = await api.batchCheck(ids.length ? ids : null);
      return `查验完成：通过 ${r.ok || 0}，有问题 ${r.bad || 0}`;
    },

    async batch_delete(p) {
      const r = await api.batchDelete(p.entity, p.ids);
      const skipped = (r.skipped || []).length;
      return `已删除 ${r.deleted ?? (p.ids || []).length} 条` + (skipped ? `，${skipped} 条被跳过` : '');
    },
  };

  /* 动作标签优先用后端目录里的（`/api/ai/actions`），拿不到时退回本地兜底。
     双份维护容易分叉，所以本地这份只当离线兜底。 */
  const LABEL_FALLBACK = {
    create_reimbursement: '新建报销单', update_reimbursement: '修改报销单',
    submit_reimbursement: '提交报销单', withdraw_reimbursement: '撤回报销单',
    approve_reimbursement: '审批通过', reject_reimbursement: '驳回报销单',
    pay_reimbursement: '登记付款', unpay_reimbursement: '撤销付款',
    delete_reimbursement: '删除报销单',
    create_invoice: '登记发票', update_invoice: '修改发票',
    categorize_invoices: '设置费用类型', link_invoices: '关联发票到报销单',
    unlink_invoices: '从报销单取下发票', check_invoices: '查验发票', delete_invoice: '删除发票',
    create_customer: '新建客户', update_customer: '修改客户',
    create_project: '新建项目', update_project: '修改项目',
    create_department: '新建部门', update_department: '修改部门',
    create_employee: '新建员工', update_employee: '修改员工',
    create_category: '新建费用类型', update_category: '修改费用类型',
    set_budget: '设置预算', delete_master: '删除主数据',
    create_user: '新建登录账号', update_user: '修改账号',
    reset_password: '重置账号口令', set_setting: '修改系统参数',
    create_mail_account: '新增收票邮箱', sync_mail: '立即收票',
    batch_check_invoices: '批量查验发票', batch_delete: '批量删除',
  };

  function labelOf(type) {
    const fromCatalog = state.catalog && state.catalog[type];
    return fromCatalog || LABEL_FALLBACK[type] || type;
  }

  /* ---------------------------------------------------------------- DOM */
  function fabEl() {
    return document.getElementById('ai-fab');
  }

  function ready() {
    return !!(state.status && state.status.configured);
  }

  function mountFab() {
    if (fabEl()) return;
    const btn = document.createElement('button');
    btn.id = 'ai-fab';
    btn.className = 'ai-fab' + (ready() ? '' : ' off');
    btn.type = 'button';
    btn.innerHTML = `${ICON_SPARK}<span class="ai-fab-dot"></span><span>智能助手</span>`;
    btn.onclick = () => toggle();
    document.body.appendChild(btn);
  }

  function syncFab() {
    const btn = fabEl();
    if (!btn) return;
    btn.classList.toggle('off', !ready());
    btn.title = ready()
      ? `已接入 ${(state.status.provider || '')} / ${state.status.model || ''}`
      : '尚未配置大模型，点此查看如何开启';
  }

  function currentPage() {
    return (location.hash || '').replace(/^#\/?/, '').split('?')[0] || 'dashboard';
  }

  function renderCtx() {
    const el = document.getElementById('ai-ctx');
    if (!el) return;
    const ocr = (state.status && state.status.ocr) || {};
    const ocrTxt = ocr.ocr_ready
      ? `识别引擎 ${ocr.engine}`
      : '未启用本地 OCR（照片类发票需人工补录）';
    el.innerHTML = `
      <span class="chip">当前页面：${U.esc(PAGE_LABEL[currentPage()] || currentPage())}</span>
      <span class="chip">${U.esc(state.status && state.status.model ? state.status.model : '未配置模型')}</span>
      <span class="chip">${U.esc(ocrTxt)}</span>`;
  }

  function renderMsgs() {
    const box = document.getElementById('ai-msgs');
    if (!box) return;
    if (!state.msgs.length) {
      box.innerHTML = `<div class="ai-empty">
        我是这套系统里的助手。可以问功能怎么用、让我读数据做分析，
        也可以让我起草新建/提交/审批这类操作——最终执行由你点确认。
        <div class="ai-chips">${QUICK.map((q) => `<button class="ai-chip" type="button">${U.esc(q)}</button>`).join('')}</div>
      </div>`;
      U.qsa('.ai-chip', box).forEach((c) => {
        c.onclick = () => send(c.textContent);
      });
      return;
    }
    box.innerHTML = state.msgs
      .map((m) => {
        const who = m.role === 'user' ? 'u' : 'a';
        const badge = m.role === 'user' ? '我' : 'AI';
        let inner;
        if (m.text) inner = m.html || md(m.text);
        else if (m.error) inner = `<span class="ai-err-text">${U.esc(m.error)}</span>`;
        else inner = '';
        const typing = m.done ? '' : '<span class="ai-typing"><i></i><i></i><i></i></span>';
        const prog = !m.done && m.progress ? `<div class="ai-progress">${U.esc(m.progress)}</div>` : '';
        const body =
          m.role === 'user'
            ? `<div class="ai-bubble">${U.esc(m.text)}</div>`
            : `<div class="ai-bubble rich">${inner}${prog}${typing}</div>`;
        const act = m.action ? actionCard(m) : '';
        return `<div class="ai-msg ${who}"><span class="ai-badge">${badge}</span><div style="min-width:0;max-width:100%">${body}${act}</div></div>`;
      })
      .join('');
    box.scrollTop = box.scrollHeight;
    bindActionCards(box);
  }

  function actionCard(m) {
    const a = m.action;
    const label = a.label || labelOf(a.type);
    const danger = a.danger || (state.catalog[a.type] && state.catalog[a.type].danger);
    const cls = m.actState === 'done' ? ' done' : m.actState === 'failed' ? ' failed' : '';
    const result = m.actResult
      ? `<div class="ai-test-line ${m.actState === 'failed' ? 'bad' : 'ok'}">${U.esc(m.actResult)}</div>`
      : '';
    const warns = (a.warnings || []).length
      ? `<div class="ai-action-warns">${a.warnings.map((w) => `<div>· ${U.esc(w)}</div>`).join('')}</div>`
      : '';
    const buttons = m.actState
      ? ''
      : `<button class="btn btn-sm btn-primary" data-ai-run="${m.id}">执行</button>
         <button class="btn btn-sm" data-ai-skip="${m.id}">忽略</button>`;
    return `<div class="ai-action${cls}${danger ? ' danger' : ''}">
      <div class="ai-action-head">${ICON_BOLT}<span>待确认操作：${U.esc(label)}${
        danger ? '<em class="ai-danger-tag">不可逆</em>' : ''
      }</span></div>
      ${a.summary ? `<div class="ai-action-sum">${U.esc(a.summary)}</div>` : ''}
      <div class="ai-action-body"><pre>${U.esc(JSON.stringify(a.params || {}, null, 2))}</pre></div>
      ${warns}
      <div class="ai-action-foot">${buttons}${result}
        ${m.actState ? '' : '<span class="ai-note">执行走的是你本人的权限</span>'}</div>
    </div>`;
  }

  function bindActionCards(box) {
    U.qsa('[data-ai-run]', box).forEach((btn) => {
      btn.onclick = () => runAction(Number(btn.dataset.aiRun));
    });
    U.qsa('[data-ai-skip]', box).forEach((btn) => {
      btn.onclick = () => {
        const m = state.msgs.find((x) => x.id === Number(btn.dataset.aiSkip));
        if (!m) return;
        m.actState = 'failed';
        m.actResult = '已忽略，未执行任何写操作。';
        renderMsgs();
      };
    });
  }

  async function runAction(id) {
    const m = state.msgs.find((x) => x.id === id);
    if (!m || !m.action) return;
    const a = m.action;
    const label = a.label || labelOf(a.type);
    const danger = a.danger || (state.catalog[a.type] && state.catalog[a.type].danger);
    const warns = (a.warnings || []).map((w) => `· ${U.esc(w)}`).join('<br>');
    const go = await U.confirm(
      `${danger ? '<b>这个操作不可逆，确认后无法一键撤销。</b><br><br>' : ''}` +
        `将执行「${U.esc(label)}」。<br><br>${U.esc(a.summary || '')}` +
        (warns ? `<br><br>${warns}` : ''),
      { title: danger ? '确认执行（不可逆）' : '确认执行', okText: '执行' }
    );
    if (!go) return;
    const fn = EXECUTORS[a.type];
    if (!fn) {
      m.actState = 'failed';
      m.actResult = `前端没有实现「${a.type}」的执行器，请手动在对应页面操作。`;
      renderMsgs();
      return;
    }
    m.actState = 'running';
    renderMsgs();
    try {
      const msg = await fn(m.action.params || {});
      m.actState = 'done';
      m.actResult = msg;
      U.toast(msg, 'success');
      // 写操作完成后刷新当前视图，让用户立刻看到结果
      WB.rerender && WB.rerender();
    } catch (e) {
      m.actState = 'failed';
      m.actResult = `执行失败：${e.message || e}`;
    }
    renderMsgs();
  }

  /* ---------------------------------------------------------------- 面板 */
  function mountPanel() {
    if (document.getElementById('ai-panel')) return;
    const el = document.createElement('div');
    el.id = 'ai-panel';
    el.className = 'ai-panel';
    el.innerHTML = `
      <div class="ai-head">
        <span class="ai-avatar">${ICON_SPARK}</span>
        <div class="ai-head-body">
          <b>智能助手</b>
          <span id="ai-head-sub">读取你有权限的数据 · 写操作需你确认</span>
        </div>
        <div class="ai-head-actions">
          <button class="ai-icon-btn" id="ai-reset" title="清空对话">${ICON_RESET}</button>
          <button class="ai-icon-btn" id="ai-min" title="收起">${ICON_MIN}</button>
          <button class="ai-icon-btn" id="ai-close" title="关闭">${ICON_CLOSE}</button>
        </div>
      </div>
      <div class="ai-ctx" id="ai-ctx"></div>
      <div class="ai-msgs" id="ai-msgs"></div>
      ${ready() ? composerHtml() : unsetHtml()}`;
    document.body.appendChild(el);

    U.qs('#ai-min', el).onclick = () => close();
    U.qs('#ai-close', el).onclick = () => close();
    U.qs('#ai-reset', el).onclick = async () => {
      if (!state.msgs.length) return;
      const go = await U.confirm('清空当前对话？历史只保留在本次会话中。', { okText: '清空' });
      if (!go) return;
      state.msgs = [];
      saveHistory();
      renderMsgs();
    };

    if (ready()) bindComposer(el);
    renderCtx();
    renderMsgs();
  }

  function composerHtml() {
    return `<div class="ai-composer">
      <textarea id="ai-input" rows="1" placeholder="问点什么，或直接说「帮我起草…」（Enter 发送，Shift+Enter 换行）"></textarea>
      <div class="ai-composer-row">
        <span class="ai-tip">助手只做读取与分析，写操作一律先给你确认。</span>
        <span class="spacer"></span>
        <button class="ai-send" id="ai-send">发送</button>
      </div>
    </div>`;
  }

  function unsetHtml() {
    const canManage = state.status && state.status.can_manage;
    return `<div class="ai-unset">
      <b>尚未配置大模型。</b>
      ${canManage
        ? '请到「AI 设置」页填写厂商、API Key 与模型名，保存后即可对话。'
        : '请联系管理员在「AI 设置」里接入模型后使用。'}
      ${canManage
        ? '<div style="margin-top:8px"><button class="btn btn-sm btn-primary" id="ai-go-set">去配置</button></div>'
        : ''}
      <div class="ai-test-line" style="margin-top:8px">
        本地识别引擎：${U.esc(
          state.status && state.status.ocr && state.status.ocr.ocr_ready
            ? state.status.ocr.engine + '（已就绪）'
            : '未启用，照片/扫描件发票需人工补录'
        )}
      </div>
    </div>`;
  }

  function bindComposer(el) {
    const input = U.qs('#ai-input', el);
    const send_ = U.qs('#ai-send', el);
    const submit = () => {
      const v = input.value.trim();
      if (!v || state.busy) return;
      input.value = '';
      input.style.height = 'auto';
      send(v);
    };
    send_.onclick = submit;
    input.onkeydown = (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        submit();
      }
    };
    input.oninput = () => {
      input.style.height = 'auto';
      input.style.height = Math.min(132, input.scrollHeight) + 'px';
    };
    setTimeout(() => input.focus(), 60);
  }

  /* ---------------------------------------------------------------- 对话 */
  async function send(text) {
    if (state.busy) return;
    if (!ready()) {
      U.toast('尚未配置大模型，请先在「AI 设置」里接入', 'warn');
      return;
    }
    state.busy = true;
    const sendBtn = document.getElementById('ai-send');
    if (sendBtn) sendBtn.disabled = true;

    state.msgs.push({ role: 'user', text });
    const aiMsg = { role: 'assistant', text: '', id: Date.now(), done: false };
    state.msgs.push(aiMsg);
    renderMsgs();
    saveHistory();

    const history = state.msgs
      .filter((m) => (m.role === 'user' || m.role === 'assistant') && m.text)
      .slice(-MAX_TURNS * 2)
      .map((m) => ({ role: m.role, content: m.text }));

    const box = document.getElementById('ai-msgs');

    try {
      const res = await fetch('/api/ai/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: 'Bearer ' + (WB.auth.token || ''),
        },
        body: JSON.stringify({
          messages: history,
          page: currentPage(),
          focus: state.focus,
          stream: true,
        }),
      });

      if (res.status === 401) {
        WB.auth.onUnauthorized();
        throw new Error('未登录或登录已过期');
      }
      if (!res.ok) {
        let msg = `请求失败 (${res.status})`;
        try {
          const d = await res.json();
          if (typeof d.detail === 'string') msg = d.detail;
        } catch (_) {}
        throw new Error(msg);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const chunk = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 2);
          if (!chunk.startsWith('data:')) continue;
          let payload;
          try {
            payload = JSON.parse(chunk.slice(5).trim());
          } catch (_) {
            continue;
          }
          if (payload.type === 'delta') {
            aiMsg.text += payload.text || '';
            // 边收边渲染：只重画这一条，避免整列闪烁
            const bubble = box.querySelector('.ai-msg:last-child .ai-bubble');
            if (bubble && aiMsg.text) bubble.innerHTML = md(aiMsg.text);
            if (box) box.scrollTop = box.scrollHeight;
          } else if (payload.type === 'progress') {
            // 助手正在查库/补参数。多轮工具调用时文字会晚几十秒才出现，
            // 这行提示就是「它还在干活」的唯一信号。
            aiMsg.progress = payload.text || '';
            renderMsgs();
          } else if (payload.type === 'error') {
            throw new Error(payload.message || '模型调用失败');
          } else if (payload.type === 'done') {
            aiMsg.text = payload.text || aiMsg.text;
            aiMsg.done = true;
            aiMsg.progress = '';
            aiMsg.model = payload.model;
            aiMsg.trace = payload.trace || [];
            // 动作已经过后端 prepare_write 规范化：params 里是可直接落库的 id，
            // 还带 warnings（如「申请人默认用了你绑定的员工」）与 danger 标记
            if (payload.action && payload.action.type) aiMsg.action = payload.action;
          }
        }
      }

      if (!aiMsg.text) aiMsg.text = '（模型没有返回内容，请重试或检查模型配置）';
      aiMsg.done = true;
    } catch (e) {
      aiMsg.done = true;
      if (!aiMsg.text) {
        aiMsg.text = '';
        aiMsg.error = e.message || String(e);
      } else {
        aiMsg.text += `\n\n（出错：${e.message || e}）`;
      }
      U.toast(e.message || '助手调用失败', 'error');
    } finally {
      state.busy = false;
      if (sendBtn) sendBtn.disabled = false;
      saveHistory();
      renderMsgs();
      const input = document.getElementById('ai-input');
      if (input) input.focus();
    }
  }

  /* ---------------------------------------------------------------- 开关 */
  function open() {
    mountPanel();
    const p = document.getElementById('ai-panel');
    if (p) p.style.display = '';
    const btn = fabEl();
    if (btn) btn.classList.add('hidden');
    state.open = true;
    const input = document.getElementById('ai-input');
    if (input) input.focus();
  }

  function close() {
    const p = document.getElementById('ai-panel');
    if (p) p.style.display = 'none';
    const btn = fabEl();
    if (btn) btn.classList.remove('hidden');
    state.open = false;
  }

  function toggle() {
    if (state.open) close();
    else open();
  }

  async function refresh() {
    try {
      state.status = await api.get('/api/ai/status');
    } catch (_) {
      state.status = null;
    }
    await loadCatalog();
    const p = document.getElementById('ai-panel');
    if (p) p.remove(); // 配置变了要重建（未配置 -> 已配置 会换掉整个底部区域）
    if (state.open) mountPanel();
    syncFab();
    return state.status;
  }

  /** 拉当前角色可用的动作目录（标签 + 危险标记）。拉不到不影响对话，只是标签退回兜底。 */
  async function loadCatalog() {
    // 没会话就别发（登出后浮窗初始化延迟回来时，这里会打出一条无意义的 401）
    if (!WB.auth || !WB.auth.token) {
      state.catalog = {};
      return;
    }
    try {
      const data = await api.aiActions();
      const map = {};
      (data.actions || []).forEach((a) => {
        map[a.type] = a;
      });
      state.catalog = map;
    } catch (_) {
      state.catalog = {};
    }
  }

  async function init() {
    state.msgs = loadHistory();
    await refresh();
    mountFab();
  }

  WB.ai = {
    init,
    open,
    close,
    toggle,
    refresh,
    state,
    /** 由详情页调用，把当前单据带给助手当上下文 */
    setFocus(focus) {
      state.focus = focus || null;
    },
  };
})();
