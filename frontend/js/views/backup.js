/* 数据备份（v2.9.28 重做）
 *
 * 上一版的问题，也是本次重做的三条主线：
 * 1) 找不到文件：界面只给容器路径 /app/data/backups，管理员在服务器上根本对不上号。
 *    现在每张存储卡都给出「容器内路径」与「宿主路径」（后端反查自身容器挂载表），
 *    旁边带复制按钮；查不到就明确说「未挂载到宿主」并给出 docker cp 命令。
 * 2) 下载报 HTTP 错误：改走一次性票据 + 浏览器原生下载，不再把几个 GB 的包
 *    fetch 成 Blob（那是标签页内存爆炸和反代超时的主因）；失败时把后端 detail 说出来。
 * 3) 排版丑、配置难用：周期改成一键分段按钮，保留份数改成步进器，目录改动有应用按钮
 *    并即时回报保存状态；列表表格化，宿主路径可复制。
 */
window.WB = window.WB || {};
WB.views = WB.views || {};

(function () {
  const U = WB.util;
  const api = WB.api;

  let cachedStatus = null;
  let cachedList = [];
  const ui = { filter: '', showAll: false, running: false };

  /* ---------------- 格式化 ---------------- */
  function fmtBytes(n) {
    if (!n && n !== 0) return '-';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1024 * 1024 * 1024) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
  }

  function fmtTime(iso) {
    if (!iso) return '-';
    try { return String(iso).replace('T', ' ').slice(0, 19); } catch (_) { return String(iso); }
  }

  function fmtAgo(iso) {
    if (!iso) return '-';
    const t = new Date(String(iso).replace(' ', 'T')).getTime();
    if (Number.isNaN(t)) return '-';
    const dt = (Date.now() - t) / 1000;
    if (dt < 60) return '刚刚';
    if (dt < 3600) return Math.floor(dt / 60) + ' 分钟前';
    if (dt < 86400) return Math.floor(dt / 3600) + ' 小时前';
    if (dt < 86400 * 30) return Math.floor(dt / 86400) + ' 天前';
    return fmtTime(iso).slice(0, 10);
  }

  /* ---------------- 数据 ---------------- */
  async function loadStatus(refresh) {
    try {
      cachedStatus = await api.backupStatus(refresh);
    } catch (e) { /* api 层已 toast */ }
    return cachedStatus || {};
  }

  async function loadList() {
    try {
      const r = await api.backupList();
      cachedList = (r && r.items) || [];
    } catch (e) { cachedList = []; }
    return cachedList;
  }

  async function reload(refresh) {
    await Promise.all([loadStatus(refresh), loadList()]);
    WB.rerender();
  }

  /* ---------------- 概览 ---------------- */
  function renderHero(s) {
    const h = s.health || {};
    const cfg = s.config || {};
    const last = s.last_result || {};
    const ok = last && last.ok !== false && !last.error;
    const level = h.level || (ok ? 'ok' : 'bad');
    const used = ((s.storage && s.storage.local && s.storage.local.bytes) || 0);
    const pills = [
      { l: '最近备份', v: ok && last.finished_at ? fmtAgo(last.finished_at) : '—' },
      { l: '自动周期', v: Number(cfg.backup_interval_hours) > 0 ? `每 ${Number(cfg.backup_interval_hours)} 小时` : '已关闭' },
      { l: '下次自动', v: s.next_run_at ? fmtTime(s.next_run_at).slice(5, 16) : '—' },
      { l: '备份包', v: `${(s.counts && s.counts.local) || 0} 份 · ${fmtBytes(used)}` },
    ].map((p) => `<div class="bk-hero-stat"><span>${U.esc(p.l)}</span><b>${U.esc(p.v)}</b></div>`).join('');

    return `
      <div class="card bk-hero ${level}">
        <div class="bk-hero-main">
          <div class="bk-hero-txt">
            <div class="bk-hero-title">数据备份与还原</div>
            <div class="bk-hero-sub">${U.esc(h.text || '周期备份 · 手动备份 · 跨机还原')}</div>
          </div>
          <div class="bk-hero-acts">
            <button class="btn btn-primary" id="bk-run" ${ui.running ? 'disabled' : ''}>
              ${ui.running ? '<span class="spin"></span> 备份中…' : '立即备份'}
            </button>
            <button class="btn" id="bk-refresh">刷新</button>
          </div>
        </div>
        <div class="bk-hero-stats">${pills}</div>
      </div>`;
  }

  /* ---------------- 存储位置 ---------------- */
  function storageCard(title, tag, d, cmd) {
    if (!d) return '';
    if (!d.configured) {
      return `
        <div class="bk-store">
          <div class="bk-store-h"><span class="bk-store-t">${U.esc(title)}</span><span class="bk-tag">${U.esc(tag)}</span></div>
          <div class="bk-store-off">未启用 —— 在下方「备份策略 → 高级」里填外挂目录即可开启</div>
        </div>`;
    }
    const mounted = !!d.host_path;
    const reason = d.reason === 'docker_unavailable'
      ? '容器内看不到宿主挂载信息（未挂载 docker.sock 或非容器运行），下面给出取包命令'
      : (d.reason === 'not_mounted' ? '该目录没有挂到宿主，备份只存在于容器内，重建容器会丢' : '');
    const writable = d.exists ? (d.writable ? '可写' : '只读') : '目录不存在';
    const wcls = d.exists ? (d.writable ? 'ok' : 'bad') : 'bad';
    const usedPct = d.total ? Math.min(100, Math.round(((d.total - (d.free || 0)) / d.total) * 100)) : 0;
    const mountName = d.mount_type === 'bind' ? '宿主目录挂载' : (d.mount_type === 'volume' ? `Docker 卷 ${d.volume || ''}` : '');

    return `
      <div class="bk-store">
        <div class="bk-store-h">
          <span class="bk-store-t">${U.esc(title)}</span>
          <span class="bk-tag ${wcls}">${U.esc(writable)}</span>
          ${mountName ? `<span class="bk-tag">${U.esc(mountName)}</span>` : ''}
        </div>
        <div class="bk-path">
          <span class="bk-path-l">容器内</span>
          <code class="bk-path-v">${U.esc(d.path || '-')}</code>
          <button class="bk-copy" data-copy="${U.esc(d.path || '')}" title="复制容器内路径">复制</button>
        </div>
        <div class="bk-path ${mounted ? 'hl' : ''}">
          <span class="bk-path-l">宿主机</span>
          ${mounted
            ? `<code class="bk-path-v">${U.esc(d.host_path)}</code>
               <button class="bk-copy" data-copy="${U.esc(d.host_path)}" title="复制宿主路径">复制</button>`
            : `<span class="bk-path-none">宿主上找不到这个目录</span>`}
        </div>
        ${reason ? `<div class="bk-store-warn">${U.esc(reason)}</div>` : ''}
        ${!mounted && cmd ? `
          <div class="bk-path cmd">
            <span class="bk-path-l">取包命令</span>
            <code class="bk-path-v">${U.esc(cmd)}</code>
            <button class="bk-copy" data-copy="${U.esc(cmd)}" title="复制 docker cp 命令">复制</button>
          </div>` : ''}
        <div class="bk-space">
          <div class="bk-space-bar"><i style="width:${usedPct}%"></i></div>
          <div class="bk-space-txt">
            <span>备份占用 ${fmtBytes(d.bytes)}（${d.files} 份）</span>
            <span>可用 ${fmtBytes(d.free)} / 共 ${fmtBytes(d.total)}</span>
          </div>
        </div>
      </div>`;
  }

  function renderStorage(s) {
    const st = s.storage || {};
    return `
      <div class="card bk-card">
        <div class="bk-card-h">
          <span>备份文件存在哪</span>
          <button class="btn btn-xs" id="bk-remount" title="重新读取容器挂载信息">重新检测挂载</button>
        </div>
        <div class="bk-store-grid">
          ${storageCard('本地备份目录', '主', st.local, s.copy_command)}
          ${storageCard('外挂备份目录', '副本', st.remote, s.copy_command_remote)}
        </div>
        ${s.docker && !s.docker.available ? `<div class="bk-store-warn">读不到宿主挂载信息（${U.esc(s.docker.message || 'Docker socket 不可用')}），只能给出容器路径与 docker cp 命令。</div>` : ''}
      </div>`;
  }

  /* ---------------- 备份策略 ---------------- */
  const INTERVALS = [
    { v: '0', t: '关闭' },
    { v: '6', t: '每 6 小时' },
    { v: '12', t: '每 12 小时' },
    { v: '24', t: '每天' },
    { v: '168', t: '每周' },
  ];

  function stepper(key, label, val, hint) {
    return `
      <div class="bk-field">
        <label class="bk-f-l">${U.esc(label)}</label>
        <div class="bk-step">
          <button class="bk-step-b" data-step="${key}" data-delta="-1">−</button>
          <input class="bk-step-v" type="number" min="0" step="1" value="${U.esc(val == null ? '' : val)}" data-cfg-num="${key}" />
          <button class="bk-step-b" data-step="${key}" data-delta="1">＋</button>
        </div>
        <div class="bk-f-h">${U.esc(hint)}</div>
      </div>`;
  }

  function renderPolicy(s) {
    const cfg = s.config || {};
    const iv = String(cfg.backup_interval_hours == null ? '' : cfg.backup_interval_hours);
    const known = INTERVALS.some((x) => x.v === iv);
    const segs = INTERVALS.map(
      (x) => `<button class="bk-seg ${x.v === iv ? 'on' : ''}" data-iv="${x.v}">${U.esc(x.t)}</button>`
    ).join('');

    return `
      <div class="card bk-card">
        <div class="bk-card-h"><span>备份策略</span><span class="bk-saved" id="bk-saved"></span></div>
        <div class="bk-policy">
          <div class="bk-field">
            <label class="bk-f-l">自动备份周期</label>
            <div class="bk-segs">${segs}</div>
            <div class="bk-f-h">${known ? '按上面的节奏自动跑；服务重启后周期重新计时' : `当前：每 ${U.esc(iv)} 小时`}。选「关闭」则只在手工点击时备份</div>
          </div>

          <div class="bk-field">
            <label class="bk-f-l">自定义周期（小时）</label>
            <input class="bk-input" type="number" min="0" step="0.5" value="${U.esc(iv)}" data-cfg-num="backup_interval_hours" />
            <div class="bk-f-h">支持小数，例如 0.5 = 每 30 分钟；最短 0.25 小时</div>
          </div>

          <div class="bk-row2">
            ${stepper('backup_keep_local', '本地保留份数', cfg.backup_keep_local, '超出份数自动删除最旧的（0 = 永不自动删）')}
            ${stepper('backup_keep_remote', '外挂保留份数', cfg.backup_keep_remote, '外挂目录未启用时不生效')}
          </div>

          <div class="bk-field">
            <label class="bk-f-l">容器启动时立即备份一次</label>
            <label class="switch"><input type="checkbox" id="cfg-boot" ${cfg.backup_at_boot ? 'checked' : ''} /><span class="slider"></span></label>
            <div class="bk-f-h">适合每天关机、第二天才开机的办公室环境</div>
          </div>

          <details class="bk-adv">
            <summary>高级：目录设置（容器内路径）</summary>
            <div class="bk-field">
              <label class="bk-f-l">本地备份目录</label>
              <input class="bk-input" data-cfg-path="backup_dir" value="${U.esc(cfg.backup_dir || '')}" />
              <div class="bk-f-h">容器内的绝对路径。宿主那一侧由 docker-compose 的挂载决定，改完需要重建容器</div>
            </div>
            <div class="bk-field">
              <label class="bk-f-l">外挂备份目录</label>
              <input class="bk-input" data-cfg-path="backup_remote_dir" value="${U.esc(cfg.backup_remote_dir || '')}" />
              <div class="bk-f-h">填宿主/NFS/SMB 挂进容器后的路径；留空 = 不存副本</div>
            </div>
            <div class="bk-adv-act">
              <button class="btn btn-sm btn-primary" id="bk-apply-path">应用目录设置</button>
              <button class="btn btn-sm" id="bk-reset-cfg">恢复默认</button>
            </div>
          </details>
        </div>
      </div>`;
  }

  /* ---------------- 最近一次 ---------------- */
  function renderLast(s) {
    const r = (s || {}).last_result || {};
    if (!r || (!r.ok && !r.error && !r.name)) {
      return '<div class="card bk-card bk-empty">还没有备份记录 —— 点右上角「立即备份」生成第一份。</div>';
    }
    if (r.ok === false || r.error) {
      return `<div class="card bk-card bk-last err">
        <div class="bk-card-h"><span>最近一次备份失败</span><span class="bk-tag bad">失败</span></div>
        <div class="bk-last-msg">${U.esc(r.error || '未知错误')}</div>
        <div class="bk-last-meta">${U.esc(fmtTime(r.finished_at || ''))}</div>
      </div>`;
    }
    const cells = [
      ['大小', fmtBytes(r.size)],
      ['耗时', (r.elapsed_seconds ?? '-') + ' s'],
      ['表行数', r.table_rows ?? '-'],
      ['影像文件', r.uploaded_files ?? '-'],
      ['完成时间', fmtTime(r.finished_at || '')],
      ['sha256', (r.sha256 || '').slice(0, 16) + '…'],
    ].map((c) => `<div class="bk-cell"><b>${U.esc(c[0])}</b><span>${U.esc(String(c[1]))}</span></div>`).join('');

    return `
      <div class="card bk-card bk-last">
        <div class="bk-card-h"><span>最近一次备份</span><span class="bk-tag ok">成功</span></div>
        <div class="bk-last-name">${U.esc(r.name || '')}</div>
        <div class="bk-cells">${cells}</div>
        ${r.host_path ? `<div class="bk-path hl">
            <span class="bk-path-l">宿主机</span>
            <code class="bk-path-v">${U.esc(r.host_path)}</code>
            <button class="bk-copy" data-copy="${U.esc(r.host_path)}">复制</button>
          </div>` : (r.copy_command ? `<div class="bk-path cmd">
            <span class="bk-path-l">取包命令</span>
            <code class="bk-path-v">${U.esc(r.copy_command)}</code>
            <button class="bk-copy" data-copy="${U.esc(r.copy_command)}">复制</button>
          </div>` : '')}
        ${r.remote_error ? `<div class="bk-store-warn">外挂副本失败：${U.esc(r.remote_error)}</div>` : ''}
      </div>`;
  }

  /* ---------------- 备份包列表 ---------------- */
  function renderList(s) {
    const all = cachedList || [];
    const kw = (ui.filter || '').trim().toLowerCase();
    const list = kw ? all.filter((x) => String(x.name).toLowerCase().indexOf(kw) >= 0) : all;
    const shown = ui.showAll ? list : list.slice(0, 20);

    const head = `
      <div class="bk-card-h">
        <span>备份包 <em class="bk-count">${list.length} 份</em></span>
        <div class="bk-list-tools">
          <input class="bk-input bk-search" placeholder="按文件名筛选…" value="${U.esc(ui.filter)}" id="bk-kw" />
          <button class="btn btn-xs" id="bk-prune">裁剪到保留份数</button>
        </div>
      </div>`;

    if (!list.length) {
      return `<div class="card bk-card">${head}<div class="bk-empty">没有匹配的备份包。</div></div>`;
    }

    const rows = shown.map((it) => {
      const whereTxt = it.where === 'remote' ? '外挂' : '本地';
      return `
        <tr>
          <td class="bk-td-name">
            <div class="bk-nm" title="${U.esc(it.name)}">${U.esc(it.name)}</div>
            ${it.host_path
              ? `<div class="bk-hp"><code title="${U.esc(it.host_path)}">${U.esc(it.host_path)}</code><button class="bk-copy xs" data-copy="${U.esc(it.host_path)}">复制</button></div>`
              : `<div class="bk-hp muted"><code title="${U.esc(it.path)}">${U.esc(it.path)}</code>（容器内）</div>`}
          </td>
          <td><span class="bk-tag ${it.where === 'remote' ? '' : 'ok'}">${whereTxt}</span></td>
          <td class="num">${fmtBytes(it.size)}</td>
          <td><div>${U.esc(fmtTime(it.mtime_iso).slice(5, 16))}</div><div class="muted xs">${U.esc(fmtAgo(it.mtime_iso))}</div></td>
          <td class="bk-td-act">
            <button class="btn btn-xs btn-primary" data-dl="${U.esc(it.name)}">下载</button>
            <button class="btn btn-xs btn-danger" data-restore="${U.esc(it.name)}">还原</button>
          </td>
        </tr>`;
    }).join('');

    const more = (!ui.showAll && list.length > 20)
      ? `<div class="bk-more"><button class="btn btn-xs" id="bk-more">显示全部 ${list.length} 份</button></div>`
      : '';

    return `
      <div class="card bk-card">
        ${head}
        <div class="table-wrap">
          <table class="table bk-table">
            <thead><tr>
              <th>备份包 / 宿主路径</th><th>位置</th><th class="num">大小</th><th>时间</th><th class="bk-th-act">操作</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
        ${more}
      </div>`;
  }

  function renderDanger() {
    return `
      <div class="card bk-card bk-danger">
        <div class="bk-card-h"><span>危险操作</span></div>
        <div class="bk-danger-row">
          <div class="bk-danger-txt">
            <b>上传备份包并还原</b>
            <span>从别的机器打包带来的 .tar.gz，会<b>覆盖</b>当前数据库与全部影像</span>
          </div>
          <button class="btn btn-danger btn-sm" id="bk-upload">选择备份包…</button>
          <input type="file" id="bk-file" accept=".gz,.tar.gz" style="display:none" />
        </div>
      </div>`;
  }

  /* ---------------- 交互 ---------------- */
  function flashSaved(txt) {
    const el = document.getElementById('bk-saved');
    if (!el) return;
    el.textContent = txt || '已保存';
    el.classList.add('on');
    clearTimeout(flashSaved._t);
    flashSaved._t = setTimeout(() => {
      el.classList.remove('on');
      setTimeout(() => { el.textContent = ''; }, 300);
    }, 1800);
  }

  async function saveCfg(values, quiet) {
    try {
      await api.backupSetConfigBatch(values);
      if (!quiet) flashSaved('已保存');
      await loadStatus();
      return true;
    } catch (e) {
      flashSaved('保存失败');
      return false;
    }
  }

  async function downloadBackup(name) {
    // 首选：一次性票据 + 浏览器原生下载（大包不进内存，也支持续传）
    try {
      const t = await api.backupTicket(name);
      U.download(t.url);
      U.toast('已开始下载：' + name);
      return;
    } catch (e) { /* 落到下面的 Bearer 方案 */ }

    const token = WB.auth && WB.auth.token;
    try {
      const res = await fetch(api.backupDownloadUrl(name), {
        headers: token ? { Authorization: 'Bearer ' + token } : {},
      });
      if (!res.ok) {
        let detail = '';
        try {
          const j = await res.json();
          detail = j && j.detail ? '：' + j.detail : '';
        } catch (_) { /* 非 JSON 响应 */ }
        throw new Error('下载失败 HTTP ' + res.status + detail);
      }
      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
      U.toast('已开始下载：' + name);
    } catch (err) {
      U.toast(err.message || '下载失败', 'error', 4200);
    }
  }

  async function restoreFlow(name) {
    const ok1 = await U.confirm(
      `将从「${name}」还原：当前数据库与全部影像会被覆盖，且不可撤销。\n建议先「立即备份」一次再操作。`,
      { title: '还原确认', okText: '我了解风险', danger: true },
    );
    if (!ok1) return;
    const word = await U.prompt('请输入 RESTORE 四个字母以确认', '', { placeholder: 'RESTORE', confirmText: '确认还原', danger: true });
    if (!word || String(word).trim().toUpperCase() !== 'RESTORE') {
      U.toast('确认词不对，已取消', 'warn');
      return;
    }
    try {
      const r = await api.backupRestore(name);
      U.toast(r.message || '还原完成', 'success', 4000);
      await reload(true);
    } catch (e) { /* 已 toast */ }
  }

  function bind(root) {
    /* 复制 */
    root.addEventListener('click', (e) => {
      const b = e.target.closest('[data-copy]');
      if (b && root.contains(b)) U.copyText(b.dataset.copy);
    });

    /* 立即备份 */
    const runBtn = root.querySelector('#bk-run');
    if (runBtn) {
      runBtn.onclick = async () => {
        ui.running = true;
        runBtn.disabled = true;
        runBtn.innerHTML = '<span class="spin"></span> 备份中…';
        try {
          const r = await api.backupRun();
          const res = (r && r.result) || {};
          U.toast(`备份完成：${res.name}（${fmtBytes(res.size)}）`, 'success', 3600);
          await reload(false);
        } catch (e) { /* 已 toast */ }
        ui.running = false;
      };
    }

    const rf = root.querySelector('#bk-refresh');
    if (rf) rf.onclick = () => reload(true);

    const rm = root.querySelector('#bk-remount');
    if (rm) rm.onclick = async () => {
      U.toast('正在重新读取容器挂载信息…');
      await reload(true);
    };

    /* 周期分段按钮 */
    root.querySelectorAll('[data-iv]').forEach((b) => {
      b.onclick = async () => {
        const v = b.dataset.iv;
        root.querySelectorAll('[data-iv]').forEach((x) => x.classList.toggle('on', x === b));
        const num = root.querySelector('[data-cfg-num="backup_interval_hours"]');
        if (num) num.value = v;
        await saveCfg({ backup_interval_hours: v });
      };
    });

    /* 数字项：改动即保存（防抖，避免边输边请求） */
    const saveNum = U.debounce(async (key, val) => {
      const ok = await saveCfg({ [key]: String(val) });
      flashSaved(ok ? '已保存' : '保存失败');
    }, 500);
    root.querySelectorAll('[data-cfg-num]').forEach((inp) => {
      inp.oninput = () => saveNum(inp.dataset.cfgNum, inp.value);
    });

    /* 步进器 */
    root.querySelectorAll('[data-step]').forEach((b) => {
      b.onclick = () => {
        const key = b.dataset.step;
        const inp = root.querySelector(`[data-cfg-num="${key}"]`);
        if (!inp) return;
        const cur = parseInt(inp.value, 10);
        const base = Number.isNaN(cur) ? 0 : cur;
        inp.value = Math.max(0, base + Number(b.dataset.delta));
        saveNum(key, inp.value);
      };
    });

    /* 启动即备份 */
    const boot = root.querySelector('#cfg-boot');
    if (boot) {
      boot.onchange = async () => {
        await saveCfg({ backup_at_boot: boot.checked ? '1' : '0' });
      };
    }

    /* 目录设置 */
    const apply = root.querySelector('#bk-apply-path');
    if (apply) {
      apply.onclick = async () => {
        const vals = {};
        root.querySelectorAll('[data-cfg-path]').forEach((inp) => { vals[inp.dataset.cfgPath] = inp.value.trim(); });
        const ok = await saveCfg(vals, true);
        if (!ok) { U.toast('目录设置未生效，请检查格式（必须是以 / 开头的绝对路径）', 'error', 4000); return; }
        U.toast('目录已更新，正在重新检测挂载…');
        await reload(true);
      };
    }
    const reset = root.querySelector('#bk-reset-cfg');
    if (reset) {
      reset.onclick = async () => {
        const okc = await U.confirm('恢复为系统默认配置（周期 24 小时、保留 30 份、目录回到 /app/data/backups）？', { okText: '恢复默认' });
        if (!okc) return;
        for (const k of ['backup_dir', 'backup_remote_dir', 'backup_keep_local', 'backup_keep_remote', 'backup_interval_hours', 'backup_at_boot']) {
          try { await api.backupDelConfig(k); } catch (e) { /* 单项失败不影响其余 */ }
        }
        await reload(true);
        U.toast('已恢复默认配置', 'success');
      };
    }

    /* 筛选 */
    const kw = root.querySelector('#bk-kw');
    if (kw) {
      kw.oninput = U.debounce(() => { ui.filter = kw.value; WB.rerender(); setTimeout(() => { const el = root.querySelector('#bk-kw'); if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }, 0); }, 260);
    }
    const more = root.querySelector('#bk-more');
    if (more) more.onclick = () => { ui.showAll = true; WB.rerender(); };

    /* 裁剪 */
    const prune = root.querySelector('#bk-prune');
    if (prune) {
      prune.onclick = async () => {
        try {
          const r = await api.backupPrune();
          U.toast(`裁剪完成：本地 ${r.local_pruned} 份，外挂 ${r.remote_pruned} 份`);
          await reload(false);
        } catch (e) { /* 已 toast */ }
      };
    }

    /* 下载 / 还原 */
    root.querySelectorAll('[data-dl]').forEach((b) => { b.onclick = () => downloadBackup(b.dataset.dl); });
    root.querySelectorAll('[data-restore]').forEach((b) => { b.onclick = () => restoreFlow(b.dataset.restore); });

    /* 上传还原 */
    const fileInput = root.querySelector('#bk-file');
    const upBtn = root.querySelector('#bk-upload');
    if (upBtn && fileInput) {
      upBtn.onclick = () => fileInput.click();
      fileInput.onchange = async () => {
        const f = fileInput.files && fileInput.files[0];
        fileInput.value = '';
        if (!f) return;
        const ok1 = await U.confirm(
          `上传备份包「${f.name}」（${fmtBytes(f.size)}）并立即覆盖当前数据？`,
          { title: '上传还原', okText: '我了解风险', danger: true },
        );
        if (!ok1) return;
        const word = await U.prompt('请输入 RESTORE 四个字母以确认', '', { placeholder: 'RESTORE', confirmText: '确认还原', danger: true });
        if (!word || String(word).trim().toUpperCase() !== 'RESTORE') {
          U.toast('确认词不对，已取消', 'warn');
          return;
        }
        try {
          const r = await api.backupUploadRestore(f);
          U.toast(r.message || '还原完成', 'success', 4000);
          await reload(true);
        } catch (e) { /* 已 toast */ }
      };
    }
  }

  WB.views.backup = async function (root) {
    await Promise.all([loadStatus(false), loadList()]);
    const s = cachedStatus || {};
    root.innerHTML = `
      ${renderHero(s)}
      ${renderStorage(s)}
      ${renderPolicy(s)}
      ${renderLast(s)}
      ${renderList(s)}
      ${renderDanger()}
    `;
    bind(root);
    return { title: '数据备份', sub: '存储位置 · 周期策略 · 备份与还原', toolbar: '' };
  };
})();
