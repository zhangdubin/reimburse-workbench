/* 视图：发票管理 */
window.WB = window.WB || {};
WB.views = WB.views || {};

(function () {
  const U = WB.util;
  const api = WB.api;
  const C = WB.charts;

  const state = {
    tab: 'ledger',
    q: '', check_status: '', invoice_type: '', unlinked: false,
    date_from: '', date_to: '', amount_min: '', amount_max: '',
    sort: 'id_desc', page: 1, page_size: 20,
  };

  function params(withPage = true) {
    const p = {
      q: state.q, check_status: state.check_status, invoice_type: state.invoice_type,
      unlinked: state.unlinked || '', date_from: state.date_from, date_to: state.date_to,
      amount_min: state.amount_min, amount_max: state.amount_max, sort: state.sort,
    };
    if (withPage) {
      p.page = state.page;
      p.page_size = state.page_size;
    }
    return p;
  }

  /* ---------------- 表单弹窗 ---------------- */

  /** 把识别结果填进登记表单，返回被填入项的名称。
   *
   *  识别值优先覆盖表单（登记页的默认值只是占位，不是人的输入），
   *  但识别不到的一律不动——不能把默认值冲成空，也不能让空值盖掉人工已填的内容。
   */
  function applyRecognition(g, r, meta) {
    const filled = [];
    const put = (sel, value, label) => {
      if (value === null || value === undefined || value === '') return;
      const el = g(sel);
      if (!el) return;
      el.value = String(value);
      filled.push(label);
    };
    put('#v-no', r.invoice_no, '发票号码');
    put('#v-code', r.invoice_code, '发票代码');
    put('#v-date', r.invoice_date, '开票日期');
    put('#v-amount', r.amount, '价税合计');
    put('#v-rate', r.tax_rate, '税率');
    put('#v-tax', r.tax_amount, '税额');
    put('#v-seller', r.seller_name, '销售方名称');
    put('#v-selltax', r.seller_tax_no, '销售方税号');
    put('#v-buyer', r.buyer_name, '购买方名称');
    put('#v-buytax', r.buyer_tax_no, '购买方税号');
    // 发票类型是下拉框：只有选项里确实存在才写入，否则会静默落到第一项，反而改错类型
    const typeEl = g('#v-type');
    if (r.invoice_type && typeEl && [...typeEl.options].some((o) => o.value === r.invoice_type)) {
      typeEl.value = r.invoice_type;
      filled.push('发票类型');
    }
    return filled;
  }

  function openForm(row, meta, onDone) {
    const isNew = !row;
    const d = row || {
      invoice_no: '', invoice_code: '', invoice_type: '增值税电子普通发票', amount: '',
      tax_rate: 6, tax_amount: '', invoice_date: U.today(), seller_name: '', seller_tax_no: '',
      buyer_name: '深圳市智联云创科技有限公司', buyer_tax_no: '', category_id: null, remark: '',
    };
    U.modal({
      title: isNew ? '登记发票' : `编辑发票 ${d.invoice_no}`,
      width: 720,
      body: `
        ${isNew ? `<div class="form-section">上传发票自动识别</div>
        <div class="recog-drop">
          <label class="btn btn-sm" style="cursor:pointer">选择发票文件
            <input type="file" id="v-file" hidden accept=".pdf,.ofd,.xml,.xlsx,.jpg,.jpeg,.png,.webp">
          </label>
          ${U.cameraBtn('#v-file', '拍照识别')}
          <label class="switch-row" title="本地解析与 OCR 都没覆盖到的关键字段，交给大模型再读一遍">
            <input type="checkbox" id="v-use-ai" ${(WB.ai && WB.ai.state && WB.ai.state.status && WB.ai.state.status.configured) ? 'checked' : 'disabled'}>
            <span>AI 兜底识别</span>
          </label>
          <span class="muted" id="v-recog-hint">支持 PDF / OFD / XML / XLSX / 图片；识别结果会自动填入下方表单，仍可手工修改</span>
        </div>` : ''}
        <div class="form-section">票面信息</div>
        <div class="form-grid">
          <div class="form-item"><label>发票号码<span class="req">*</span></label>
            <input id="v-no" value="${U.esc(d.invoice_no)}" placeholder="8 位或 20 位"></div>
          <div class="form-item"><label>发票代码</label>
            <input id="v-code" value="${U.esc(d.invoice_code || '')}"></div>
          <div class="form-item"><label>发票类型</label>
            <select id="v-type">${U.options(meta.invoice_types.map((t) => ({ id: t, name: t })), { selected: d.invoice_type })}</select></div>
          <div class="form-item"><label>开票日期</label><input type="date" id="v-date" value="${U.esc(d.invoice_date || '')}"></div>
          <div class="form-item"><label>价税合计(元)<span class="req">*</span></label>
            <input type="number" step="0.01" id="v-amount" value="${d.amount === '' ? '' : d.amount}"></div>
          <div class="form-item"><label>税率(%)</label>
            <input type="number" step="0.01" id="v-rate" value="${d.tax_rate == null ? '' : d.tax_rate}"></div>
          <div class="form-item"><label>税额(元)</label>
            <input type="number" step="0.01" id="v-tax" value="${d.tax_amount === '' ? '' : d.tax_amount}"></div>
          <div class="form-item"><label>费用类型</label>
            <select id="v-cat">${U.options(meta.categories, { selected: d.category_id, placeholder: '不指定' })}</select></div>
        </div>
        <div class="form-section" style="margin-top:14px">购销双方</div>
        <div class="form-grid">
          <div class="form-item"><label>销售方名称</label><input id="v-seller" value="${U.esc(d.seller_name || '')}"></div>
          <div class="form-item"><label>销售方税号</label><input id="v-selltax" value="${U.esc(d.seller_tax_no || '')}"></div>
          <div class="form-item"><label>购买方名称</label><input id="v-buyer" value="${U.esc(d.buyer_name || '')}"></div>
          <div class="form-item"><label>购买方税号</label><input id="v-buytax" value="${U.esc(d.buyer_tax_no || '')}"></div>
          <div class="form-item full"><label>备注</label><input id="v-remark" value="${U.esc(d.remark || '')}"></div>
        </div>
        <p class="muted" style="font-size:11.5px;margin:12px 0 0">提示：保存时系统会自动检测发票号码是否重复，重复的发票会被标记为「异常」。</p>`,
      footer: `<button class="btn" data-close>取消</button><button class="btn btn-primary" data-save>保存</button>`,
      onMount(m) {
        const g = (id) => U.qs(id, m.el);
        // 上传发票 -> 服务端识别 -> 自动填表。财务手工登记发票时不用再逐个字段手敲，
        // 识别不准的地方仍然可以改（这是「手工录入信息不全」的正解：先把能识别的填上）。
        const fileEl = g('#v-file');
        if (fileEl) {
          fileEl.onchange = async (e) => {
            const f = e.target.files && e.target.files[0];
            if (!f) return;
            const hint = g('#v-recog-hint');
            hint.textContent = `正在识别 ${f.name}…`;
            try {
              const useAi = !!(g('#v-use-ai') && g('#v-use-ai').checked);
              const r = await api.recognizeFile(f, null, null, { useAi });
              const filled = applyRecognition(g, r, meta);
              const ocr = r.ocr || {};
              const ai = r.ai || {};
              const aiN = (ai.applied || []).length;
              const bits = [`来源 ${U.esc(r.source || '—')}`, `置信度 ${Math.round((r.confidence || 0) * 100)}%`];
              if (ocr.backend) bits.push(`OCR ${U.esc(ocr.backend)}`);
              if (aiN) bits.push(`<b style="color:#8a4d06">模型补录 ${aiN} 项</b>`);
              if (filled.length) {
                hint.innerHTML = `已识别并填入 <b>${filled.length}</b> 项：${filled.join('、')}
                  <span class="muted">（${bits.join(' · ')}）</span>，请核对后再保存`;
                U.toast(
                  aiN ? `已自动填入 ${filled.length} 项，其中 ${aiN} 项由模型补录，请重点核对` : `已自动填入 ${filled.length} 项，请核对`,
                  aiN ? 'warn' : 'success'
                );
              } else {
                hint.textContent = '未能识别出内容（可能是拍照件或扫描件），请手工填写';
                U.toast('未能识别出内容，请手工填写', 'warn');
              }
            } catch (err) {
              hint.textContent = `识别失败：${err.message || err}`;
            } finally {
              fileEl.value = '';
            }
          };
        }
        g('#v-amount').oninput = () => {
          const a = Number(g('#v-amount').value) || 0;
          const r = Number(g('#v-rate').value) || 0;
          if (a && r) g('#v-tax').value = ((a / (1 + r / 100)) * (r / 100)).toFixed(2);
        };
        g('[data-save]').onclick = async () => {
          const payload = {
            invoice_no: g('#v-no').value.trim(),
            invoice_code: g('#v-code').value.trim() || null,
            invoice_type: g('#v-type').value,
            amount: Number(g('#v-amount').value) || 0,
            tax_rate: Number(g('#v-rate').value) || 0,
            tax_amount: Number(g('#v-tax').value) || 0,
            invoice_date: g('#v-date').value || null,
            seller_name: g('#v-seller').value.trim() || null,
            seller_tax_no: g('#v-selltax').value.trim() || null,
            buyer_name: g('#v-buyer').value.trim() || null,
            buyer_tax_no: g('#v-buytax').value.trim() || null,
            category_id: g('#v-cat').value ? Number(g('#v-cat').value) : null,
            remark: g('#v-remark').value.trim() || null,
          };
          if (!payload.invoice_no) return U.toast('请填写发票号码', 'warn');
          if (!payload.amount) return U.toast('请填写价税合计金额', 'warn');
          try {
            if (isNew) await api.createInvoice(payload);
            else await api.updateInvoice(d.id, payload);
            U.toast('已保存', 'success');
            m.close();
            onDone();
          } catch (e) {}
        };
      },
    });
  }

  /* ---------------- 关联报销单 ---------------- */
  function openLink(inv, onDone) {
    let timer;
    const m = U.modal({
      title: `关联报销单 · 发票 ${inv.invoice_no}`,
      width: 680,
      body: `
        <div class="field" style="margin-bottom:10px">
          <input class="search-input" id="lk-q" style="width:100%" placeholder="输入单号 / 标题 / 申请人搜索">
        </div>
        <div id="lk-list" style="max-height:340px;overflow-y:auto"></div>`,
      footer: `<button class="btn" data-close>取消</button>`,
      onMount(mm) {
        const list = U.qs('#lk-list', mm.el);
        async function search() {
          const kw = U.qs('#lk-q', mm.el).value.trim();
          list.innerHTML = U.loading(1);
          const data = await api.reimbursements({ q: kw, page: 1, page_size: 30, sort: 'id_desc' });
          if (!data.items.length) {
            list.innerHTML = '<div class="empty">没有匹配的报销单</div>';
            return;
          }
          list.innerHTML = data.items
            .map(
              (r) => `<div class="alert-row" data-id="${r.id}" style="cursor:pointer">
              <span class="sev ${r.status === '草稿' ? 'low' : ''}"></span>
              <div class="grow"><div class="strong mono">${U.esc(r.code)} <span class="muted" style="font-weight:400">${U.esc(r.title)}</span></div>
                <div class="sub-line">${U.esc(r.applicant_name || '—')} · ${U.esc(r.department_name || '—')} · ${U.date(r.occur_start)}</div></div>
              <div class="num amount">${U.money(r.total_amount)}</div>
              ${U.badge(r.status)}
            </div>`
            )
            .join('');
        }
        U.qs('#lk-q', mm.el).oninput = () => {
          clearTimeout(timer);
          timer = setTimeout(search, 350);
        };
        list.addEventListener('click', async (e) => {
          const row = e.target.closest('[data-id]');
          if (!row) return;
          await api.linkInvoice(inv.id, { reimbursement_id: Number(row.dataset.id) });
          U.toast('已关联到报销单', 'success');
          mm.close();
          onDone();
        });
        search();
      },
    });
  }

  /* ---------------- 发票影像 ---------------- */
  const IMG_EXT = /\.(png|jpe?g|webp)$/i;

  function openAttachments(inv, onChanged) {
    const canWrite = WB.can('invoice.write');
    let blobs = [];
    const notify = () => {
      if (typeof onChanged === 'function') onChanged();
    };

    const m = U.modal({
      title: `发票影像 · ${inv.invoice_no}`,
      width: 760,
      body: `
        <div class="att-toolbar">
          ${canWrite
            ? `<label class="btn btn-sm btn-primary att-pick">+ 上传影像
                 <input type="file" id="att-file" accept=".pdf,.jpg,.jpeg,.png,.webp" hidden>
               </label>
               ${U.cameraBtn('#att-file', '拍照')}`
            : ''}
          <span class="muted" style="font-size:12px">支持 PDF / JPG / PNG / WEBP，单文件不超过 10MB</span>
        </div>
        <div id="att-list"><div class="empty"><span class="spin"></span>加载中…</div></div>`,
      footer: `<button class="btn" data-close>关闭</button>`,
      onMount(apiMod) {
        const listEl = U.qs('#att-list', apiMod.el);

        async function draw() {
          // 每次重绘都释放上一轮的 blob URL，避免内存泄漏
          blobs.forEach((u) => URL.revokeObjectURL(u));
          blobs = [];
          const list = await api.attachments(inv.id);
          if (!list.length) {
            listEl.innerHTML = '<div class="empty">还没有上传影像</div>';
            return;
          }
          listEl.innerHTML = `
            <table class="tbl">
              <thead><tr><th>文件名</th><th>类型</th><th class="num">大小</th><th>上传人</th><th>时间</th><th style="width:1%">操作</th></tr></thead>
              <tbody>
                ${list
                  .map(
                    (a) => `<tr>
                  <td class="ellipsis" title="${U.esc(a.filename)}">${U.esc(a.filename)}</td>
                  <td class="muted">${U.esc(a.mime || '—')}</td>
                  <td class="num">${(a.size / 1024).toFixed(1)} KB</td>
                  <td class="muted">${U.esc(a.uploaded_by || '—')}</td>
                  <td class="sub-line nowrap">${U.datetime(a.created_at)}</td>
                  <td><div class="row-actions">
                    <button class="btn btn-xs" data-view="${a.id}">预览</button>
                    <button class="btn btn-xs" data-dl="${a.id}">下载</button>
                    ${canWrite ? `<button class="btn btn-xs btn-danger" data-del="${a.id}">删除</button>` : ''}
                  </div></td>
                </tr>`
                  )
                  .join('')}
              </tbody>
            </table>`;

          // 影像接口需要鉴权，所以不能直接把 /raw 塞进 src，必须先取回 blob
          for (const a of list) {
            if (!IMG_EXT.test(a.filename)) continue;
            try {
              const url = await api.attachmentBlobUrl(a.id);
              blobs.push(url);
              const cell = listEl.querySelector(`[data-view="${a.id}"]`);
              if (cell) {
                cell.dataset.blob = url;
                cell.dataset.name = a.filename;
              }
            } catch (_) {}
          }
        }

        listEl.addEventListener('click', async (e) => {
          const view = e.target.closest('[data-view]');
          const dl = e.target.closest('[data-dl]');
          const del = e.target.closest('[data-del]');
          if (view) {
            const id = Number(view.dataset.view);
            const url = view.dataset.blob || (await api.attachmentBlobUrl(id));
            if (!view.dataset.blob) blobs.push(url);
            const a = document.createElement('a');
            a.href = url;
            a.target = '_blank';
            a.rel = 'noopener';
            document.body.appendChild(a);
            a.click();
            a.remove();
          } else if (dl) {
            const id = Number(dl.dataset.dl);
            const url = await api.attachmentBlobUrl(id);
            const a = document.createElement('a');
            a.href = url;
            a.download = '';
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 4000);
          } else if (del) {
            const ok = await U.confirm('确认删除该影像？删除后不可恢复。', { okText: '删除', danger: true });
            if (!ok) return;
            await api.deleteAttachment(Number(del.dataset.del));
            U.toast('已删除影像', 'success');
            notify();
            draw();
          }
        });

        const fileInput = U.qs('#att-file', apiMod.el);
        if (fileInput) {
          fileInput.onchange = async () => {
            const f = fileInput.files && fileInput.files[0];
            if (!f) return;
            if (f.size > 10 * 1024 * 1024) {
              return U.toast('文件超过 10MB 限制', 'warn');
            }
            try {
              await api.uploadAttachment(inv.id, f);
              U.toast('上传成功', 'success');
              notify();
              draw();
            } catch (_) {}
            fileInput.value = '';
          };
        }

        draw().catch((err) => {
          listEl.innerHTML = `<div class="empty">加载失败：${U.esc(err.message || err)}</div>`;
        });

        // 关闭时释放所有 blob，避免内存泄漏
        const origClose = apiMod.close;
        apiMod.close = function () {
          blobs.forEach((u) => URL.revokeObjectURL(u));
          blobs = [];
          origClose.call(apiMod);
        };
      },
    });
    return m;
  }

  /* ---------------- 主视图 ---------------- */
  WB.views.invoices = async function (root) {
    const meta = WB.meta;

    root.innerHTML = `
      <div class="kpi-grid" id="iv-kpi"></div>
      <div class="chart-grid g3">
        <div class="card">
          <div class="card-head"><h3>发票类型分布</h3><span class="hint">按金额</span></div>
          <div class="card-body"><div class="chart sm" id="c-inv-type"></div></div>
        </div>
        <div class="card">
          <div class="card-head"><h3>查验状态分布</h3><span class="hint">按张数</span></div>
          <div class="card-body"><div class="chart sm" id="c-inv-check"></div></div>
        </div>
        <div class="card">
          <div class="card-head"><h3>月度开票金额</h3><span class="hint">近 6 个月</span></div>
          <div class="card-body"><div class="chart sm" id="c-inv-month"></div></div>
        </div>
      </div>
      <div class="card">
        <div class="tabs" id="iv-tabs">
          <div class="tab ${state.tab === 'ledger' ? 'active' : ''}" data-tab="ledger">发票台账 <span class="cnt" id="tab-cnt"></span></div>
          <div class="tab ${state.tab === 'dup' ? 'active' : ''}" data-tab="dup">重复报销检测 <span class="cnt" id="tab-dup"></span></div>
        </div>
        <div id="iv-body"></div>
      </div>`;

    const bodyHost = U.qs('#iv-body', root);

    /* --- 汇总 + 图表 --- */
    async function loadSummary() {
      const s = await api.invoiceSummary();
      U.qs('#iv-kpi', root).innerHTML = `
        <div class="kpi"><div class="kpi-label">发票总金额</div><div class="kpi-value">${U.moneyShort(s.total_amount)}</div><div class="kpi-sub">共 ${U.num(s.total_count)} 张 · 税额 ${U.moneyShort(s.total_tax)}</div></div>
        <div class="kpi k-orange"><div class="kpi-label">待关联报销单</div><div class="kpi-value">${U.num(s.unlinked_count)}</div><div class="kpi-sub">尚未挂到任何报销单</div></div>
        <div class="kpi k-red"><div class="kpi-label">问题发票</div><div class="kpi-value">${U.num(s.duplicate_count)}</div><div class="kpi-sub">重复号码组，存在重复报销风险</div></div>
        <div class="kpi k-green"><div class="kpi-label">已查验</div><div class="kpi-value">${U.num((s.by_status.find((x) => x.name === '已查验') || {}).count || 0)}</div><div class="kpi-sub">基础校验通过</div></div>`;
      U.qs('#tab-cnt', root).textContent = s.total_count;
      U.qs('#tab-dup', root).textContent = s.duplicate_count;

      C.donut(U.qs('#c-inv-type', root), s.by_type.slice(0, 8), { unit: '金额' });
      C.donut(U.qs('#c-inv-check', root), s.by_status.map((x) => ({ name: x.name, value: x.count })), { unit: '张数', valueKey: 'value' });
      return s;
    }

    async function loadMonthTrend() {
      const t = await api.invoiceMonthly({ months: 6 });
      const now = new Date();
      const curKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
      C.set(U.qs('#c-inv-month', root), {
        tooltip: {
          trigger: 'axis',
          backgroundColor: 'rgba(255,255,255,.97)', borderColor: '#e7ebf2',
          textStyle: { color: '#1b2430', fontSize: 12 },
          axisPointer: { type: 'line', lineStyle: { color: 'rgba(22,119,255,.35)' } },
          formatter: (ps) => {
            const r = t[ps[0].dataIndex];
            const tail = r.month === curKey ? '（本月进行中）' : '';
            return `<b>${r.month}</b>${tail}<br/>开票金额：<b>${U.money(r.amount)}</b><br/>税额：${U.money(r.tax)}<br/>发票张数：${r.count} 张`;
          },
        },
        grid: { left: 6, right: 10, top: 20, bottom: 4, containLabel: true },
        xAxis: {
          type: 'category', data: t.map((x) => x.month.slice(2)),
          axisLine: { lineStyle: { color: '#e7ebf2' } }, axisTick: { show: false },
          axisLabel: {
            color: '#7a8699', fontSize: 11,
            formatter: (v, i) => (t[i] && t[i].month === curKey ? `{cur|${v}}` : v),
            rich: { cur: { color: '#1677ff', fontSize: 11, fontWeight: 600 } },
          },
        },
        yAxis: {
          type: 'value', splitLine: { lineStyle: { color: '#eef1f6' } }, axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { color: '#7a8699', fontSize: 11, formatter: (v) => U.moneyShort(v) },
        },
        series: [
          {
            type: 'line', smooth: true, symbolSize: 7, data: t.map((x) => x.amount),
            areaStyle: {
              color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                { offset: 0, color: 'rgba(22,119,255,.28)' },
                { offset: 1, color: 'rgba(22,119,255,.02)' },
              ]),
            },
            lineStyle: { width: 2.4, color: '#1677ff' }, itemStyle: { color: '#1677ff' },
          },
        ],
      });
    }

    /* --- 批量操作条 --- */
    function batchBarHTML() {
      if (!WB.can('invoice.write')) return '';
      return `
        <div class="batch-bar" id="iv-batch-bar" style="display:none">
          已选 <b id="iv-picked">0</b> 张发票
          <span class="muted">批量删除会连已上传的影像文件一并清理</span>
          <div class="spacer"></div>
          <button class="btn btn-sm" id="iv-ai-book">AI 记账草稿</button>
          <button class="btn btn-sm btn-danger" id="iv-del-selected">批量删除</button>
          <button class="btn btn-sm" id="iv-purge-unlinked">清空待关联散票</button>
          ${
            WB.can('admin.audit')
              ? '<button class="btn btn-sm btn-danger" id="iv-purge-all">清空全部发票</button>'
              : ''
          }
        </div>`;
    }

    function syncBatchBar() {
      const bar = U.qs('#iv-batch-bar', bodyHost);
      if (!bar) return;
      const ids = U.pickedIds(bodyHost);
      U.qs('#iv-picked', bar).textContent = ids.length;
      bar.style.display = ids.length ? '' : 'none';
      U.qsa('tr[data-id]', bodyHost).forEach((tr) => {
        const c = U.qs('.ck-row', tr);
        tr.classList.toggle('picked', !!(c && c.checked));
      });
    }

    async function batchDeleteInvoices() {
      const ids = U.pickedIds(bodyHost);
      if (!ids.length) return U.toast('请先勾选要删除的发票', 'warn');
      const ok = await U.dangerConfirm(
        `即将删除 <b>${ids.length}</b> 张发票记录，连同已上传的影像文件一并清理。` +
          `已被报销单引用的发票会先解绑再删除，不会影响报销单本身。`,
        '删除发票',
        { okText: `删除 ${ids.length} 张` }
      );
      if (!ok) return;
      const r = await api.batchDeleteInvoices(ids);
      U.toast(U.batchResult(r, '张'), r.skipped_count ? 'warn' : 'success', 4200);
      if (r.skipped_count) {
        const why = U.skipReasons(r);
        if (why) console.warn('[批量删除] 跳过原因：', why);
      }
      await loadSummary();
      await loadBody();
    }

    /* --- AI 记账：把勾选的散票组织成报销单草稿 ---
     * 模型只出方案，落库仍然走常规的「创建报销单 + 关联发票」接口，
     * 因此权限、状态机、审计都与手工建单完全一致。
     */
    async function aiBookkeeping() {
      const ids = U.pickedIds(bodyHost);
      if (!ids.length) return U.toast('请先勾选要记账的发票', 'warn');

      const m = U.modal({
        title: 'AI 记账草稿',
        width: 880,
        body: '<div class="empty" style="padding:32px"><span class="spin"></span>正在让模型读这几张发票并组织报销单…</div>',
        footer: `<span class="muted" style="font-size:12px">模型只给方案，点「按此建单」才会真正写入。</span>
                 <div class="spacer"></div><button class="btn" data-close>关闭</button>`,
      });
      const box = U.qs('.modal-body', m.el);

      let data;
      try {
        data = await api.aiBookkeeping({ invoice_ids: ids });
      } catch (e) {
        box.innerHTML = `<div class="empty">生成失败：${U.esc(e.message || e)}</div>`;
        return;
      }

      if (!data.drafts.length) {
        box.innerHTML = `<div class="empty">模型没能把所选发票组织成报销单。
          ${data.skipped && data.skipped.length ? `<br>被跳过的发票：${U.esc(data.skipped.join('、'))}` : ''}</div>`;
        return;
      }

      box.innerHTML = `
        <div class="recog-note">共处理 <b>${data.invoice_count}</b> 张散票，组织成 <b>${data.drafts.length}</b> 张报销单草稿。
          模型 <code class="mono">${U.esc(data.model || '-')}</code>。${U.esc(data.disclaimer || '')}</div>
        ${data.drafts
          .map(
            (dr, i) => `<div class="ai-prov" style="margin-bottom:12px">
          <div class="ai-prov-head">
            <b>${U.esc(dr.title || '未命名报销单')}</b>
            <span class="badge b-blue">${U.money(dr.total_amount)}</span>
            <span class="right"><button class="btn btn-sm btn-primary" data-build="${i}">按此建单</button></span>
          </div>
          <div class="ai-prov-meta">${U.esc(dr.purpose || '（无说明）')}</div>
          <div class="table-wrap" style="margin-top:8px">
            <table class="tbl">
              <thead><tr><th>发票号</th><th>销售方</th><th>费用类型</th><th>发生日期</th><th class="num">税额</th><th class="num">金额</th></tr></thead>
              <tbody>${(dr.items || [])
                .map((it) => {
                  const inv = (dr.invoices || []).find((x) => x.id === it.invoice_id) || {};
                  const cat = (WB.meta.categories || []).find((c) => c.id === it.category_id);
                  return `<tr>
                    <td class="mono">${U.esc(inv.invoice_no || '#' + it.invoice_id)}</td>
                    <td class="ellipsis muted">${U.esc(inv.seller_name || '—')}</td>
                    <td>${U.esc(cat ? cat.name : it.category_id ? '#' + it.category_id : '未分类')}</td>
                    <td class="nowrap">${U.date(it.occur_date)}</td>
                    <td class="num muted">${U.money(it.tax_amount)}</td>
                    <td class="num amount">${U.money(it.amount)}</td>
                  </tr>`;
                })
                .join('')}</tbody>
            </table>
          </div>
        </div>`
          )
          .join('')}
        ${data.skipped && data.skipped.length
          ? `<div class="mail-hint">模型跳过了这些发票（信息不足或与本次无关）：${U.esc(data.skipped.join('、'))}</div>`
          : ''}`;

      U.qsa('[data-build]', box).forEach((b) => {
        b.onclick = async () => {
          const dr = data.drafts[Number(b.dataset.build)];
          const ok = await U.confirm(
            `将创建报销单「${U.esc(dr.title || '')}」，金额 ${U.money(dr.total_amount)}，` +
              `并关联 ${dr.invoices.length} 张发票。`,
            { title: '确认建单', okText: '创建' }
          );
          if (!ok) return;
          b.disabled = true;
          b.textContent = '创建中…';
          try {
            const r = await api.createReimbursement({
              title: dr.title || 'AI 记账',
              purpose: dr.purpose || null,
              department_id: dr.department_id ?? null,
              customer_id: dr.customer_id ?? null,
              project_id: dr.project_id ?? null,
              items: (dr.items || []).map((it) => ({
                category_id: it.category_id ?? null,
                occur_date: it.occur_date || U.today(),
                amount: Number(it.amount || 0),
                tax_amount: Number(it.tax_amount || 0),
                description: it.description || '',
              })),
            });
            for (const inv of dr.invoices || []) {
              await api.linkInvoice(inv.id, { reimbursement_id: r.id });
            }
            U.toast(`已创建 ${r.code}（${U.money(r.total_amount)}），可在「报销单管理」继续编辑`, 'success', 5000);
            m.close();
            await loadSummary();
            await loadBody();
          } catch (e) {
            b.disabled = false;
            b.textContent = '按此建单';
          }
        };
      });
    }

    async function purgeInvoices(onlyUnlinked) {
      const keyword = onlyUnlinked ? '清空散票' : '清空全部发票';
      const ok = await U.dangerConfirm(
        onlyUnlinked
          ? '即将删除<b>全部未关联报销单的散票</b>，已关联的发票会保留。'
          : '即将删除<b>全部发票记录</b>（含已关联报销单的），并清理所有影像文件。报销单本身不受影响。',
        keyword,
        { okText: onlyUnlinked ? '清空散票' : '清空全部发票' }
      );
      if (!ok) return;
      const r = await api.purgeInvoices(keyword, onlyUnlinked ? { only_unlinked: true } : undefined);
      U.toast(U.batchResult(r, '张'), 'success', 4200);
      await loadSummary();
      await loadBody();
    }

    /* --- 台账表 --- */
    let lastRows = []; // 当前台账展示的行：打印清单用（勾选优先，未勾选打整页）
    async function loadLedger() {
      bodyHost.innerHTML = `<div class="table-wrap">${U.loading(9)}</div>`;
      const data = await api.invoices(params());
      const rows = data.items;
      lastRows = rows;

      bodyHost.innerHTML = `
        ${batchBarHTML()}
        <div class="table-wrap">
          ${
            rows.length
              ? `<table class="tbl tbl-pin-actions">
            <thead><tr>
              <th style="width:34px">${WB.can('invoice.write') ? '<input type="checkbox" id="ck-all">' : ''}</th>
              <th>发票号码 / 代码</th><th>类型</th><th>开票日期</th><th>销售方</th>
              <th class="num">价税合计</th><th class="num">税额</th><th>查验状态</th><th>关联单号</th><th style="width:1%">操作</th>
            </tr></thead>
            <tbody>
              ${rows
                .map(
                  (v) => `<tr data-id="${v.id}">
                <td>${WB.can('invoice.write') ? `<input type="checkbox" class="ck-row" value="${v.id}">` : ''}</td>
                <td><div class="mono strong">${U.esc(v.invoice_no)}</div>${v.invoice_code ? `<div class="sub-line mono">${U.esc(v.invoice_code)}</div>` : ''}</td>
                <td class="nowrap">${U.esc(v.invoice_type)}${v.category_name ? `<br><span class="muted">${U.esc(v.category_name)}</span>` : ''}</td>
                <td class="nowrap">${U.date(v.invoice_date)}</td>
                <td class="ellipsis" title="${U.esc(v.seller_name || '')}">${U.esc(v.seller_name || '—')}</td>
                <td class="num amount">${U.money(v.amount)}</td>
                <td class="num muted">${U.money(v.tax_amount)}</td>
                <td>${U.badge(v.check_status)}${v.check_result ? `<div class="sub-line ellipsis" title="${U.esc(v.check_result)}">${U.esc(v.check_result)}</div>` : ''}</td>
                <td>${v.reimbursement_code ? `<span class="chip">${U.esc(v.reimbursement_code)}</span>` : '<span class="badge b-orange">待关联</span>'}</td>
                <td>${WB.can('invoice.write')
                  ? `<div class="row-actions">
                  <button class="btn btn-xs" data-act="att">影像</button>
                  <button class="btn btn-xs" data-act="check">查验</button>
                  <button class="btn btn-xs" data-act="link">${v.reimbursement_code ? '改关联' : '关联'}</button>
                  <button class="btn btn-xs" data-act="edit">编辑</button>
                  <button class="btn btn-xs" data-act="print">打印</button>
                  <button class="btn btn-xs btn-danger" data-act="del">删除</button>
                </div>`
                  : `<div class="row-actions">
                  <button class="btn btn-xs" data-act="att">影像</button>
                  <button class="btn btn-xs" data-act="print">打印</button>
                </div>`}</td>
              </tr>`
                )
                .join('')}
            </tbody>
          </table>`
              : U.emptyRow(10, '没有符合条件的发票')
          }
        </div>
        ${U.pager(data.total, data.page, data.page_size)}`;

      U.bindCheckAll(bodyHost);
      syncBatchBar();
    }

    /* --- 重复检测 --- */
    async function loadDup() {
      bodyHost.innerHTML = U.loading(1);
      const s = await api.invoiceSummary();
      if (!s.duplicates.length) {
        bodyHost.innerHTML = '<div class="empty">未发现重复发票号码，账目干净 👍</div>';
        return;
      }
      bodyHost.innerHTML = `
        <div class="card-body tight" style="border-bottom:1px solid var(--border);background:var(--panel-2)">
          <span class="muted" style="font-size:12.5px">共发现 <b style="color:#f53f3f">${s.duplicates.length}</b> 组发票号码重复，涉及金额 ${U.money(
        s.duplicates.reduce((a, b) => a + b.amount, 0)
      )}。请逐组核对是否为重复报销。</span>
        </div>
        <div class="table-wrap">
          <table class="tbl">
            <thead><tr><th style="width:190px">发票号码</th><th style="width:150px">发票代码</th><th class="num">重复次数</th><th class="num">涉及金额</th><th>明细</th></tr></thead>
            <tbody>
              ${s.duplicates
                .map(
                  (g) => `<tr>
                <td class="mono strong">${U.esc(g.invoice_no)}</td>
                <td class="mono muted">${U.esc(g.invoice_code || '—')}</td>
                <td class="num"><span class="badge b-red">${g.count} 次</span></td>
                <td class="num amount">${U.money(g.amount)}</td>
                <td>
                  <div style="display:flex;flex-wrap:wrap;gap:6px">
                    ${g.records
                      .map(
                        (x) => `<span class="chip" title="${U.esc(x.invoice_date || '')} ${U.esc(
                          x.reimbursement_code || '未关联'
                        )}">
                        ${U.date(x.invoice_date)} · ${U.money(x.amount)} · ${U.esc(x.reimbursement_code || '未关联')}
                      </span>`
                      )
                      .join('')}
                  </div>
                </td>
              </tr>`
                )
                .join('')}
            </tbody>
          </table>
        </div>`;
    }

    async function loadBody() {
      if (state.tab === 'ledger') await loadLedger();
      else await loadDup();
    }

    U.qs('#iv-tabs', root).addEventListener('click', (e) => {
      const t = e.target.closest('.tab');
      if (!t) return;
      state.tab = t.dataset.tab;
      state.page = 1;
      // 工具条内容随页签变化（台账有筛选/登记，重复检测只有重新扫描），
      // 只切样式不重渲染会让上一页签的按钮残留在工具条上。
      WB.rerender();
    });

    // 勾选变化 -> 刷新批量条（委托到 bodyHost，子表重绘后依然有效）
    bodyHost.addEventListener('change', (e) => {
      if (e.target.classList.contains('ck-row') || e.target.id === 'ck-all') syncBatchBar();
    });

    bodyHost.addEventListener('click', async (e) => {
      // 批量操作按钮
      if (e.target.closest('#iv-del-selected')) return batchDeleteInvoices();
      if (e.target.closest('#iv-ai-book')) return aiBookkeeping();
      if (e.target.closest('#iv-purge-unlinked')) return purgeInvoices(true);
      if (e.target.closest('#iv-purge-all')) return purgeInvoices(false);

      const pg = e.target.closest('button[data-page]');
      if (pg && !pg.disabled) {
        state.page = Number(pg.dataset.page);
        return loadLedger();
      }
      const b = e.target.closest('button[data-act]');
      if (!b) return;
      const tr = b.closest('tr[data-id]');
      const id = Number(tr.dataset.id);
      const act = b.dataset.act;
      if (act === 'print') {
        const v = lastRows.find((x) => Number(x.id) === id);
        if (v) WB.print.invoice(v);
        return;
      }
      const row = (await api.invoice(id));
      if (act === 'att') {
        return openAttachments(row, () => {
          loadSummary();
          loadBody();
        });
      } else if (act === 'check') {
        const r = await api.checkInvoice(id);
        U.toast(`查验结果：${r.check_status} — ${r.check_result || ''}`, r.check_status === '异常' ? 'error' : 'success', 4200);
        loadSummary();
        loadBody();
      } else if (act === 'edit') {
        openForm(row, meta, () => {
          loadSummary();
          loadBody();
        });
      } else if (act === 'link') {
        openLink(row, () => {
          loadSummary();
          loadBody();
        });
      } else if (act === 'del') {
        const ok = await U.confirm(`确认删除发票 <b>${U.esc(row.invoice_no)}</b>（${U.money(row.amount)}）？`, {
          title: '删除发票', okText: '删除', danger: true,
        });
        if (!ok) return;
        await api.deleteInvoice(id);
        U.toast('已删除', 'success');
        loadSummary();
        loadBody();
      }
    });

    await Promise.all([loadSummary(), loadMonthTrend()]);
    await loadBody();

    return {
      title: '发票管理',
      sub: '票据台账、查验与重复报销防控',
      toolbar: `
        ${
          state.tab === 'ledger'
            ? `<input class="search-input" id="f-q" placeholder="号码 / 代码 / 销售方" value="${U.esc(state.q)}">
        <div class="field"><label>查验状态</label>
          <select id="f-check">
            <option value="">全部</option>
            ${['未查验', '已查验', '异常'].map((s) => `<option value="${s}" ${state.check_status === s ? 'selected' : ''}>${s}</option>`).join('')}
          </select></div>
        <div class="field"><label>类型</label>
          <select id="f-type"><option value="">全部</option>
            ${meta.invoice_types.map((t) => `<option value="${t}" ${state.invoice_type === t ? 'selected' : ''}>${t}</option>`).join('')}
          </select></div>
        <label class="field" style="cursor:pointer"><input type="checkbox" id="f-unlinked" ${state.unlinked ? 'checked' : ''}> 只看待关联</label>
        <div class="field"><label>开票日期</label>
          <input type="date" id="f-from" value="${state.date_from}"> <span class="muted">~</span>
          <input type="date" id="f-to" value="${state.date_to}"></div>
        <button class="btn btn-sm" id="f-reset">重置</button>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="btn-print">打印清单</button>
        ${WB.can('invoice.write') ? '<button class="btn btn-sm" id="btn-batch">批量查验</button>' : ''}
        <button class="btn btn-sm" id="btn-export">导出 CSV</button>
        ${WB.can('invoice.write') ? '<button class="btn btn-sm btn-primary" id="btn-new">+ 登记发票</button>' : ''}`
            : `<span class="muted" style="font-size:12.5px">重复报销检测会扫描全部发票号码，无需筛选条件。</span>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="btn-refresh">重新扫描</button>`
        }`,
      onToolbar(tb) {
        const reload = () => {
          state.page = 1;
          loadBody();
        };
        if (state.tab === 'dup') {
          U.qs('#btn-refresh', tb).onclick = () => {
            loadSummary();
            loadBody();
          };
          return;
        }
        U.qs('#f-q', tb).oninput = U.debounce((e) => {
          state.q = e.target.value.trim();
          reload();
        }, 400);
        U.qs('#f-check', tb).onchange = (e) => {
          state.check_status = e.target.value;
          reload();
        };
        U.qs('#f-type', tb).onchange = (e) => {
          state.invoice_type = e.target.value;
          reload();
        };
        U.qs('#f-unlinked', tb).onchange = (e) => {
          state.unlinked = e.target.checked;
          reload();
        };
        ['#f-from', '#f-to'].forEach((s, i) => {
          U.qs(s, tb).onchange = (e) => {
            state[i === 0 ? 'date_from' : 'date_to'] = e.target.value;
            reload();
          };
        });
        U.qs('#f-reset', tb).onclick = () => {
          Object.assign(state, { q: '', check_status: '', invoice_type: '', unlinked: false, date_from: '', date_to: '', page: 1 });
          WB.rerender();
        };
        const newBtn = U.qs('#btn-new', tb);
        if (newBtn) newBtn.onclick = () =>
          openForm(null, meta, () => {
            loadSummary();
            loadBody();
          });
        const batchBtn = U.qs('#btn-batch', tb);
        if (batchBtn) batchBtn.onclick = async () => {
          const ids = U.qsa('.ck-row', bodyHost).filter((c) => c.checked).map((c) => Number(c.value));
          const r = await api.batchCheck(ids.length ? ids : null);
          U.toast(`已查验 ${r.checked} 张：通过 ${r.ok}，异常 ${r.bad}`, r.bad ? 'warn' : 'success', 4000);
          loadSummary();
          loadBody();
        };
        U.qs('#btn-export', tb).onclick = () => {
          const q = {};
          Object.entries(params(false)).forEach(([k, v]) => {
            if (v !== '' && v !== null && v !== false) q[k] = v;
          });
          api.downloadExport('invoices.csv', q).catch(() => {});
        };
        // 打印清单：勾选了就打勾选的，否则打当前筛选结果的整页
        U.qs('#btn-print', tb).onclick = () => {
          const checked = new Set(
            U.qsa('.ck-row', bodyHost).filter((c) => c.checked).map((c) => Number(c.value))
          );
          const rows = checked.size ? lastRows.filter((v) => checked.has(Number(v.id))) : lastRows;
          if (!rows.length) return U.toast('当前没有可打印的发票', 'warn');
          const where = checked.size ? `勾选 ${rows.length} 张` : `当前筛选结果 ${rows.length} 张（第 ${state.page} 页）`;
          WB.print.invoices(rows, where);
        };
      },
    };
  };

  /* v2.8.0：供「扫码核验」等外部入口直接打开发票详情（复用发票编辑表单） */
  WB.openInvoice = async (id) => openForm(await api.invoice(id), WB.meta);
})();
