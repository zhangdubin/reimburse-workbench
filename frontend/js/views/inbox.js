/* 发票收件箱：邮箱收票 + 自动识别 + 手工导入。
 *
 * 三个页签：
 *   收件记录 —— 每封收过的邮件一行，能追溯「这张票是从哪封邮件来的」
 *   邮箱账号 —— IMAP 配置、连通性测试、立即收票
 *   手工入账 —— 上传电子发票原件，先识别预览再确认入账
 *
 * 识别不出来的票不会丢：会以「待识别-xxxxxx」占位入账并标注需人工复核，
 * 人工在发票台账里补全即可。
 */
window.WB = window.WB || {};
WB.views = WB.views || {};

(function () {
  const api = WB.api;
  const U = WB.util;

  const state = { tab: 'messages', q: '', status: '', page: 1, page_size: 20 };

  const STATUS_TONE = { 成功: 'b-green', 部分成功: 'b-orange', 失败: 'b-red', 跳过: 'b-gray' };

  /* ---------------- 邮箱账号 ---------------- */

  const ACC_FIELDS = [
    { key: 'name', label: '账号名称', required: true, placeholder: '如：财务部收票箱', full: true },
    { key: 'host', label: 'IMAP 服务器', required: true, placeholder: 'imap.exmail.qq.com' },
    { key: 'port', label: '端口', type: 'number', placeholder: '993' },
    { key: 'username', label: '邮箱账号', required: true, placeholder: 'finance@example.com' },
    { key: 'password', label: '密码 / 授权码', type: 'password', placeholder: '留空表示不修改' },
    { key: 'folder', label: '收件文件夹', placeholder: 'INBOX' },
    { key: 'since_days', label: '只看最近 N 天', type: 'number', placeholder: '30' },
    { key: 'max_per_sync', label: '单次最多收几封', type: 'number', placeholder: '30' },
    { key: 'subject_keywords', label: '主题关键词', placeholder: '发票,电子发票（空=不过滤）', full: true },
    { key: 'sender_allow', label: '发件人白名单', placeholder: 'noreply@fapiao.com,*@tax.cn（空=不限制）', full: true },
    { key: 'use_ssl', label: '使用 SSL', type: 'checkbox', hint: '企业邮箱一般都要勾' },
    { key: 'only_unseen', label: '只收未读邮件', type: 'checkbox', hint: '避免重复收取历史邮件' },
    { key: 'mark_seen', label: '收完标记已读', type: 'checkbox', hint: '防止下次重复收取' },
    { key: 'active', label: '启用', type: 'checkbox', hint: '停用后不会被「立即收票」收取' },
    { key: 'remark', label: '备注', full: true },
  ];

  function fieldHtml(f, value) {
    const v = value === null || value === undefined ? '' : value;
    const hint = f.hint ? `<div class="hint">${U.esc(f.hint)}</div>` : '';
    if (f.type === 'checkbox') {
      return `<div class="form-item ${f.full ? 'full' : ''}">
        <label class="switch-row"><input type="checkbox" data-k="${f.key}" ${v ? 'checked' : ''}> ${U.esc(f.label)}</label>
        ${hint}
      </div>`;
    }
    if (f.type === 'select') {
      return `<div class="form-item ${f.full ? 'full' : ''}">
        <label>${U.esc(f.label)}${f.required ? ' <em class="req">*</em>' : ''}</label>
        <select data-k="${f.key}">
          ${f.options.map((o) => `<option value="${U.esc(o.id)}" ${String(o.id) === String(v) ? 'selected' : ''}>${U.esc(o.name)}</option>`).join('')}
        </select>${hint}
      </div>`;
    }
    return `<div class="form-item ${f.full ? 'full' : ''}">
      <label>${U.esc(f.label)}${f.required ? ' <em class="req">*</em>' : ''}</label>
      <input data-k="${f.key}" type="${f.type || 'text'}" value="${U.esc(v)}" placeholder="${U.esc(f.placeholder || '')}">
      ${hint}
    </div>`;
  }

  function collect(root, fields) {
    const out = {};
    fields.forEach((f) => {
      const el = U.qs(`[data-k="${f.key}"]`, root);
      if (!el) return;
      if (f.type === 'checkbox') out[f.key] = el.checked;
      else if (f.type === 'number') out[f.key] = el.value === '' ? null : Number(el.value);
      else out[f.key] = el.value.trim();
    });
    return out;
  }

  function accountForm(initial, onSaved) {
    const isNew = !initial;
    const data = initial || {
      port: 993, folder: 'INBOX', use_ssl: true, only_unseen: true,
      mark_seen: true, active: true, since_days: 30, max_per_sync: 30,
    };
    const m = U.modal({
      title: isNew ? '新增收票邮箱' : `编辑收票邮箱 · ${data.name}`,
      width: 720,
      body: `<div class="mail-fields">${ACC_FIELDS.map((f) => fieldHtml(f, data[f.key])).join('')}</div>
        <div class="mail-hint">密码会用 MAIL_SECRET 加密后落库，前端与接口都不会回显明文；
          编辑时留空即保持原密码不变。企业邮箱通常需要在邮箱后台开启 IMAP 并生成「授权码」而非登录密码。</div>`,
      footer: `<button class="btn" data-close>取消</button>
               <button class="btn btn-primary" id="ma-save">${isNew ? '创建' : '保存'}</button>`,
      onMount(apiMod) {
        U.qs('#ma-save', apiMod.el).onclick = async () => {
          const p = collect(apiMod.el, ACC_FIELDS);
          if (!p.name || !p.host || !p.username) return U.toast('名称、服务器、账号必填', 'warn');
          if (p.port === null) p.port = p.use_ssl ? 993 : 143;
          if (isNew && !p.password) return U.toast('新建时必须填写密码或授权码', 'warn');
          try {
            if (isNew) await api.createMailAccount(p);
            else await api.updateMailAccount(data.id, p);
            U.toast(isNew ? '已创建' : '已保存', 'success');
            apiMod.close();
            onSaved();
          } catch (_) {}
        };
      },
    });
    return m;
  }

  async function testAccount(id, btn) {
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = '测试中…';
    try {
      const r = await api.testMailAccount(id);
      U.toast(r.ok ? `连接成功，可见 ${r.folders.length} 个文件夹` : `连接失败：${r.message}`,
              r.ok ? 'success' : 'error', 6000);
    } catch (_) {
      /* api 层已提示 */
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  async function syncAccount(id, btn) {
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = '收取中…';
    try {
      const r = await api.syncMailAccount(id);
      U.toast(`扫描 ${r.scanned} 封，入库 ${r.imported} 张，跳过 ${r.skipped}，失败 ${r.failed}`,
              r.failed ? 'warn' : 'success', 6000);
      if (r.details && r.details.length) console.info('[收票明细]', r.details);
      WB.rerender();
    } catch (_) {
      WB.rerender();
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  async function tabAccounts(host) {
    const list = await api.mailAccounts();
    // 账号增删改仅管理员（后端 _admin），财务只能看与收票
    const w = WB.can('inbox.account');
    host.innerHTML = `
      <div class="batch-bar">
        <span>共 <b>${list.length}</b> 个收票邮箱，
          其中启用 <b>${list.filter((a) => a.active).length}</b> 个</span>
        <div class="spacer"></div>
        ${w ? '<button class="btn btn-sm btn-primary" id="ma-new">+ 新增收票邮箱</button>' : ''}
      </div>
      <div class="table-wrap">
        <table class="tbl">
          <thead><tr>
            <th>账号名称</th><th>IMAP 服务器</th><th>邮箱账号</th><th>文件夹</th>
            <th>收票口径</th><th>上次收取</th><th class="num">累计入库</th>
            <th style="width:1%">操作</th>
          </tr></thead>
          <tbody>
            ${list.length ? list.map((a) => `<tr data-id="${a.id}">
              <td class="strong">${U.esc(a.name)}
                ${a.active ? '' : '<span class="badge b-gray">已停用</span>'}</td>
              <td class="mono muted">${U.esc(a.host)}:${a.port}${a.use_ssl ? ' / SSL' : ''}</td>
              <td>${U.esc(a.username)}
                ${a.has_password ? '' : '<span class="badge b-red">缺密码</span>'}</td>
              <td class="mono muted">${U.esc(a.folder)}</td>
              <td class="muted" style="font-size:12px">
                ${a.only_unseen ? '仅未读' : '全部'} · 最近 ${a.since_days} 天 · 单次 ${a.max_per_sync} 封
                ${a.subject_keywords ? `<br>主题含：${U.esc(a.subject_keywords)}` : ''}
              </td>
              <td class="sub-line nowrap">
                ${a.last_sync_at ? U.datetime(a.last_sync_at) : '<span class="muted">未收取</span>'}
                ${a.last_sync_status
                  ? `<div class="mail-state ${a.last_sync_status === '成功' ? 'ok' : 'bad'}">
                       <span class="dot"></span>${U.esc(a.last_sync_status)}</div>` : ''}
                ${a.last_sync_detail ? `<div class="muted ellipsis" title="${U.esc(a.last_sync_detail)}" style="max-width:220px">${U.esc(a.last_sync_detail)}</div>` : ''}
              </td>
              <td class="num strong">${a.imported_total}</td>
              <td><div class="row-actions">
                <button class="btn btn-xs btn-primary" data-act="sync">立即收票</button>
                <button class="btn btn-xs" data-act="test">测试连接</button>
                ${w
                  ? `<button class="btn btn-xs" data-act="edit">编辑</button>
                <button class="btn btn-xs btn-danger" data-act="del">删除</button>`
                  : ''}
              </div></td>
            </tr>`).join('') : U.emptyRow(8, '还没有配置收票邮箱。新增后即可自动把邮件里的电子发票收进台账')}
          </tbody>
        </table>
      </div>`;

    const reload = () => WB.rerender();
    if (w) U.qs('#ma-new', host).onclick = () => accountForm(null, reload);
    host.addEventListener('click', async (e) => {
      const b = e.target.closest('button[data-act]');
      if (!b) return;
      const tr = b.closest('tr[data-id]');
      const id = Number(tr.dataset.id);
      const row = list.find((a) => a.id === id);
      const act = b.dataset.act;
      if (act === 'test') return testAccount(id, b);
      if (act === 'sync') return syncAccount(id, b);
      if (act === 'edit') return accountForm(row, reload);
      if (act === 'del') {
        const ok = await U.confirm(
          `确认删除收票邮箱 <b>${U.esc(row.name)}</b>？<br>已收取的发票不受影响，收件记录会保留。`,
          { title: '删除收票邮箱', okText: '删除', danger: true }
        );
        if (!ok) return;
        await api.deleteMailAccount(id);
        U.toast('已删除', 'success');
        reload();
      }
    });
  }

  /* ---------------- 收件记录 ---------------- */

  async function tabMessages(host) {
    host.innerHTML = U.loading(8);
    const data = await api.inboxMessages({
      q: state.q, status: state.status, page: state.page, page_size: state.page_size,
    });
    const canPurge = WB.can('inbox.purge');
    host.innerHTML = `
      <div class="inbox-summary">
        <span class="chip">收件记录 <b>${U.num(data.total)}</b></span>
        <span class="chip">累计入库发票 <b>${U.num(data.summary.imported)}</b></span>
        <span class="chip">失败邮件 <b>${U.num(data.summary.failed)}</b></span>
      </div>
      <div class="batch-bar" id="ib-bar" style="display:none">
        <span>已选 <b id="ib-count">0</b> 条</span>
        <div class="spacer"></div>
        <button class="btn btn-sm btn-danger" id="ib-del">批量删除记录</button>
        ${canPurge ? '<button class="btn btn-sm" id="ib-purge">清空记录</button>' : ''}
      </div>
      <div class="table-wrap">
        <table class="tbl">
          <thead><tr>
            <th class="ck-col"><input type="checkbox" id="ck-all"></th>
            <th>收件时间</th><th>主题</th><th>发件人</th>
            <th class="num">附件</th><th class="num">入库</th><th>状态</th><th>处理明细</th>
          </tr></thead>
          <tbody>
            ${data.items.length ? data.items.map((x) => `<tr data-id="${x.id}">
              <td class="ck-col"><input type="checkbox" class="ck-row" value="${x.id}"></td>
              <td class="sub-line nowrap">${U.datetime(x.created_at)}
                ${x.account_name ? `<div class="muted">${U.esc(x.account_name)}</div>` : '<div class="muted">外部投递</div>'}</td>
              <td class="ellipsis" style="max-width:280px" title="${U.esc(x.subject || '')}">
                ${U.esc(x.subject || '（无主题）')}
                ${x.sent_at ? `<div class="muted sub-line">发件 ${U.datetime(x.sent_at)}</div>` : ''}</td>
              <td class="muted ellipsis" style="max-width:190px" title="${U.esc(x.sender || '')}">${U.esc(x.sender || '—')}</td>
              <td class="num">${x.attachment_count}</td>
              <td class="num strong">${x.imported_count}
                ${x.skipped_count ? `<div class="muted" style="font-size:11px">跳过 ${x.skipped_count}</div>` : ''}</td>
              <td><span class="badge ${STATUS_TONE[x.status] || 'b-gray'}">${U.esc(x.status)}</span></td>
              <td class="muted ellipsis" style="max-width:300px" title="${U.esc(x.detail || '')}">${U.esc(x.detail || '—')}</td>
            </tr>`).join('') : U.emptyRow(8, '还没有收票记录。配置邮箱后点「立即收票」，或用外部通道投递发票附件')}
          </tbody>
        </table>
      </div>
      ${U.pager(data.total, data.page, data.page_size)}`;

    const refreshBar = () => {
      const ids = U.pickedIds(host);
      const bar = U.qs('#ib-bar', host);
      if (!bar) return;
      bar.style.display = ids.length ? '' : 'none';
      U.qs('#ib-count', host).textContent = ids.length;
    };
    U.bindCheckAll(host);
    host.addEventListener('change', (e) => {
      if (e.target.classList.contains('ck-row') || e.target.id === 'ck-all') refreshBar();
    });
    host.addEventListener('click', async (e) => {
      const pg = e.target.closest('button[data-page]');
      if (pg && !pg.disabled) {
        state.page = Number(pg.dataset.page);
        return WB.rerender();
      }
      if (e.target.closest('#ib-del')) {
        const ids = U.pickedIds(host);
        if (!ids.length) return;
        const ok = await U.confirm(`确认删除选中的 <b>${ids.length}</b> 条收件记录？<br>已入账的发票不受影响。`,
                                   { title: '删除收件记录', okText: '删除', danger: true });
        if (!ok) return;
        const r = await api.deleteInboxMessages(ids);
        U.toast(`已删除 ${r.deleted} 条记录`, 'success');
        WB.rerender();
      }
      if (e.target.closest('#ib-purge')) {
        const ok = await U.dangerConfirm(
          '将删除<b>全部收件记录</b>。已入账的发票不受影响，但同一个邮箱的历史邮件可能被重新收取。',
          '清空收件记录'
        );
        if (!ok) return;
        const r = await api.purgeInboxMessages('清空收件记录');
        U.toast(`已清空 ${r.deleted} 条记录`, 'success');
        WB.rerender();
      }
    });
  }

  /* ---------------- 手工识别入账 ---------------- */

  // 识别只是替人打草稿，所有字段都必须能改、能补——识别引擎再强也总有抓不到的
  // 票（拍照件、特殊版式），让人在预览页补齐是最后一道保险。
  // 这里必须和台账的发票字段一一对应，少一个字段就等于「这张票的信息录不全」。
  const REC_FIELDS = [
    { k: 'invoice_no', label: '发票号码', full: true, placeholder: '8 位或 20 位；留空则以「待识别-」占位入账' },
    // 数电票（全电发票）没有发票代码，抓不到是正常的，不能标成「待补录」误导人
    { k: 'invoice_code', label: '发票代码', optional: true, placeholder: '数电票无代码，可留空' },
    { k: 'invoice_type', label: '发票类型' },
    { k: 'invoice_date', label: '开票日期', type: 'date' },
    { k: 'amount', label: '价税合计(元)', type: 'number' },
    { k: 'tax_rate', label: '税率(%)', type: 'number' },
    { k: 'tax_amount', label: '税额(元)', type: 'number' },
    { k: 'seller_name', label: '销售方名称' },
    { k: 'seller_tax_no', label: '销售方税号' },
    { k: 'buyer_name', label: '购买方名称' },
    { k: 'buyer_tax_no', label: '购买方税号' },
  ];

  /* 逐字段来源：让人一眼看出「这一项是解析出来的、OCR 认出来的，还是模型补的」。
   * 模型补的要标出来——它最可能编，最需要人工核。 */
  const SRC_LABEL = {
    ofd: '票面解析', xml: '票面解析', xlsx: '表格', pdf: 'PDF 文字层',
    ocr: 'OCR 识别', text: '邮件正文', filename: '文件名',
    ai: 'AI 补录', mail: '邮件', pdf_text: 'PDF 文字层', pdfium: 'PDF 文字层',
  };

  function srcTag(result, key) {
    const s = (result.field_sources || {})[key];
    if (!s) return '';
    const label = SRC_LABEL[s] || s;
    // 来源可能带后缀（如 pdf+ai），只要含 ai 就按「模型补的」标红提示
    const isAi = String(s).includes('ai');
    return `<span class="recog-src ${isAi ? 'is-ai' : ''}">${U.esc(label)}</span>`;
  }

  function recValue(result, key) {
    const v = result[key];
    return v === null || v === undefined ? '' : String(v);
  }

  /** 该字段是否属于「没识别出来就得人工补」——可选字段（如数电票的发票代码）不算 */
  function recMissing(field, result) {
    return !field.optional && !recValue(result, field.k);
  }

  /** 识别过程摘要：本机走了哪几层、OCR 引擎是什么、模型补了几项 */
  function recognizeTrace(result) {
    const parts = [];
    const layers = result.layers || [];
    if (layers.length) parts.push(`命中层：${layers.map((l) => SRC_LABEL[l] || l).join(' → ')}`);
    const ocr = result.ocr || {};
    if (ocr.backend) {
      parts.push(
        `OCR 后端 ${ocr.backend}${ocr.elapsed_ms ? `（${(ocr.elapsed_ms / 1000).toFixed(1)}s）` : ''}` +
          (ocr.avg_score ? `，平均置信度 ${Math.round(ocr.avg_score * 100)}%` : '')
      );
    }
    const ai = result.ai;
    if (ai) {
      if (ai.applied_count || (ai.applied && ai.applied.length)) {
        const n = ai.applied_count || ai.applied.length;
        parts.push(`模型补录 ${n} 项（${(ai.applied || []).join('、')}）`);
      } else if (ai.called === false || ai.skipped) {
        parts.push(`未调用模型：${ai.skipped || ai.reason || '本地结果已足够'}`);
      } else {
        parts.push('模型已复核，未追加字段');
      }
    } else {
      parts.push('未启用模型兜底');
    }
    return parts.join(' · ');
  }

  function renderRecognize(box, result) {
    const score = result.confidence || 0;
    const low = score < 0.6;
    const missing = REC_FIELDS.filter((f) => recMissing(f, result));
    const aiApplied = (result.ai && (result.ai.applied || [])) || [];
    box.innerHTML = `
      <div class="batch-bar" style="margin-bottom:12px">
        <span>文件 <b>${U.esc(result.filename || '')}</b>（${(result.size / 1024).toFixed(1)} KB）</span>
        <div class="spacer"></div>
        <span class="recog-score">识别置信度
          <span class="recog-bar ${low ? 'low' : ''}"><i style="width:${Math.round(score * 100)}%"></i></span>
          <b>${Math.round(score * 100)}%</b>
        </span>
        <span class="chip">来源 ${U.esc(result.source || 'none')}</span>
      </div>
      <div class="recog-note">
        ${result.recognized ? '' : '<b>未能识别出发票号码</b>，可手工补填。<br>'}
        识别结果已填入下表，<b>可以直接修改、补录</b>${missing.length ? `；标「待补录」的有 <b>${missing.length}</b> 项` : '（本次全部识别成功）'}。
        确认无误后点「确认入账」。
        <div class="recog-trace">${U.esc(recognizeTrace(result))}</div>
        ${aiApplied.length
          ? `<div class="recog-trace" style="color:#8a4d06">
               <b>${aiApplied.length} 项由大模型补录</b>（标红字段），模型可能推断错误，请重点核对金额与税号。
             </div>`
          : ''}
      </div>
      <div class="recog-grid">
        ${REC_FIELDS.map((f) => {
          const missed = recMissing(f, result);
          const blank = !recValue(result, f.k);
          return `<div class="recog-item ${missed ? 'miss' : ''} ${f.optional ? 'opt' : ''}"
            ${f.full ? 'style="grid-column:1/-1"' : ''}>
          <label>${f.label}${srcTag(result, f.k)}${missed ? '<em>待补录</em>' : (blank ? '<em class="opt">可留空</em>' : '')}</label>
          <input data-rk="${f.k}" type="${f.type || 'text'}"
                 ${f.type === 'number' ? 'step="0.01"' : ''}
                 value="${U.esc(recValue(result, f.k))}"
                 placeholder="${U.esc(f.placeholder || '')}">
        </div>`;
        }).join('')}
      </div>
      <div class="row-actions" style="margin-top:16px">
        <button class="btn btn-primary" id="up-import">确认入账</button>
        <button class="btn" id="up-cancel">取消</button>
      </div>`;
  }

  async function tabUpload(host) {
    const aiCfg = (WB.ai && WB.ai.state && WB.ai.state.status) || {};
    const aiReady = !!aiCfg.configured;
    host.innerHTML = `
      <div class="card-body">
        <div class="batch-bar" style="margin-bottom:14px">
          <span>上传电子发票原件（PDF / OFD / XML / XLSX / 图片），系统会自动识别号码、金额、日期与购销方</span>
        </div>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <label class="btn btn-primary" style="cursor:pointer">选择发票文件
            <input type="file" id="up-file" hidden accept=".pdf,.ofd,.xml,.xlsx,.jpg,.jpeg,.png,.webp">
          </label>
          ${U.cameraBtn('#up-file', '拍照识别')}
          <button class="btn" id="up-pick-msg" disabled>识别邮件原文（.eml）</button>
          <label class="switch-row" title="${aiReady ? '本地没认出来的关键字段交给大模型再读一遍' : '尚未在「AI 设置」里接入模型'}">
            <input type="checkbox" id="up-use-ai" ${aiReady ? 'checked' : 'disabled'}>
            <span>AI 兜底识别${aiReady ? '' : '（未接入模型）'}</span>
          </label>
          <span class="muted" style="font-size:12px">识别只会先预览，点「确认入账」才会写入台账</span>
        </div>
        <div id="up-result" style="margin-top:16px"></div>
      </div>`;

    let currentFile = null;
    const useAi = () => {
      const el = U.qs('#up-use-ai', host);
      return !!(el && el.checked);
    };

    function collectForm(box) {
      const out = {};
      box.querySelectorAll('input[data-rk]').forEach((el) => { out[el.dataset.rk] = el.value.trim(); });
      return out;
    }

    const reset = () => {
      currentFile = null;
      const box = U.qs('#up-result', host);
      if (box) box.innerHTML = '';
      const input = U.qs('#up-file', host);
      if (input) input.value = '';
    };

    const doRecognize = async (file) => {
      const box = U.qs('#up-result', host);
      box.innerHTML = `<div class="empty"><span class="spin"></span>正在识别…（图片与扫描件会先跑本地 OCR，可能需要几秒）</div>`;
      try {
        const r = await api.recognizeFile(file, null, null, { useAi: useAi() });
        currentFile = file;
        renderRecognize(box, r);
        U.qs('#up-cancel', box).onclick = reset;

        // 改金额或税率时联动算税额，跟台账登记发票的规则保持一致
        const amountEl = U.qs('[data-rk="amount"]', box);
        const rateEl = U.qs('[data-rk="tax_rate"]', box);
        const taxEl = U.qs('[data-rk="tax_amount"]', box);
        const syncTax = () => {
          const a = Number(amountEl.value) || 0;
          const rt = Number(rateEl.value) || 0;
          if (a && rt) taxEl.value = ((a / (1 + rt / 100)) * (rt / 100)).toFixed(2);
        };
        amountEl.oninput = syncTax;
        rateEl.oninput = syncTax;

        U.qs('#up-import', box).onclick = async () => {
          if (!currentFile) return;
          const btn = U.qs('#up-import', box);
          const vals = collectForm(box);
          if (!vals.invoice_no) {
            const ok = await U.confirm(
              '发票号码为空，将以「待识别-」占位入账，之后可在发票台账里补全。<br>确定继续？',
              { title: '确认入账', okText: '继续入账' }
            );
            if (!ok) return;
          }
          btn.disabled = true;
          // 表单里的值一律提交（含空值）：人工清掉的字段就该保持为空，
          // 不能被识别结果又填回去
          btn.textContent = '入账中…';
          try {
            const r = await api.importFile(currentFile, vals, { useAi: useAi() });
            U.toast(r.duplicate
              ? `已入账，但发票号码已存在，请到「重复报销检测」核对：${r.invoice_no}`
              : `已入账：${r.invoice_no}（${U.money(r.amount)}）`,
              r.duplicate ? 'warn' : 'success', 6000);
            reset();
          } catch (_) {
            btn.disabled = false;
            btn.textContent = '确认入账';
          }
        };
      } catch (err) {
        box.innerHTML = `<div class="empty">识别失败：${U.esc(err.message || err)}</div>`;
      }
    };

    U.qs('#up-file', host).onchange = (e) => {
      const f = e.target.files && e.target.files[0];
      if (f) doRecognize(f);
    };

    // 拖拽上传
    host.addEventListener('dragover', (e) => e.preventDefault());
    host.addEventListener('drop', (e) => {
      e.preventDefault();
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) doRecognize(f);
    });
  }

  /* ---------------- 主视图 ---------------- */

  WB.views.inbox = async function (root) {
    const TABS = [
      ['messages', '收件记录'],
      ['accounts', '收票邮箱'],
      ['upload', '手工入账'],
    ];
    if (!TABS.some(([k]) => k === state.tab)) state.tab = 'messages';

    root.innerHTML = `
      <div class="card">
        <div class="tabs" id="ib-tabs">
          ${TABS.map(([k, n]) => `<div class="tab ${state.tab === k ? 'active' : ''}" data-tab="${k}">${n}</div>`).join('')}
        </div>
        <div id="ib-body"></div>
      </div>`;

    const body = U.qs('#ib-body', root);
    U.qs('#ib-tabs', root).addEventListener('click', (e) => {
      const t = e.target.closest('.tab');
      if (!t) return;
      state.tab = t.dataset.tab;
      state.page = 1;
      // 工具条随页签变化（收件记录有筛选，其余页签有「立即收取全部邮箱」），
      // 必须整视图重渲染，否则按钮会残留或缺失。
      WB.rerender();
    });

    function paint() {
      if (state.tab === 'accounts') return tabAccounts(body);
      if (state.tab === 'upload') return tabUpload(body);
      return tabMessages(body);
    }
    await paint();

    return {
      title: '发票收件箱',
      sub: '邮箱自动收票、电子发票识别与手工入账',
      toolbar: `
        ${state.tab === 'messages'
          ? `<input class="search-input" id="f-q" placeholder="主题 / 发件人" value="${U.esc(state.q)}">
             <div class="field"><label>处理状态</label>
               <select id="f-status"><option value="">全部</option>
                 ${['成功', '部分成功', '失败', '跳过'].map((s) =>
                   `<option value="${s}" ${state.status === s ? 'selected' : ''}>${s}</option>`).join('')}
               </select></div>
             <button class="btn btn-sm" id="ib-refresh">刷新</button>`
          : `<span class="muted" style="font-size:12.5px">
               ${state.tab === 'accounts'
                 ? 'IMAP 收票配置：账号密码加密存储，不会回显'
                 : '支持 PDF / OFD / XML / XLSX / 图片，识别在服务端完成，数据不出内网'}
             </span>`}
        <div class="spacer"></div>
        ${state.tab === 'accounts' || state.tab === 'upload'
          ? `<button class="btn btn-sm btn-primary" id="ib-sync-all">立即收取全部邮箱</button>` : ''}`,
      onToolbar(tb) {
        const q = U.qs('#f-q', tb);
        if (q) {
          let timer = null;
          q.oninput = () => {
            clearTimeout(timer);
            timer = setTimeout(() => {
              state.q = q.value.trim();
              state.page = 1;
              WB.rerender();
            }, 400);
          };
        }
        const st = U.qs('#f-status', tb);
        if (st) st.onchange = () => { state.status = st.value; state.page = 1; WB.rerender(); };
        const rf = U.qs('#ib-refresh', tb);
        if (rf) rf.onclick = () => WB.rerender();
        const sa = U.qs('#ib-sync-all', tb);
        if (sa) {
          sa.onclick = async () => {
            const old = sa.textContent;
            sa.disabled = true;
            sa.textContent = '收取中…';
            try {
              const r = await api.syncAllMail();
              U.toast(`共 ${r.accounts} 个邮箱，本次入库 ${r.imported} 张`, r.imported ? 'success' : 'warn', 6000);
              r.results.forEach((x) => {
                if (!x.ok) U.toast(`${x.account}：${x.message}`, 'error', 6000);
              });
              WB.rerender();
            } catch (_) {
              WB.rerender();
            } finally {
              sa.disabled = false;
              sa.textContent = old;
            }
          };
        }
      },
    };
  };
})();
