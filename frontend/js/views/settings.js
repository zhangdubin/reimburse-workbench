/* 视图：系统参数（管理员可改，财务可看）
 *
 * 排版约定（v2.9.8 视觉重做）
 * ----------------
 * 结构分三层，解决「一列灰线到底、看不出主次」的问题：
 *   1. 顶部「当前生效」速览 —— 三张指标卡，沿用统计看板 .kpi 的视觉语言
 *      （左侧色条 + 大号等宽数值），先给结论再看明细。
 *   2. 每个分组一张面板卡（图标 + 标题 + 说明 + 组内设置行），
 *      取代原来「小蓝字标题 + 一条虚线到底」。
 *   3. 组内设置行：左信息右控件，行高统一，说明与「更新于」分主次。
 * 分组顺序由 GROUPS 决定，库里多出来的 key 兜底进「其他」组，不再裸奔。
 */
window.WB = window.WB || {};
WB.views = WB.views || {};

(function () {
  const U = WB.util;
  const api = WB.api;

  /* 24×24 线性图标，与侧栏导航同一视觉体系（stroke 1.7） */
  const ICON = {
    coins: '<circle cx="12" cy="12" r="8.4"/><path d="M12 7.4v9.2M9.7 9.6h4.6M9.7 13.2h4.6"/>',
    clock: '<circle cx="12" cy="12" r="8.4"/><path d="M12 7.6V12l3.1 1.9"/>',
    bell: '<path d="M18 8.6a6 6 0 1 0-12 0c0 5.5-2 6.8-2 6.8h16s-2-1.3-2-6.8z"/><path d="M10.3 19.4a2 2 0 0 0 3.4 0"/>',
    flow: '<rect x="3" y="3.6" width="7" height="5.2" rx="1.7"/><rect x="14" y="3.6" width="7" height="5.2" rx="1.7"/><path d="M6.5 8.8v3.4a2 2 0 0 0 2 2h7a2 2 0 0 0 2-2V8.8"/><path d="M12 14.2v6.2"/>',
    alert: '<path d="M12 3.6l8.4 14.8H3.6z"/><path d="M12 9.4v4.4M12 16.6h.01"/>',
    spark: '<path d="M11.5 3.2l1.9 5.1 5.1 1.9-5.1 1.9-1.9 5.1-1.9-5.1L4.5 10.2l5.1-1.9z"/><path d="M18.6 15.6l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
    info: '<circle cx="12" cy="12" r="8.4"/><path d="M12 11.2v5M12 8h.01"/>',
    shield: '<path d="M12 21.4s7.4-3.7 7.4-9.3V5.4L12 2.6 4.6 5.4v6.7c0 5.6 7.4 9.3 7.4 9.3z"/><path d="M9.3 12l1.9 1.9 3.5-3.5"/>',
  };

  function icon(name, cls) {
    return `<svg class="${cls || ''}" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name] || ''}</svg>`;
  }

  /* 每个参数的输入形态与说明，避免把「天数」当金额填 */
  const SPEC = {
    approval_level2_threshold: {
      label: '二级审批金额阈值', unit: '元', kind: 'money', group: 'flow',
      help: '单据金额超过该值时，需要二级审批。提交时会按当时的阈值重新计算。',
    },
    auto_approve_level1: {
      label: '一级审批自动通过', kind: 'switch', group: 'flow',
      help: '开启后提交即视为一级通过，直接进入二级审批；用于部门经理缺位时的临时放行。',
    },
    approval_overdue_days: {
      label: '待审批超期预警天数', unit: '天', kind: 'int', group: 'alert',
      help: '待审批超过该天数会进入「异常与预警」页的超期提醒。',
    },
    large_amount_threshold: {
      label: '大额单据预警阈值', unit: '元', kind: 'money', group: 'alert',
      help: '用于「异常与预警」中的大额单据统计，不影响审批级数。',
    },
    ai_recognize_on_ingest: {
      label: '收票后自动识别补全', kind: 'switch', group: 'ai',
      help: '邮箱/自动收票入库后再用大模型补全字段。每张票多一次模型调用，会增加耗时与费用；手工上传识别不受影响。',
    },
  };

  /* 顶部速览指标（来自后端 approval_summary，只读） */
  const METRICS = [
    {
      tone: 'blue', icon: 'coins', label: '二级审批门槛',
      foot: '超过该金额需二级审批', value: (ap) => U.money(ap.level2_threshold),
    },
    {
      tone: 'orange', icon: 'clock', label: '待审批超期预警',
      foot: '超过该天数进入预警', value: (ap) => `${ap.overdue_days} 天`,
    },
    {
      tone: 'purple', icon: 'bell', label: '大额单据预警线',
      foot: '异常统计口径，不影响审批', value: (ap) => U.money(ap.large_amount_threshold),
    },
  ];

  /* 分组展示顺序；库里多出的 key 兜底进 other */
  const GROUPS = [
    { id: 'flow', title: '审批流程', desc: '影响提交后的审批级数', icon: 'flow' },
    { id: 'alert', title: '异常与预警', desc: '统计口径，不影响审批', icon: 'alert' },
    { id: 'ai', title: '收票与识别', desc: '自动链路的模型开关', icon: 'spark' },
    { id: 'other', title: '其他', desc: '外部配置，仅供查看', icon: 'info' },
  ];

  function groupOf(key) {
    return (SPEC[key] || {}).group || 'other';
  }

  /* 开关：真 toggle 视觉，勾选状态同步到右侧状态胶囊 */
  function switchFor(item, editable) {
    const on = String(item.value) === '1';
    return `<label class="set-toggle">
      <input type="checkbox" id="set-${item.key}" ${on ? 'checked' : ''} ${editable ? '' : 'disabled'}>
      <span class="tk" aria-hidden="true"></span>
      <span class="tk-text">${on ? '已开启' : '已关闭'}</span>
    </label>`;
  }

  function controlFor(item, editable) {
    const spec = SPEC[item.key] || {};
    if (spec.kind === 'switch') return switchFor(item, editable);
    if (spec.kind === 'money' || spec.kind === 'int') {
      const step = spec.kind === 'money' ? '0.01' : '1';
      return `<div class="input-unit">
        <input type="number" step="${step}" min="0" id="set-${item.key}" value="${U.esc(item.value)}" ${editable ? '' : 'disabled'}>
        ${spec.unit ? `<em>${U.esc(spec.unit)}</em>` : ''}
      </div>`;
    }
    /* 兜底必须是文本框：number 输入框塞进 URL/密钥等字符串会触发浏览器
       "The specified value ... cannot be parsed" 且值被静默清空（v2.9.7）。 */
    return `<div class="input-unit">
      <input type="text" id="set-${item.key}" value="${U.esc(item.value)}" ${editable ? '' : 'disabled'}>
    </div>`;
  }

  function rowFor(item, editable) {
    const spec = SPEC[item.key] || {};
    const name = spec.label || item.remark || item.key;
    const help = spec.help || item.remark || '';
    return `<div class="set-row" data-key="${U.esc(item.key)}">
      <div class="set-info">
        <div class="set-name">${U.esc(name)}<code class="set-key">${U.esc(item.key)}</code><span class="set-meta">更新于 ${U.datetime(item.updated_at)}</span></div>
        ${help ? `<div class="set-help">${U.esc(help)}</div>` : ''}
      </div>
      <div class="set-ctrl">
        ${controlFor(item, editable)}
      </div>
    </div>`;
  }

  WB.views.settings = async function (root) {
    const editable = WB.can('admin.settings.write');

    root.innerHTML = `
      <div class="card set-card">
        <div class="card-head">
          <h3>审批与预警参数</h3>
          <span class="hint">${editable ? '修改后立即生效，并记入操作审计' : '当前角色只读，修改请联系管理员'}</span>
          <div class="right">${editable ? '' : `<span class="set-ro">${icon('shield')}只读</span>`}</div>
        </div>
        <div class="set-body" id="set-body"><div class="empty"><span class="spin"></span>加载中…</div></div>
        <div class="card-foot" id="set-foot"></div>
      </div>`;

    const body = U.qs('#set-body', root);
    const foot = U.qs('#set-foot', root);

    async function load() {
      const data = await api.settings();
      const ap = data.approval || {};

      /* 按分组归位；组内保持 GROUPS 声明的 key 顺序 */
      const byGroup = {};
      for (const g of GROUPS) byGroup[g.id] = [];
      for (const it of data.items) {
        (byGroup[groupOf(it.key)] || byGroup.other).push(it);
      }

      const metricsHtml = `
        <div class="set-metrics">
          ${METRICS.map((mt) => `
            <div class="set-metric m-${mt.tone}">
              <span class="sm-ico">${icon(mt.icon)}</span>
              <div class="sm-body">
                <div class="sm-label">${U.esc(mt.label)}</div>
                <div class="sm-value">${mt.value(ap)}</div>
                <div class="sm-foot">${U.esc(mt.foot)}</div>
              </div>
            </div>`).join('')}
        </div>`;

      const panelsHtml = GROUPS
        .filter((g) => byGroup[g.id].length)
        .map((g) => `
          <section class="set-panel">
            <div class="set-panel-head">
              <span class="sp-ico">${icon(g.icon)}</span>
              <span class="sp-title">${U.esc(g.title)}</span>
              ${g.desc ? `<span class="sp-desc">${U.esc(g.desc)}</span>` : ''}
            </div>
            <div class="set-list">
              ${byGroup[g.id].map((it) => rowFor(it, editable)).join('')}
            </div>
          </section>`)
        .join('');

      body.innerHTML = metricsHtml + panelsHtml;

      // toggle 的状态文字跟随勾选
      U.qsa('.set-toggle input[type=checkbox]', body).forEach((cb) => {
        cb.onchange = () => {
          const txt = cb.parentElement.querySelector('.tk-text');
          if (txt) {
            txt.textContent = cb.checked ? '已开启' : '已关闭';
            txt.classList.toggle('on', cb.checked);
          }
        };
      });

      if (!editable) {
        foot.innerHTML = '';
        return;
      }
      foot.innerHTML = `
        <span class="cf-hint">${icon('info')}仅提交发生变化的参数，未改动的不写入审计。</span>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="set-reload">还原</button>
        <button class="btn btn-sm btn-primary" id="set-save">保存修改</button>`;

      U.qs('#set-reload', foot).onclick = () => WB.rerender();
      U.qs('#set-save', foot).onclick = async () => {
        const changed = [];
        for (const it of data.items) {
          if (!SPEC[it.key]) continue; /* 未登记 key 只展示不提交，PUT 白名单会 400 */
          const el = U.qs(`#set-${it.key}`, body);
          if (!el) continue;
          let v;
          if (el.type === 'checkbox') v = el.checked ? '1' : '0';
          else v = String(el.value).trim();
          if (v === '') {
            return U.toast(`「${(SPEC[it.key] || {}).label || it.key}」不能为空`, 'warn');
          }
          if (v !== String(it.value)) changed.push([it.key, v]);
        }
        if (!changed.length) return U.toast('没有需要保存的修改', 'info');
        try {
          for (const [k, v] of changed) {
            await api.saveSetting(k, v);
          }
          U.toast(`已保存 ${changed.length} 项参数`, 'success');
          WB.rerender();
        } catch (e) {}
      };
    }

    await load();

    return {
      title: '系统参数',
      sub: '审批分级阈值与预警口径',
      toolbar: `<button class="btn btn-sm" id="btn-refresh">刷新</button>`,
      onToolbar(tb) {
        U.qs('#btn-refresh', tb).onclick = () => WB.rerender();
      },
    };
  };
})();
