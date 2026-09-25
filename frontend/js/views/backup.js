/* 数据备份（v2.9.13+）：管理员独立页面，与「系统更新」同级。
 *
 * 设计要点（与现有页面风格一致）：
 * - 顶部状态卡：上次备份结果、本地/外挂目录、周期
 * - 配置区：周期、本地/外挂目录、保留份数、启动即备份（每个写一项就 PUT 一次）
 * - 备份列表：本地+外挂合并展示，每条带下载 / 还原按钮
 * - 还原：要求二次确认 + 输入「RESTORE」字串确认（防误点）
 * - 上传包还原：本地不可用时可从其他机器打包传过来
 */
window.WB = window.WB || {};
WB.views = WB.views || {};

(function () {
  const U = WB.util;
  const api = WB.api;

  let cachedStatus = null;
  let cachedList = null;

  function fmtBytes(n) {
    if (!n && n !== 0) return '-';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1024 * 1024 * 1024) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
  }

  function fmtTime(iso) {
    if (!iso) return '-';
    try { return iso.replace('T', ' ').slice(0, 19); } catch (_) { return iso; }
  }

  function fmtAgo(iso) {
    if (!iso) return '-';
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return '-';
    const dt = (Date.now() - t) / 1000;
    if (dt < 60) return Math.floor(dt) + ' 秒前';
    if (dt < 3600) return Math.floor(dt / 60) + ' 分钟前';
    if (dt < 86400) return Math.floor(dt / 3600) + ' 小时前';
    return Math.floor(dt / 86400) + ' 天前';
  }

  function chip(r) {
    if (!r) return '<span class="bk-chip muted">暂无备份</span>';
    if (r.ok === false || r.error) return `<span class="bk-chip err">失败：${U.esc(r.error || '未知错误')}</span>`;
    return `<span class="bk-chip ok">最近成功 · ${fmtAgo(r.finished_at)}</span>`;
  }

  async function loadStatus() {
    try {
      cachedStatus = await api.backupStatus();
    } catch (e) { /* api 层已 toast */ }
    return cachedStatus || {};
  }

  async function loadList() {
    try {
      const r = await api.backupList();
      cachedList = r.items || [];
    } catch (e) { cachedList = []; }
    return cachedList;
  }

  function renderHeader() {
    const s = cachedStatus || {};
    const cfg = s.config || {};
    const last = s.last_result || {};
    return `
      <div class="bk-head card">
        <div class="bk-head-l">
          <div class="bk-title">数据备份与还原</div>
          <div class="bk-sub">管理员专属 · 自动周期备份到本地与外挂目录 · 支持跨机还原</div>
        </div>
        <div class="bk-head-r">
          ${chip(last)}
        </div>
      </div>
      <div class="bk-grid card">
        <div class="bk-stat">
          <span class="bk-stat-l">本地目录</span>
          <span class="bk-stat-v" title="${U.esc(cfg.backup_dir || '-')}">${U.esc(cfg.backup_dir || '-')}</span>
        </div>
        <div class="bk-stat">
          <span class="bk-stat-l">外挂目录</span>
          <span class="bk-stat-v" title="${U.esc(cfg.backup_remote_dir || '（未启用）')}">${cfg.backup_remote_dir ? U.esc(cfg.backup_remote_dir) : '<em class="muted">未启用</em>'}</span>
        </div>
        <div class="bk-stat">
          <span class="bk-stat-l">自动周期</span>
          <span class="bk-stat-v">每 ${cfg.backup_interval_hours} 小时 · 启动期${cfg.backup_at_boot ? '立即备份' : '不备份'}</span>
        </div>
        <div class="bk-stat">
          <span class="bk-stat-l">保留份数</span>
          <span class="bk-stat-v">本地 ${cfg.backup_keep_local} · 外挂 ${cfg.backup_keep_remote}</span>
        </div>
      </div>`;
  }

  function renderLast(s) {
    const r = (s || {}).last_result || {};
    if (!r || (!r.ok && !r.error && !r.name)) {
      return '<div class="card bk-empty">尚无备份记录。点击右上方「立即备份」开始。</div>';
    }
    if (r.ok === false || r.error) {
      return `<div class="card bk-last err">
        <div class="bk-last-h">最近一次备份失败</div>
        <div class="bk-last-d">${U.esc(r.error || '未知错误')}</div>
        <div class="bk-last-meta">${U.esc(r.finished_at || '')}</div>
      </div>`;
    }
    return `<div class="card bk-last">
      <div class="bk-last-h">最近一次备份</div>
      <div class="bk-last-name">${U.esc(r.name || '')}</div>
      <div class="bk-last-grid">
        <div><b>大小</b><span>${fmtBytes(r.size)}</span></div>
        <div><b>sha256</b><span>${U.esc((r.sha256 || '').slice(0, 16))}…</span></div>
        <div><b>表行数</b><span>${r.table_rows ?? '-'}</span></div>
        <div><b>上传文件</b><span>${r.uploaded_files ?? '-'}</span></div>
        <div><b>耗时</b><span>${r.elapsed_seconds ?? '-'} s</span></div>
        <div><b>完成时间</b><span>${U.esc(fmtTime(r.finished_at || ''))}</span></div>
        <div><b>外挂副本</b><span>${r.remote_path ? U.esc(r.remote_path) : (r.remote_error ? '失败：' + U.esc(r.remote_error) : '未启用')}</span></div>
      </div>
    </div>`;
  }

  function renderConfig(s) {
    const cfg = (s || {}).config || {};
    const keys = (s || {}).config_keys || {};
    const ROWS = [
      { key: 'backup_dir', label: keys.backup_dir || '本地备份根目录', type: 'text' },
      { key: 'backup_remote_dir', label: keys.backup_remote_dir || '外挂备份目录（空=关闭）', type: 'text' },
      { key: 'backup_interval_hours', label: keys.backup_interval_hours || '自动周期（小时，0=关闭）', type: 'text' },
      { key: 'backup_at_boot', label: keys.backup_at_boot || '容器启动立即备份', type: 'switch' },
      { key: 'backup_keep_local', label: keys.backup_keep_local || '本地保留份数', type: 'text' },
      { key: 'backup_keep_remote', label: keys.backup_keep_remote || '外挂保留份数', type: 'text' },
    ];
    const rows = ROWS.map((r) => {
      const cur = cfg[r.key];
      const isSwitch = r.type === 'switch';
      const showVal = isSwitch ? (cur ? '1' : '0') : (cur == null ? '' : String(cur));
      const helpKey = (s && s.config_keys) ? '' : ''; // 占位
      return `
        <div class="bk-cfg-row">
          <div class="bk-cfg-l">
            <div class="bk-cfg-label">${U.esc(r.label)}</div>
          </div>
          <div class="bk-cfg-r">
            ${isSwitch
              ? `<label class="switch"><input type="checkbox" data-cfg="${r.key}" ${cur ? 'checked' : ''}/><span class="slider"></span></label>`
              : `<input class="bk-input" data-cfg="${r.key}" value="${U.esc(showVal)}" />`}
            <button class="btn btn-xs" data-save="${r.key}">保存</button>
          </div>
        </div>`;
    }).join('');
    return `
      <div class="card bk-cfg">
        <div class="bk-h">备份配置</div>
        <div class="bk-cfg-list">${rows}</div>
      </div>`;
  }

  function renderActions() {
    return `
      <div class="card bk-actions">
        <button class="btn primary" id="bk-run">立即备份</button>
        <button class="btn" id="bk-prune">裁剪到保留份数</button>
        <button class="btn danger" id="bk-upload">上传备份包并还原</button>
        <input type="file" id="bk-file" accept=".gz,.tar.gz" style="display:none" />
      </div>`;
  }

  function renderList() {
    const list = cachedList || [];
    if (!list.length) {
      return '<div class="card bk-empty">暂无备份包。点击「立即备份」生成第一份。</div>';
    }
    const local = list.filter((x) => x.where === 'local');
    const remote = list.filter((x) => x.where === 'remote');
    function block(title, items, where) {
      if (!items.length) return '';
      const rows = items.map((it) => `
        <div class="bk-item">
          <div class="bk-item-name" title="${U.esc(it.path)}">${U.esc(it.name)}</div>
          <div class="bk-item-meta">${fmtBytes(it.size)} · ${U.esc(fmtTime(it.mtime_iso))}</div>
          <div class="bk-item-act">
            <button class="btn btn-xs" data-dl="${U.esc(it.name)}">下载</button>
            <button class="btn btn-xs btn-danger" data-restore="${U.esc(it.name)}" data-where="${where}">从此还原</button>
          </div>
        </div>`).join('');
      return `<div class="bk-list-block">
        <div class="bk-list-h">${title}（${items.length}）</div>
        ${rows}
      </div>`;
    }
    return `<div class="card bk-list">
      ${block('本地', local, 'local')}
      ${block('外挂', remote, 'remote')}
    </div>`;
  }

  function bind(root) {
    root.querySelector('#bk-run').onclick = async () => {
      const btn = root.querySelector('#bk-run');
      btn.disabled = true; btn.textContent = '备份中…';
      try {
        const r = await api.backupRun();
        U.toast(`备份完成：${r.result.name}（${fmtBytes(r.result.size)}）`);
        await Promise.all([loadStatus(), loadList()]);
        WB.rerender();
      } catch (e) { /* api 已 toast */ }
      finally { btn.disabled = false; btn.textContent = '立即备份'; }
    };
    root.querySelector('#bk-prune').onclick = async () => {
      const r = await api.backupPrune();
      U.toast(`裁剪完成：本地 ${r.local_pruned} 份，外挂 ${r.remote_pruned} 份`);
      await loadList();
      WB.rerender();
    };
    // 配置保存
    root.querySelectorAll('[data-save]').forEach((b) => {
      b.onclick = async () => {
        const k = b.dataset.save;
        const el = root.querySelector(`[data-cfg="${k}"]`);
        let val = '';
        if (el.type === 'checkbox') val = el.checked ? '1' : '0';
        else val = el.value.trim();
        try {
          await api.backupSetConfig(k, val);
          U.toast(`已保存：${k}`);
          await loadStatus();
          WB.rerender();
        } catch (e) { /* 已 toast */ }
      };
    });
    // 下载
    root.querySelectorAll('[data-dl]').forEach((b) => {
      b.onclick = () => {
        const name = b.dataset.dl;
        const url = api.backupDownloadUrl(name);
        // 走 Bearer 头：不能用 <a>，走 fetch 转 blob
        const t = WB.auth && WB.auth.token;
        fetch(url, { headers: t ? { Authorization: 'Bearer ' + t } : {} })
        .then((res) => {
          if (!res.ok) throw new Error('下载失败 HTTP ' + res.status);
          return res.blob();
        })
        .then((blob) => {
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = name;
          document.body.appendChild(a); a.click(); a.remove();
          setTimeout(() => URL.revokeObjectURL(a.href), 4000);
        })
        .catch((e) => U.toast(e.message, 'error'));
      };
    });
    // 还原
    root.querySelectorAll('[data-restore]').forEach((b) => {
      b.onclick = async () => {
        const name = b.dataset.restore;
        const ok1 = await U.confirm(
          `确定要从「${name}」还原吗？\n还原会覆盖当前数据库与上传文件，且不可撤销。`,
          { okText: '我了解风险', danger: true },
        );
        if (!ok1) return;
        const token = await U.prompt('请输入 RESTORE 四个字母以确认', '', { placeholder: 'RESTORE', confirmText: '确认还原' });
        if (!token || token.trim().toUpperCase() !== 'RESTORE') {
          U.toast('确认词不对，已取消', 'warn');
          return;
        }
        try {
          const r = await api.backupRestore(name);
          U.toast(r.message, 'success');
          await loadStatus();
          WB.rerender();
        } catch (e) { /* 已 toast */ }
      };
    });
    // 上传还原
    const fileInput = root.querySelector('#bk-file');
    root.querySelector('#bk-upload').onclick = () => fileInput.click();
    fileInput.onchange = async () => {
      const f = fileInput.files && fileInput.files[0];
      fileInput.value = '';
      if (!f) return;
      const ok1 = await U.confirm(
        `上传备份包「${f.name}」（${fmtBytes(f.size)}）并立即覆盖当前数据？`,
        { okText: '我了解风险', danger: true },
      );
      if (!ok1) return;
      const token = await U.prompt('请输入 RESTORE 四个字母以确认', '', { placeholder: 'RESTORE', confirmText: '确认还原' });
      if (!token || token.trim().toUpperCase() !== 'RESTORE') {
        U.toast('确认词不对，已取消', 'warn');
        return;
      }
      try {
        const r = await api.backupUploadRestore(f);
        U.toast(r.message, 'success');
        await Promise.all([loadStatus(), loadList()]);
        WB.rerender();
      } catch (e) { /* 已 toast */ }
    };
  }

  WB.views.backup = async function (root) {
    await Promise.all([loadStatus(), loadList()]);
    root.innerHTML = `
      ${renderHeader()}
      ${renderLast(cachedStatus)}
      ${renderActions()}
      ${renderConfig(cachedStatus)}
      ${renderList()}
    `;
    bind(root);
    return {
      title: '数据备份',
      sub: '周期备份 · 手动备份 · 跨机还原',
      toolbar: '',
    };
  };
})();