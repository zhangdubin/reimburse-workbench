/* 视图：AI 设置（管理员）
 *
 * 为什么单独一个页面
 * ----------------
 * 模型接入牵扯 API Key、代理、超时这些运维细节，塞进「系统参数」那张
 * 审批阈值表里会互相干扰。这里集中管理：厂商预设、凭据、能力开关、
 * 连通性测试、用量与成本。
 *
 * Key 的处理：明文只在提交时上行一次，后端加密落库，回显永远只有掩码。
 * 「留空 = 不修改，填空串 = 清空」这条语义由后端 ProviderIn.api_key 定义，
 * 前端提交时必须把「用户没动过」和「用户想清空」区分开，别一律送 null。
 */
window.WB = window.WB || {};
WB.views = WB.views || {};

(function () {
  const U = WB.util;
  const api = WB.api;

  const AUTH_STYLES = [
    { v: 'bearer', label: 'Authorization: Bearer（绝大多数厂商）' },
    { v: 'api-key', label: 'api-key 请求头（Azure OpenAI 等）' },
    { v: 'none', label: '不带凭据（本地 Ollama / 内网网关）' },
  ];

  const KIND_LABEL = {
    助手对话: '助手对话', 识别增强: '识别增强', 报销单分析: '报销单分析',
    审批建议: '审批建议', AI记账: 'AI 记账', 连通性测试: '连通性测试',
  };

  let presets = [];

  function presetByKey(key) {
    return presets.find((p) => p.key === key) || null;
  }

  function cap(label, on) {
    return `<span class="ai-cap ${on ? 'on' : 'off'}">${U.esc(label)}</span>`;
  }

  /* ------------------------------------------------------------ 编辑弹窗 */
  function openEditor(row) {
    const isNew = !row;
    const cur = row || {
      name: '', preset: 'custom', base_url: '', model: '', vision_model: '',
      auth_style: 'bearer', extra_headers: '', extra_query: '',
      temperature: 0.3, max_tokens: 2048, timeout_sec: 60,
      use_assistant: true, use_recognize: true, use_analyze: true,
      enabled: true, is_default: false, remark: '', key_mask: '', has_key: false,
    };

    const m = U.modal({
      title: isNew ? '接入大模型' : `配置：${cur.name}`,
      width: 760,
      body: `
        <div class="form-section">选择厂商（一键填充地址与模型）</div>
        <div class="ai-preset-grid" id="pv-presets">
          ${presets
            .map(
              (p) => `<button type="button" class="ai-preset ${p.key === cur.preset ? 'on' : ''}"
                data-pk="${U.esc(p.key)}" title="${U.esc(p.note || '')}">${U.esc(p.label)}</button>`
            )
            .join('')}
        </div>
        <div class="form-item hint" id="pv-note" style="margin:10px 0 4px">
          ${U.esc((presetByKey(cur.preset) || {}).note || '')}
        </div>

        <div class="form-section">连接参数</div>
        <div class="form-grid">
          <div class="form-item">
            <label>配置名称<span class="req">*</span></label>
            <input id="pv-name" value="${U.esc(cur.name)}" placeholder="如：通义千问-生产" maxlength="64">
          </div>
          <div class="form-item full">
            <label>接口地址 Base URL<span class="req">*</span></label>
            <input id="pv-url" value="${U.esc(cur.base_url)}" placeholder="https://api.deepseek.com/v1">
            <div class="hint">可填到域名、/v1 或完整 /chat/completions，系统会自动补齐。</div>
          </div>
          <div class="form-item full">
            <label>API Key</label>
            <input id="pv-key" type="password" autocomplete="off"
              placeholder="${cur.has_key ? '已配置（' + U.esc(cur.key_mask) + '），留空则不修改' : '粘贴厂商控制台里的 Key'}">
            <div class="hint">加密存储，保存后不再回显明文。想清空请勾选下方「清除已保存的 Key」。</div>
          </div>
          <div class="form-item full">
            <label class="switch-row"><input type="checkbox" id="pv-clear-key"><span>清除已保存的 Key</span></label>
          </div>
          <div class="form-item">
            <label>认证方式</label>
            <select id="pv-auth">
              ${AUTH_STYLES.map(
                (a) => `<option value="${a.v}" ${a.v === cur.auth_style ? 'selected' : ''}>${U.esc(a.label)}</option>`
              ).join('')}
            </select>
          </div>
          <div class="form-item">
            <label>文本模型<span class="req">*</span></label>
            <input id="pv-model" value="${U.esc(cur.model)}" placeholder="如 qwen-plus" list="pv-model-list">
            <datalist id="pv-model-list"></datalist>
          </div>
          <div class="form-item">
            <label>视觉模型（选填）</label>
            <input id="pv-vmodel" value="${U.esc(cur.vision_model || '')}" placeholder="如 qwen-vl-max" list="pv-vmodel-list">
            <datalist id="pv-vmodel-list"></datalist>
            <div class="hint">填了才能用「读发票图片」这条识别增强链路。</div>
          </div>
          <div class="form-item">
            <label>超时（秒）</label>
            <input id="pv-timeout" type="number" min="5" max="600" value="${cur.timeout_sec}">
          </div>
          <div class="form-item">
            <label>温度（0-2）</label>
            <input id="pv-temp" type="number" min="0" max="2" step="0.1" value="${cur.temperature}">
            <div class="hint">识别与审批建议建议 0～0.3，越低越稳定。</div>
          </div>
          <div class="form-item">
            <label>单次最大输出 tokens</label>
            <input id="pv-maxtok" type="number" min="16" max="32000" value="${cur.max_tokens}">
          </div>
          <div class="form-item full">
            <label>附加请求头（JSON，选填）</label>
            <textarea id="pv-headers" rows="2" placeholder='{"X-Custom": "value"}'>${U.esc(cur.extra_headers || '')}</textarea>
          </div>
          <div class="form-item full">
            <label>附加查询参数（JSON，选填）</label>
            <textarea id="pv-query" rows="2" placeholder='{"api-version": "2024-10-21"}'>${U.esc(cur.extra_query || '')}</textarea>
          </div>
        </div>

        <div class="form-section">启用范围</div>
        <div class="form-grid">
          <div class="form-item"><label class="switch-row"><input type="checkbox" id="pv-u-assist" ${cur.use_assistant ? 'checked' : ''}><span>用于助手对话</span></label></div>
          <div class="form-item"><label class="switch-row"><input type="checkbox" id="pv-u-recog" ${cur.use_recognize ? 'checked' : ''}><span>用于识别增强</span></label></div>
          <div class="form-item"><label class="switch-row"><input type="checkbox" id="pv-u-analyze" ${cur.use_analyze ? 'checked' : ''}><span>用于分析与审批建议</span></label></div>
          <div class="form-item"><label class="switch-row"><input type="checkbox" id="pv-enabled" ${cur.enabled ? 'checked' : ''}><span>启用该配置</span></label></div>
          <div class="form-item"><label class="switch-row"><input type="checkbox" id="pv-default" ${cur.is_default ? 'checked' : ''}><span>设为默认（同场景二选一时用）</span></label></div>
          <div class="form-item full">
            <label>备注</label>
            <input id="pv-remark" value="${U.esc(cur.remark || '')}" placeholder="如：财务专用、仅工作日可用">
          </div>
        </div>
        <div id="pv-test" class="ai-test-line"></div>`,
      footer: isNew
        ? `<button class="btn" data-close>取消</button>
           <button class="btn btn-primary" id="pv-save">保存</button>`
        : `<button class="btn btn-danger" id="pv-del">删除</button>
           <div class="spacer"></div>
           <button class="btn" id="pv-test-btn">测试连通性</button>
           <button class="btn" data-close>取消</button>
           <button class="btn btn-primary" id="pv-save">保存</button>`,
      onMount(apiMod) {
        const el = apiMod.el;
        const q = (s) => U.qs(s, el);

        /* 预设联动：填地址、模型候选、认证方式、附加查询参数 */
        function applyPreset(key) {
          const p = presetByKey(key);
          U.qsa('.ai-preset', el).forEach((b) => b.classList.toggle('on', b.dataset.pk === key));
          if (!p) return;
          q('#pv-note').textContent = p.note || '';
          if (p.base_url) q('#pv-url').value = p.base_url;
          if (p.default_model) q('#pv-model').value = p.default_model;
          if (p.auth_style) q('#pv-auth').value = p.auth_style;
          if (p.query) q('#pv-query').value = JSON.stringify(p.query, null, 2);
          if (p.key && !q('#pv-name').value.trim()) q('#pv-name').value = p.label;
          q('#pv-model-list').innerHTML = (p.models || []).map((x) => `<option value="${U.esc(x)}">`).join('');
          q('#pv-vmodel-list').innerHTML = (p.vision_models || [])
            .map((x) => `<option value="${U.esc(x)}">`).join('');
          if (p.vision_models && p.vision_models.length && !q('#pv-vmodel').value) {
            q('#pv-vmodel').value = p.vision_models[0];
          }
        }
        U.qsa('.ai-preset', el).forEach((b) => {
          b.onclick = () => applyPreset(b.dataset.pk);
        });
        // 打开时把当前预设的模型候选也铺上
        applyPreset(cur.preset);
        if (cur.base_url) q('#pv-url').value = cur.base_url;
        if (cur.model) q('#pv-model').value = cur.model;
        if (cur.vision_model) q('#pv-vmodel').value = cur.vision_model;
        q('#pv-auth').value = cur.auth_style || 'bearer';

        function collect() {
          const json = (sel, label) => {
            const raw = q(sel).value.trim();
            if (!raw) return null;
            try {
              const v = JSON.parse(raw);
              if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('x');
              return v;
            } catch (_) {
              throw new Error(`${label} 不是合法的 JSON 对象`);
            }
          };
          const name = q('#pv-name').value.trim();
          const baseUrl = q('#pv-url').value.trim();
          const model = q('#pv-model').value.trim();
          if (!name) throw new Error('请填写配置名称');
          if (!baseUrl) throw new Error('请填写接口地址');
          if (!model) throw new Error('请填写文本模型名');
          const body = {
            name,
            preset: (U.qs('.ai-preset.on', el) || {}).dataset
              ? U.qs('.ai-preset.on', el).dataset.pk
              : 'custom',
            base_url: baseUrl,
            model,
            vision_model: q('#pv-vmodel').value.trim() || null,
            auth_style: q('#pv-auth').value,
            extra_headers: json('#pv-headers', '附加请求头'),
            extra_query: json('#pv-query', '附加查询参数'),
            temperature: Number(q('#pv-temp').value || 0.3),
            max_tokens: Number(q('#pv-maxtok').value || 2048),
            timeout_sec: Number(q('#pv-timeout').value || 60),
            use_assistant: q('#pv-u-assist').checked,
            use_recognize: q('#pv-u-recog').checked,
            use_analyze: q('#pv-u-analyze').checked,
            enabled: q('#pv-enabled').checked,
            is_default: q('#pv-default').checked,
            remark: q('#pv-remark').value.trim() || null,
          };
          // 三态：不传（留空且没勾清除）= 保持原 Key；'' = 清空；字符串 = 更新
          const typed = q('#pv-key').value;
          const clear = q('#pv-clear-key').checked;
          if (clear) body.api_key = '';
          else if (typed) body.api_key = typed;
          return body;
        }

        q('#pv-save').onclick = async () => {
          let body;
          try {
            body = collect();
          } catch (e) {
            return U.toast(e.message, 'warn');
          }
          const btn = q('#pv-save');
          btn.disabled = true;
          try {
            if (isNew) await api.post('/api/ai/providers', body);
            else await api.put(`/api/ai/providers/${cur.id}`, body);
            U.toast('已保存', 'success');
            apiMod.close();
            WB.rerender();
          } catch (e) {
          } finally {
            btn.disabled = false;
          }
        };

        if (!isNew) {
          q('#pv-test-btn').onclick = async () => {
            const box = q('#pv-test');
            const btn = q('#pv-test-btn');
            btn.disabled = true;
            box.className = 'ai-test-line';
            box.innerHTML = '<span class="spin"></span>正在测试，最多等一会儿…';
            try {
              // 带上当前表单里的地址/Key 一起测，省得「先保存再测」来回两趟
              const payload = {};
              if (q('#pv-url').value.trim()) payload.base_url = q('#pv-url').value.trim();
              if (q('#pv-model').value.trim()) payload.model = q('#pv-model').value.trim();
              if (q('#pv-key').value) payload.api_key = q('#pv-key').value;
              if (q('#pv-auth').value) payload.auth_style = q('#pv-auth').value;
              const r = await api.post(`/api/ai/providers/${cur.id}/test`, payload);
              box.className = 'ai-test-line ' + (r.ok ? 'ok' : 'bad');
              box.textContent = `${r.ok ? '连通正常' : '连接失败'}：${r.message}（${r.latency_ms} ms）`;
            } catch (e) {
              box.className = 'ai-test-line bad';
              box.textContent = e.message || '测试失败';
            } finally {
              btn.disabled = false;
            }
          };

          q('#pv-del').onclick = async () => {
            const yes = await U.confirm(
              `删除配置「${U.esc(cur.name)}」？<br><br>删除后依赖它的场景会立即失效。`,
              { title: '删除模型配置', okText: '删除', danger: true }
            );
            if (!yes) return;
            try {
              await api.del(`/api/ai/providers/${cur.id}`);
              U.toast('已删除', 'success');
              apiMod.close();
              WB.rerender();
            } catch (e) {}
          };
        }
      },
    });
    return m;
  }

  /* ------------------------------------------------------------ 页面 */
  WB.views.ai_settings = async function (root) {
    root.innerHTML = '<div class="card"><div class="empty"><span class="spin"></span>加载中…</div></div>';

    const [status, rows, jevCfg] = await Promise.all([
      api.get('/api/ai/status'),
      WB.can('admin.ai') ? api.get('/api/ai/providers') : Promise.resolve([]),
      WB.can('admin.ai') ? api.get('/api/admin/jev-config') : Promise.resolve({items: [], available: false}),
    ]);
    if (!presets.length && WB.can('admin.ai')) {
      presets = await api.get('/api/ai/presets');
    }

    const ocr = status.ocr || {};
    const be = ocr.backends || {};

    root.innerHTML = `
      <div class="ai-hero">
        <span class="ai-avatar" style="width:38px;height:38px;flex:0 0 38px;border-radius:10px;background:linear-gradient(135deg,#1677ff,#4c9bff);color:#fff;display:flex;align-items:center;justify-content:center">AI</span>
        <div class="ai-hero-body">
          <b>${status.configured ? '已接入：' + U.esc(status.provider || '') : '尚未接入大模型'}</b>
          <span>
            ${status.configured
              ? `文本模型 <code class="mono">${U.esc(status.model || '-')}</code>${status.vision ? ' · 已启用视觉模型（可读发票图片）' : ' · 未配置视觉模型，拍照件走本地 OCR'}`
              : '接上模型后，收件箱识别兜底、报销单分析、审批建议与右下角助手才会生效。'}
          </span>
        </div>
        <div class="ai-prov-caps">
          ${cap(status.configured ? '模型已就绪' : '模型未配置', status.configured)}
          ${cap(ocr.ocr_ready ? '本地 OCR ' + U.esc(ocr.engine) : '本地 OCR 未启用', ocr.ocr_ready)}
          ${cap(ocr.pdf_text_layer ? 'PDF 文字层直读' : 'PDF 文字层不可用', ocr.pdf_text_layer)}
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h3>模型配置</h3>
          <span class="hint">可以配多个，按场景分别启用。API Key 加密存储，不回显明文。</span>
          <div class="right">
            <button class="btn btn-sm btn-primary" id="pv-add">接入大模型</button>
          </div>
        </div>
        <div class="card-body" id="pv-list"></div>
      </div>

      <div class="card">
        <div class="card-head">
          <h3>本地识别引擎</h3>
          <span class="hint">不联网、不花 token，负责把照片和扫描件变成文字</span>
        </div>
        <div class="card-body">
          <div class="ai-prov-caps" style="margin-top:0">
            ${cap('RapidOCR（PP-OCR 中文模型）', be.rapidocr)}
            ${cap('Tesseract', be.tesseract)}
            ${cap('PDFium（文字层 + 栅格化）', be.pdfium)}
            ${cap('Pillow', !!ocr.pillow)}
            ${cap('NumPy', !!ocr.numpy)}
          </div>
          <div class="recog-note" style="margin:14px 0 0">${U.esc(ocr.note || '')}</div>
          <div class="mail-hint">
            镜像里默认装了 RapidOCR 与 PDFium（约 300MB，纯 pip、无系统依赖）。
            需要瘦身时把 <code class="mono">INSTALL_OCR</code> 设为 0 重建即可，
            服务照常启动，只是图片类发票改为人工补录——识别链路会自动降级，不会报错。
          </div>
        </div>
      </div>

      <div class="card" id="jev-card">
        <div class="card-head">
          <h3>Jev 决策网关 <span class="tag" id="jev-status-tag">${jevCfg.available ? '已接入' : '未配置'}</span></h3>
          <span class="hint">Jev 做结构化决策（分类/异常/路由/重复），不写文本；未配置时 AI 工具自动降级回大模型/规则</span>
          <div class="right">
            <button class="btn" id="jev-test">连通性测试</button>
          </div>
        </div>
        <div class="card-body">
          <table class="kv">
            <thead><tr><th>配置项</th><th>当前值</th><th>说明</th><th></th></tr></thead>
            <tbody id="jev-rows">
              ${(jevCfg.items || []).map((it) => `
                <tr data-key="${U.esc(it.key)}">
                  <td data-label="配置项"><code class="mono">${U.esc(it.key)}</code></td>
                  <td data-label="当前值">
                    <input type="${it.key === 'jev_key' ? 'password' : 'text'}"
                      class="jev-inp" data-key="${U.esc(it.key)}"
                      placeholder="${it.is_set ? '已配置（留空保持原值）' : '未设置'}"
                      autocomplete="off" />
                    ${it.is_set ? `<span class="hint" style="margin-left:8px">掩码：<code class="mono">${U.esc(it.value)}</code></span>` : ''}
                  </td>
                  <td data-label="说明"><span class="hint">${U.esc(it.description || '')}</span></td>
                  <td data-label="操作">
                    <button class="btn jev-save">保存</button>
                    ${it.is_set ? '<button class="btn jev-del">清空</button>' : ''}
                  </td>
                </tr>`).join('')}
            </tbody>
          </table>
          <p class="hint" style="margin-top:12px">
            申请 key：Vercel 控制台 → AI Gateway → API Keys（形如 <code>vck_…</code>）。
            <b>注意：AI Gateway 要求账号绑定信用卡后才会放行</b>，否则调用会返回 403。
            改完保存即生效（5s 内），无需重启服务。env 变量 <code>JEV_API_KEY</code> 仍作为 fallback。
          </p>
          <div class="jev-test-out" id="jev-test-out" hidden></div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h3>调用用量</h3>
          <span class="hint">用于估算成本与排查失败</span>
          <div class="right">
            <div class="field"><label>统计范围</label>
              <select id="pv-days">
                <option value="7">近 7 天</option>
                <option value="30" selected>近 30 天</option>
                <option value="90">近 90 天</option>
              </select>
            </div>
          </div>
        </div>
        <div class="card-body" id="pv-usage"><div class="empty"><span class="spin"></span>加载中…</div></div>
      </div>`;

    /* -------- Jev 配置事件 -------- */
    if (WB.can('admin.ai')) {
      U.qsa('.jev-save', root).forEach((b) => b.onclick = async () => {
        const tr = b.closest('tr');
        const key = tr.dataset.key;
        const inp = U.qs('.jev-inp', tr);
        const val = inp.value.trim();
        if (!val) { U.toast('值不能为空（要清空请用「清空」按钮）', 'warn'); return; }
        try {
          await api.put('/api/admin/jev-config/' + encodeURIComponent(key), { value: val });
          U.toast('已保存', 'ok');
          WB.rerender();
        } catch (e) { U.toast('保存失败：' + (e.message || e), 'err'); }
      });
      U.qsa('.jev-del', root).forEach((b) => b.onclick = async () => {
        const tr = b.closest('tr');
        const key = tr.dataset.key;
        if (!confirm(`确认清空 ${key}？清空后降级回 env 或视为未配置`)) return;
        try {
          await api.del('/api/admin/jev-config/' + encodeURIComponent(key));
          U.toast('已清空', 'ok');
          WB.rerender();
        } catch (e) { U.toast('清空失败：' + (e.message || e), 'err'); }
      });
      const testBtn = U.qs('#jev-test', root);
      const testOut = U.qs('#jev-test-out', root);
      /* 连通性测试：后端不再抛笼统的 5xx，而是回一份结构化诊断，
         这里把它摊开显示——状态码、端点、耗时、服务端原文、以及可执行的建议。
         （这些字段是 2.9.1 补的：之前失败只回一句「调用失败」，查不出所以然。） */
      function showTestResult(r) {
        const lines = [];
        if (r.ok) {
          lines.push(`连通正常 · ${r.latency_ms}ms`);
          lines.push(`端点 ${r.url}`);
          if (r.answer !== null && r.answer !== undefined) {
            lines.push(`模型返回概率 ${r.answer}${r.confidence ? '（confidence ' + r.confidence + '）' : ''}`);
          }
        } else {
          lines.push(`连通失败：${r.error || '未知原因'}`);
          const bits = [];
          if (r.status) bits.push(`HTTP ${r.status}`);
          if (r.url) bits.push(r.url);
          if (r.latency_ms) bits.push(`${r.latency_ms}ms`);
          if (bits.length) lines.push(bits.join(' · '));
          if (r.hint) lines.push(`建议：${r.hint}`);
        }
        testOut.hidden = false;
        testOut.className = 'jev-test-out ' + (r.ok ? 'ok' : 'err');
        testOut.innerHTML = lines.map((t) => `<div>${U.esc(t)}</div>`).join('');
      }
      if (testBtn) testBtn.onclick = async () => {
        testBtn.disabled = true;
        const old = testBtn.textContent;
        testBtn.textContent = '测试中…';
        try {
          const r = await api.post('/api/admin/jev-config/test', {});
          showTestResult(r);
          U.toast(r.ok ? 'Jev 连通正常' : 'Jev 连通失败（原因见卡片下方）', r.ok ? 'ok' : 'err');
        } catch (e) {
          testOut.hidden = false;
          testOut.className = 'jev-test-out err';
          testOut.innerHTML = `<div>测试请求本身失败：${U.esc(e.message || String(e))}</div>`;
          U.toast('Jev 连通失败：' + (e.message || e), 'err');
        } finally {
          testBtn.disabled = false;
          testBtn.textContent = old;
        }
      };
    }

    /* -------- 配置列表 -------- */
    const listBox = U.qs('#pv-list', root);
    function renderList() {
      if (!WB.can('admin.ai')) {
        listBox.innerHTML = '<div class="empty">仅管理员可以查看与修改模型配置。</div>';
        return;
      }
      if (!rows.length) {
        listBox.innerHTML = `<div class="empty">还没有接入任何模型。<br>点右上角「接入大模型」，按厂商一键填充即可。</div>`;
        return;
      }
      listBox.innerHTML = `<div class="ai-prov-list">${rows
        .map((r) => {
          const test = r.last_test_at
            ? `<div class="ai-test-line ${r.last_test_ok ? 'ok' : 'bad'}">
                 ${r.last_test_ok ? '最近测试通过' : '最近测试失败'} · ${U.datetime(r.last_test_at)} ·
                 ${U.esc(r.last_test_detail || '')}</div>`
            : '';
          return `<div class="ai-prov ${r.is_default ? 'is-default' : ''}">
            <div class="ai-prov-head">
              <b>${U.esc(r.name)}</b>
              ${r.is_default ? '<span class="badge b-blue">默认</span>' : ''}
              ${r.enabled ? '' : '<span class="badge b-gray">已停用</span>'}
              <span class="right">
                <button class="btn btn-xs" data-edit="${r.id}">配置</button>
              </span>
            </div>
            <div class="ai-prov-meta">
              <code>${U.esc(r.model || '-')}</code>${r.vision_model ? ` · 视觉 <code>${U.esc(r.vision_model)}</code>` : ''}<br>
              ${U.esc(r.base_url || '')}<br>
              Key：${r.has_key ? U.esc(r.key_mask) : '<span style="color:var(--danger)">未配置</span>'}
              · ${U.esc((presetByKey(r.preset) || {}).label || r.preset || '自定义')}
            </div>
            <div class="ai-prov-caps">
              ${cap('助手', r.use_assistant)}
              ${cap('识别增强', r.use_recognize)}
              ${cap('分析/审批', r.use_analyze)}
              ${cap(r.vision_model ? '可读图' : '仅文本', !!r.vision_model)}
            </div>
            ${test}
          </div>`;
        })
        .join('')}</div>`;

      U.qsa('[data-edit]', listBox).forEach((b) => {
        b.onclick = () => {
          const row = rows.find((x) => String(x.id) === b.dataset.edit);
          if (row) openEditor(row);
        };
      });
    }
    renderList();

    U.qs('#pv-add', root).onclick = () => {
      if (!WB.can('admin.ai')) return U.toast('仅管理员可以修改模型配置', 'warn');
      openEditor(null);
    };

    /* -------- 用量 -------- */
    async function loadUsage(days) {
      const box = U.qs('#pv-usage', root);
      if (!WB.can('admin.ai')) {
        box.innerHTML = '<div class="empty">仅管理员可以查看调用用量。</div>';
        return;
      }
      box.innerHTML = '<div class="empty"><span class="spin"></span>加载中…</div>';
      try {
        const u = await api.get('/api/ai/usage', { days });
        if (!u.total_calls) {
          box.innerHTML = '<div class="empty">该时间范围内没有调用记录。</div>';
          return;
        }
        box.innerHTML = `
          <div class="ai-usage-grid">
            <div class="ai-usage-cell"><span>调用次数</span><b>${U.num(u.total_calls)}</b></div>
            <div class="ai-usage-cell"><span>失败次数</span><b style="color:${u.failed_calls ? 'var(--danger)' : 'inherit'}">${U.num(u.failed_calls)}</b></div>
            <div class="ai-usage-cell"><span>合计 tokens</span><b>${U.num(u.total_tokens)}</b></div>
            <div class="ai-usage-cell"><span>涉及模型</span><b>${U.num(u.by_model.length)}</b></div>
          </div>
          <div class="table-wrap" style="margin-top:16px">
            <table class="tbl">
              <thead><tr><th>场景</th><th class="num">次数</th><th class="num">失败</th><th class="num">tokens</th><th class="num">平均耗时</th></tr></thead>
              <tbody>
                ${u.by_kind
                  .map(
                    (k) => `<tr>
                    <td>${U.esc(KIND_LABEL[k.kind] || k.kind)}</td>
                    <td class="num">${U.num(k.calls)}</td>
                    <td class="num">${k.failed ? `<span style="color:var(--danger)">${U.num(k.failed)}</span>` : '0'}</td>
                    <td class="num">${U.num(k.tokens)}</td>
                    <td class="num">${U.num(k.avg_latency_ms)} ms</td>
                  </tr>`
                  )
                  .join('')}
              </tbody>
            </table>
          </div>
          <div class="table-wrap" style="margin-top:16px">
            <table class="tbl">
              <thead><tr><th style="width:150px">时间</th><th>场景</th><th>模型</th><th>调用人</th><th class="num">tokens</th><th>结果</th></tr></thead>
              <tbody>
                ${u.recent
                  .map(
                    (r) => `<tr>
                    <td class="sub-line nowrap">${U.datetime(r.created_at)}</td>
                    <td>${U.esc(KIND_LABEL[r.kind] || r.kind)}</td>
                    <td class="mono">${U.esc(r.model || '-')}</td>
                    <td>${U.esc(r.username || '-')}</td>
                    <td class="num">${U.num(r.tokens)}</td>
                    <td>${r.ok ? '<span class="badge b-green">成功</span>' : `<span class="badge b-red" title="${U.esc(r.error || '')}">失败</span>`}</td>
                  </tr>`
                  )
                  .join('')}
              </tbody>
            </table>
          </div>`;
      } catch (e) {
        box.innerHTML = `<div class="empty">用量加载失败：${U.esc(e.message || e)}</div>`;
      }
    }

    U.qs('#pv-days', root).onchange = (e) => loadUsage(e.target.value);
    await loadUsage(30);

    return {
      title: 'AI 设置',
      sub: '模型接入、识别引擎与调用用量',
      toolbar: `
        <span class="muted" style="font-size:12.5px">
          模型只做读取与分析；要落库的操作一律由用户在确认卡片上点「执行」，走本人权限。
        </span>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="pv-refresh">刷新</button>`,
      onToolbar(tb) {
        U.qs('#pv-refresh', tb).onclick = () => WB.rerender();
      },
    };
  };
})();
