/* 系统更新（v2.9.9）：从「系统参数」页内的卡片改为独立页面。
 *
 * 入口在右上角用户菜单的「系统更新」项（仅管理员），hash 路由 #/upgrade，
 * 不出现在左侧导航 / 手机标签栏（VIEWS 里 hidden: true）。
 *
 * 升级期间 app 容器会被替换，浏览器请求必然短暂失败——这是设计内的：
 * 轮询遇到网络错误不报错、也不停，只显示「服务正在重启」，恢复后继续显示结果。
 */
window.WB = window.WB || {};
WB.views = WB.views || {};

(function () {
  const U = WB.util;
  const api = WB.api;

  /* 升级源配置项的输入形态与说明 */
  const SPEC = {
    upgrade_repo: {
      label: '升级源仓库', placeholder: 'owner/repo',
      help: 'GitHub 仓库；Release 里需要放 manifest.json 与镜像包',
    },
    upgrade_token: {
      label: 'GitHub 令牌', secret: true, placeholder: '公开库可留空',
      help: '私有库必填；公开库填了也能把接口限额从 60 次/小时提到 5000',
    },
    upgrade_proxy: {
      label: '拉包代理', placeholder: '留空则跟随 AI_PROXY',
      help: '内网机器访问 GitHub 需要代理，例如 http://10.10.10.252:1086',
    },
    upgrade_auto_check: {
      label: '定时检查新版本', switch: true,
      help: '后台按间隔自动检查，发现新版就在本页提示（不会自动安装）',
    },
    upgrade_interval_hours: { label: '检查间隔（小时）', help: '默认 6 小时' },
    upgrade_allow_prerelease: {
      label: '包含预发布版', switch: true, help: 'beta / rc 版本也提示升级',
    },
    upgrade_require_manifest: {
      label: '必须校验清单', switch: true, defaultOn: true,
      help: 'Release 缺 manifest.json 时拒绝一键升级（推荐开启）',
    },
  };

  const PHASE_TEXT = {
    idle: '待命',
    downloading: '正在下载升级包',
    verifying: '正在校验安装包',
    extracting: '正在解包',
    switching: '正在切换容器',
    done: '升级已完成',
    failed: '升级失败',
  };
  const RUNNING = ['downloading', 'verifying', 'extracting', 'switching'];

  let timer = null;
  let lastPhase = '';
  let polls = 0;          // 连续失败次数（服务重启时用它设个上限，别无限轮询）

  function stopPoll() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  function isRunning(phase) {
    return RUNNING.indexOf(phase) >= 0;
  }

  /* ---------------- 状态徽章 ---------------- */

  /* ok=已是最新（绿） new=有新版（蓝） warn=未配置/异常（橙） */
  function stateChip(st) {
    const lt = st.latest || {};
    if (!st.configured) return '<span class="ug-chip warn">未配置升级源</span>';
    if (st.docker_ready === false) return '<span class="ug-chip warn">一键升级不可用</span>';
    if (lt.ok && lt.has_update) return `<span class="ug-chip new">可升级到 v${U.esc(lt.version)}</span>`;
    if (lt.ok) return '<span class="ug-chip ok">已是最新版本</span>';
    return '<span class="ug-chip">未检查</span>';
  }

  /* ---------------- 主体片段 ---------------- */

  function progressHtml(st) {
    const pr = st.progress || {};
    const pct = Math.round(Number(pr.progress || 0) * 100);
    const cls = pr.phase === 'failed' ? 'progress bad' : 'progress';
    const log = (pr.log || []).slice(-8).join('\n');
    return `
      <div class="ug-phases">
        <span class="ug-chip">当前 <b>v${U.esc(st.current_version || '—')}</b></span>
        <span class="ug-chip">目标 <b>v${U.esc(pr.target_version || '—')}</b></span>
        <span class="ug-chip new">${U.esc(PHASE_TEXT[pr.phase] || pr.phase || '')}</span>
      </div>
      <div class="${cls}" style="margin:14px 0 10px"><i style="width:${pct}%"></i></div>
      <div class="muted" style="font-size:12.5px">${U.esc(pr.message || '')}</div>
      ${log ? `<pre class="upg-log">${U.esc(log)}</pre>` : ''}`;
  }

  /* 有新版本时的版本对照 + 发布说明 */
  function latestHtml(lt) {
    if (!lt || !lt.ok || !lt.has_update) return '';
    return `
      <div class="ug-latest">
        <div class="ug-verline">
          <b>v${U.esc(lt.version)}</b>
          ${lt.prerelease ? '<span class="ug-tag">预发布</span>' : ''}
          ${lt.manifest_ok ? '<span class="ug-tag ok">含校验清单</span>' : '<span class="ug-tag warn">无校验清单</span>'}
          <span class="muted">发布于 ${U.datetime(lt.published_at)}</span>
        </div>
        ${lt.notes ? `<pre class="upg-notes">${U.esc(lt.notes)}</pre>` : ''}
      </div>`;
  }

  function alertsHtml(st) {
    const lt = st.latest || {};
    const pr = st.progress || {};
    const rows = [];
    if (!st.configured) {
      rows.push(`<div class="upg-note warn">还没配置升级源：请在下方「升级源配置」里填仓库（owner/repo），
        并确认该 Release 里有 manifest.json 与镜像包。</div>`);
    }
    if (st.docker_ready === false) {
      rows.push(`<div class="upg-note warn">容器拿不到宿主 Docker：${U.esc(st.docker_hint || '')}
        —— 一键升级不可用（其他功能不受影响）。安装时需要在 compose 里挂载 Docker socket。</div>`);
    }
    if (lt.error) {
      rows.push(`<div class="upg-note warn">上次检查未成功：${U.esc(lt.error)}</div>`);
    }
    if (pr.phase === 'failed') {
      rows.push(`<div class="upg-note warn">上次升级未完成：${U.esc(pr.detail || pr.message || '')}
        ${pr.finished_at ? '（' + U.esc(pr.finished_at) + '）' : ''}</div>`);
    }
    if (lt.ok && lt.has_update && !lt.manifest_ok) {
      rows.push(`<div class="upg-note warn">该 Release 没有 manifest.json：${U.esc(lt.manifest_error || '')}
        ${st.require_manifest ? '（当前设置为「必须校验清单」，一键升级会被拒绝）' : ''}</div>`);
    }
    return rows.join('');
  }

  function heroHtml(st) {
    const lt = st.latest || {};
    const checked = lt.checked_at ? `上次检查 ${U.datetime(lt.checked_at)}` : '还没检查过版本';
    const canApply = !!(lt.has_update && st.configured && st.docker_ready !== false);
    return `
      <div class="ug-hero">
        <div class="ug-hero-main">
          <div class="ug-label">当前版本</div>
          <div class="ug-cur">v${U.esc(st.current_version || '—')}</div>
          <div class="ug-state">${stateChip(st)}<span class="muted">${U.esc(checked)}</span></div>
        </div>
        <div class="ug-actions">
          <button class="btn" id="ug-check">检查更新</button>
          <button class="btn btn-primary" id="ug-apply" ${canApply ? '' : 'disabled'}>
            一键升级${lt.has_update && lt.version ? ' 到 v' + U.esc(lt.version) : ''}</button>
        </div>
      </div>
      <div id="ug-alerts">${alertsHtml(st)}</div>
      <div id="ug-latest">${latestHtml(lt)}</div>`;
  }

  /* 升级源配置：与系统参数页同一套「左信息右控件」行式布局 */
  function configHtml(st, cfg) {
    const items = (cfg && cfg.items) || [];
    const rows = items.map((it) => {
      const spec = SPEC[it.key] || {};
      const id = 'ug-cfg-' + it.key;
      let field;
      if (spec.switch) {
        /* 开关初值与后端口径一致：defaultOn 的项（如必须校验清单）
           在「从未设置」时后端按开启处理，UI 也要显示开启 */
        const on = spec.defaultOn ? String(it.value) !== '0' : String(it.value) === '1';
        field = `<label class="set-toggle">
            <input type="checkbox" id="${id}" data-key="${it.key}" ${on ? 'checked' : ''}>
            <span class="tk"></span>
            <span class="tk-text">${on ? '已开启' : '已关闭'}</span></label>`;
      } else {
        field = `<div class="input-unit">
            <input type="${spec.secret ? 'password' : 'text'}" id="${id}" data-key="${it.key}"
                   value="${U.esc(it.value || '')}" placeholder="${U.esc(spec.placeholder || '')}"
                   autocomplete="off" style="width:${it.key === 'upgrade_repo' ? '260px' : '200px'}">
          </div>`;
      }
      return `<div class="ug-row">
          <div class="ug-info">
            <div class="ug-name">${U.esc(spec.label || it.key)}<code class="set-key">${U.esc(it.key)}</code></div>
            <div class="ug-help">${U.esc(spec.help || it.description || '')}</div>
          </div>
          <div class="ug-ctrl">${field}</div>
        </div>`;
    }).join('');

    return `
      <div class="card ug-cfg-card">
        <div class="card-head">
          <h3>升级源配置</h3>
          <span class="hint">留空的项回落到安装时写入 .env 的 UPGRADE_* 变量</span>
        </div>
        <div class="ug-rows">${rows}</div>
        <div class="card-foot">
          <span class="muted">配置保存在系统参数表，变更记入操作审计</span>
          <div class="spacer"></div>
          <button class="btn" id="ug-cfg-save">保存配置</button>
        </div>
      </div>`;
  }

  /* ---------------- 挂载 ---------------- */

  WB.views.upgrade = async function (root) {
    stopPoll();
    root.innerHTML = `
      <div class="card ug-hero-card" id="ug-hero-card">
        <div class="empty" style="padding:40px"><span class="spin"></span>加载中…</div>
      </div>
      <div id="ug-config"></div>`;

    await render(root);

    return {
      title: '系统更新',
      sub: '在线检查新版本与一键升级',
      toolbar: '',
    };
  };

  /* ---------------- 主渲染（含升级中轮询） ---------------- */

  async function render(root) {
    if (!document.body.contains(root)) {
      stopPoll();
      return;
    }
    const card = U.qs('#ug-hero-card', root);
    if (!card) {
      stopPoll();
      return;
    }
    // 登出后不要再轮询：token 已清，继续请求只会刷出一堆 401
    if (WB.auth && !WB.auth.token) {
      stopPoll();
      return;
    }

    let st;
    try {
      st = await api.get('/api/admin/upgrade/status');
    } catch (e) {
      if (WB.auth && !WB.auth.token) {
        stopPoll();
        return;
      }
      card.innerHTML = `<div class="empty" style="padding:40px"><span class="spin"></span>
        服务正在重启，页面会自动恢复…</div>`;
      polls += 1;
      if (polls > 90) {          // 约 6 分钟还没恢复就不追了，避免无意义请求
        stopPoll();
        return;
      }
      if (!timer) timer = setInterval(() => render(root), 4000);
      return;
    }
    polls = 0;

    const pr = st.progress || {};
    const phase = pr.phase || 'idle';
    const wasRunning = isRunning(lastPhase);
    if (wasRunning && phase === 'done') {
      U.toast('升级完成，当前运行 v' + (pr.target_version || ''), 'success', 6000);
    }
    if (wasRunning && phase === 'failed') {
      U.toast('升级未完成：' + (pr.detail || pr.message || ''), 'error', 8000);
    }
    lastPhase = phase;

    /* 升级中：只渲染进度，起轮询 */
    if (isRunning(phase)) {
      card.innerHTML = `
        <div class="card-head"><h3>正在升级</h3></div>
        <div style="padding:var(--sp-5) var(--sp-6)">${progressHtml(st)}</div>
        <div class="card-foot">
          <span class="muted">升级期间页面中断属正常现象；请勿断电，完成后本页会自动恢复。</span>
        </div>`;
      U.qs('#ug-config', root).innerHTML = '';
      if (!timer) timer = setInterval(() => render(root), 3000);
      return;
    }
    stopPoll();

    let cfg = null;
    try {
      cfg = await api.get('/api/admin/upgrade/config');
    } catch (e) {
      cfg = null;
    }

    card.innerHTML = heroHtml(st);
    U.qs('#ug-config', root).innerHTML = configHtml(st, cfg);

    U.qs('#ug-check', card).onclick = () => doCheck(root);
    U.qs('#ug-apply', card).onclick = () => apply(root, st);

    /* 开关文字随勾选联动 */
    U.qsa('.ug-rows input[type=checkbox]', root).forEach((cb) => {
      cb.onchange = () => {
        const span = cb.parentElement.querySelector('.tk-text');
        if (span) span.textContent = cb.checked ? '已开启' : '已关闭';
      };
    });
    U.qs('#ug-cfg-save', root).onclick = () => saveConfig(root, cfg);
  }

  /* ---------------- 动作 ---------------- */

  async function saveConfig(root, cfg) {
    const items = (cfg && cfg.items) || [];
    const changed = [];
    for (const it of items) {
      const el = U.qs('#ug-cfg-' + it.key, root);
      if (!el) continue;
      const v = el.type === 'checkbox' ? (el.checked ? '1' : '0') : String(el.value).trim();
      if (v !== String(it.value || '')) changed.push([it.key, v]);
    }
    if (!changed.length) return U.toast('没有需要保存的修改', 'info');
    try {
      for (const [k, v] of changed) {
        await api.put('/api/admin/upgrade/config/' + encodeURIComponent(k), { value: v });
      }
      U.toast('已保存 ' + changed.length + ' 项配置', 'success');
    } catch (e) {
      /* request 层已提示 */
    }
  }

  async function doCheck(root) {
    const btn = U.qs('#ug-check', root);
    if (btn) {
      btn.disabled = true;
      btn.textContent = '检查中…';
    }
    try {
      const r = await api.post('/api/admin/upgrade/check', {});
      U.toast(r.has_update ? '发现新版本 v' + r.version : '已是最新版本', r.has_update ? 'success' : 'info');
    } catch (e) {
      /* request 层已提示 */
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = '检查更新';
      }
      render(root);
    }
  }

  async function apply(root, st) {
    const lt = st.latest || {};
    const cur = st.current_version || '';
    const target = lt.version || '';
    if (!target) return U.toast('先点「检查更新」确认目标版本', 'warn');
    const ok = await U.dangerConfirm(
      `即将把系统从 <b>v${U.esc(cur)}</b> 升级到 <b>v${U.esc(target)}</b>。<br><br>
       升级前会自动备份数据库；成功后镜像与 compose / install.sh 一起更新；
       页面会中断 1~2 分钟，若新版本未通过健康检查会自动回滚到 v${U.esc(cur)}。`,
      target,
      { title: '确认在线升级', okText: '开始升级' });
    if (!ok) return;
    try {
      await api.post('/api/admin/upgrade/apply', { version: target, confirm: true });
      U.toast('已开始下载并升级，请留在本页查看进度', 'info', 4000);
      lastPhase = 'downloading';
      render(root);
    } catch (e) {
      /* request 层已提示 */
    }
  }
})();
