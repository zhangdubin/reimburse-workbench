/* 视图：单点登录身份源配置（v2.9.23，仅管理员）
 *
 * 一个统一门户 = 一条身份源记录：协议、端点、凭据、字段映射、落地策略全在这儿配，
 * 不改配置文件、不重启服务。配完要在门户侧把「回调地址」注册上去，
 * 所以每张卡片都把这个地址摆在最显眼的位置，并提供一键复制。
 *
 * 页面三段：
 *   1. 顶部速览 —— 有几个来源、几个启用、用哪种协议。
 *   2. 卡片列表 —— 每个来源一张卡：回调地址 / 状态开关 / 测试 / 编辑 / 删除。
 *   3. 编辑弹窗 —— 五个分组：协议端点、凭据、字段映射、角色映射、落地策略；
 *      外加「OIDC 自动发现」和「字段映射预览」两个排障用的工具。
 */
window.WB = window.WB || {};
WB.views = WB.views || {};

(function () {
  const U = WB.util;
  const api = WB.api;

  /* 不同协议要填的字段不同：这里决定弹窗显示哪些分组 */
  const ENDPOINT_FIELDS = {
    oidc: ['issuer', 'authorize_url', 'token_url', 'userinfo_url', 'jwks_url'],
    oauth2: ['authorize_url', 'token_url', 'userinfo_url'],
    cas3: ['authorize_url', 'validate_url'],
    jwt: ['jwks_url'],
  };

  const FIELDS = {
    name: { label: '显示名称', placeholder: '集团统一门户', group: 'basic' },
    protocol: { label: '协议', kind: 'protocol', group: 'basic' },
    issuer: { label: '签发方 issuer', placeholder: 'https://portal.corp.com', help: 'OIDC / JWT 校验 iss 用；OIDC 还能靠它自动发现端点', group: 'endpoint' },
    authorize_url: { label: '登录地址', placeholder: 'https://portal.corp.com/oauth/authorize', help: 'OAuth2/OIDC 的 authorize；CAS 填 /login', group: 'endpoint' },
    token_url: { label: '令牌地址', placeholder: 'https://portal.corp.com/oauth/token', group: 'endpoint' },
    userinfo_url: { label: '用户信息地址', placeholder: 'https://portal.corp.com/oauth/userinfo', group: 'endpoint' },
    validate_url: { label: '票据校验地址', placeholder: 'https://portal.corp.com/cas/serviceValidate', help: 'CAS 的 serviceValidate', group: 'endpoint' },
    jwks_url: { label: '公钥地址', placeholder: 'https://portal.corp.com/.well-known/jwks.json', help: 'RS256 用；自研门户也可以直接把 PEM 公钥整段贴进来', group: 'endpoint' },
    logout_url: { label: '门户登出地址', placeholder: 'https://portal.corp.com/logout', help: '配了之后本系统登出会连带从门户登出（单点登出）', group: 'endpoint' },
    client_id: { label: 'client_id', group: 'cred' },
    client_secret: { label: 'client_secret', kind: 'secret', group: 'cred', help: '加密存储；留空表示不修改' },
    scope: { label: 'scope', placeholder: 'openid profile email', group: 'cred' },
    jwt_secret: { label: 'JWT 验签密钥', kind: 'secret', group: 'cred', help: 'HS256 且与 client_secret 不同时填这里' },
    jwt_audience: { label: '受众 aud', group: 'cred', help: '留空表示不校验 aud' },
    claim_subject: { label: '唯一标识', help: '门户侧唯一 ID，账号绑定就看它', group: 'claim' },
    claim_username: { label: '登录名', group: 'claim' },
    claim_name: { label: '姓名', group: 'claim' },
    claim_employee_no: { label: '工号', help: '用于自动绑定员工档案，从而带上部门/人事信息', group: 'claim' },
    claim_department: { label: '部门', group: 'claim' },
    claim_email: { label: '邮箱', group: 'claim' },
    claim_phone: { label: '手机', group: 'claim' },
    claim_groups: { label: '分组/角色字段', group: 'claim' },
  };

  const GROUPS = [
    { id: 'basic', title: '基本信息', desc: '名字与协议' },
    { id: 'endpoint', title: '门户端点', desc: '各协议填各自需要的地址' },
    { id: 'cred', title: '凭据与作用域', desc: 'client_id / secret 一律加密入库' },
    { id: 'claim', title: '字段映射', desc: '门户返回的字段名，支持 a.b.c 多级路径' },
    { id: 'policy', title: '落地策略', desc: '首登是否开户、落到什么角色、会话时长' },
  ];

  function esc(v) {
    return U.esc(v === undefined || v === null ? '' : String(v));
  }

  function inputRow(fid, p, value, cls) {
    const f = FIELDS[fid];
    const isSecret = f.kind === 'secret';
    return `<div class="${esc(cls || 'sso-fcol')}" data-col="${fid}">
      <label>${esc(f.label)}</label>
      <input type="${isSecret ? 'password' : 'text'}" data-f="${fid}"
        value="${esc(value === null || value === undefined ? '' : value)}"
        placeholder="${esc(f.placeholder || '')}"${isSecret ? ' autocomplete="new-password"' : ''}>
      <div class="sso-fhint">${f.help ? esc(f.help) : ''}</div>
    </div>`;
  }

  function formHtml(p, protocols, roles) {
    const proto = p ? p.protocol : 'oidc';
    return `
      <div class="sso-form">
        ${GROUPS.map((g) => {
          let cols = '';
          if (g.id === 'basic') {
            cols = inputRow('name', p, p && p.name) + `
              <div class="sso-fcol" data-col="protocol">
                <label>协议</label>
                <select data-f="protocol" id="sso-protocol">
                  ${protocols.map((x) => `<option value="${esc(x.value)}" ${proto === x.value ? 'selected' : ''}>${esc(x.label)}</option>`).join('')}
                </select>
              </div>`;
          } else if (g.id === 'endpoint') {
            cols = ENDPOINT_FIELDS[proto].concat(['logout_url'])
              .map((fid) => inputRow(fid, p, p && p[fid], 'sso-fcol sso-col-full')).join('');
          } else if (g.id === 'cred') {
            const list = proto === 'jwt' ? ['client_id', 'jwt_secret', 'jwt_audience']
              : proto === 'cas3' ? ['client_id']
              : ['client_id', 'client_secret', 'scope'];
            cols = list.map((fid) => inputRow(fid, p, p && p[fid])).join('');
            if (proto === 'jwt') {
              cols += `<div class="sso-fcol" data-col="jwt_source">
                <label>令牌来源</label>
                <select data-f="jwt_source">
                  <option value="query"${(p && p.jwt_source) !== 'header' ? ' selected' : ''}>URL 参数</option>
                  <option value="header"${(p && p.jwt_source) === 'header' ? ' selected' : ''}>HTTP Header</option>
                </select>
                <div class="sso-fhint">参数名 / Header 名在「字段映射」外的 jwt_param 里，默认 token</div>
              </div>` + inputRow('jwt_param', p, p ? p.jwt_param : 'token');
            }
            if (proto === 'oidc' || proto === 'oauth2') {
              cols += `<div class="sso-fcol sso-col-full" data-col="use_pkce">
                <label class="sso-check"><input type="checkbox" data-f="use_pkce" ${p && p.use_pkce ? 'checked' : ''}>
                <span>启用 PKCE（门户支持的话建议开启，防止授权码被劫持）</span></label>
              </div>`;
            }
          } else if (g.id === 'claim') {
            cols = ['claim_subject', 'claim_username', 'claim_name', 'claim_employee_no',
              'claim_department', 'claim_email', 'claim_phone', 'claim_groups']
              .map((fid) => inputRow(fid, p, p && p[fid])).join('');
          } else {
            const rm = (p && p.role_map) || {};
            cols = `
              <div class="sso-fcol" data-col="default_role">
                <label>默认角色</label>
                <select data-f="default_role">
                  ${roles.map((r) => `<option value="${esc(r)}" ${p && p.default_role === r ? 'selected' : ''}>${esc(r)}</option>`).join('')}
                </select>
              </div>
              <div class="sso-fcol" data-col="session_hours">
                <label>会话时长（小时）</label>
                <input type="number" min="0" max="720" data-f="session_hours" value="${esc(p ? p.session_hours : 12)}">
                <div class="sso-fhint">0 表示跟随全局设置</div>
              </div>
              <div class="sso-fcol sso-col-full" data-col="policies">
                <label class="sso-check"><input type="checkbox" data-f="auto_create" ${!p || p.auto_create ? 'checked' : ''}>
                <span>首次门户登录自动开通本地账号（关掉则只能由管理员先建号）</span></label>
                <label class="sso-check"><input type="checkbox" data-f="sync_profile" ${!p || p.sync_profile ? 'checked' : ''}>
                <span>每次登录同步资料（姓名、工号绑定；只补空缺的员工信息，不覆盖已有人事数据）</span></label>
                <label class="sso-check"><input type="checkbox" data-f="allow_local_login" ${!p || p.allow_local_login ? 'checked' : ''}>
                <span>同时允许账密登录（关闭后该来源的账号只能走门户）</span></label>
                <label class="sso-check"><input type="checkbox" data-f="bind_local_by_username" ${p && p.bind_local_by_username ? 'checked' : ''}>
                <span>同名本地账号自动归户（谨慎：管理员账号永不参与自动归户）</span></label>
                <label class="sso-check"><input type="checkbox" data-f="enabled" ${p && p.enabled ? 'checked' : ''}>
                <span>启用（登录页显示该入口）</span></label>
              </div>
              <div class="sso-fcol sso-col-full" data-col="role_map">
                <label>门户分组 → 本系统角色</label>
                <div class="sso-rolemap" id="sso-rolemap"></div>
                <div class="sso-fhint">没匹配到分组的账号落到「默认角色」</div>
              </div>
              <div class="sso-fcol sso-col-full" data-col="remark">
                <label>备注</label>
                <input type="text" data-f="remark" value="${esc(p && p.remark)}" placeholder="对接人 / 上线日期等">
              </div>`;
          }
          return `<section class="sso-group"><div class="sso-g-head"><span>${esc(g.title)}</span><em>${esc(g.desc)}</em></div>
            <div class="sso-grid">${cols}</div></section>`;
        }).join('')}
      </div>`;
  }

  /* 角色映射编辑器：分组名 → 角色下拉，可增删行。本身就是最终数据的来源。 */
  function bindRoleMap(el, initial, roles) {
    const box = U.qs('#sso-rolemap', el);
    if (!box) return;
    const addBtn = document.createElement('button');
    addBtn.className = 'btn btn-sm btn-primary rm-add';
    addBtn.type = 'button';
    addBtn.textContent = '+ 增加映射';
    addBtn.onclick = () => addRow('', roles[0]);
    box.parentElement.appendChild(addBtn);

    function addRow(k, v) {
      const hold = document.createElement('div');
      hold.className = 'sso-rm-row';
      hold.innerHTML = `
        <input class="rm-k" placeholder="门户分组名" value="${esc(k || '')}">
        <select class="rm-v">${roles.map((r) => `<option value="${esc(r)}"${String(v) === String(r) ? ' selected' : ''}>${esc(r)}</option>`).join('')}</select>
        <button type="button" class="btn btn-sm btn-ghost rm-del" title="删除该映射">删除</button>`;
      hold.querySelector('.rm-del').onclick = () => hold.remove();
      box.appendChild(hold);
    }

    Object.entries(initial || {}).forEach(([k, v]) => addRow(k, v));
    if (!Object.keys(initial || {}).length) addRow('', roles[0]);
  }

  function collectMap(el) {
    const out = {};
    U.qsa('.sso-rm-row', el).forEach((r) => {
      const k = (U.qs('.rm-k', r).value || '').trim();
      const v = U.qs('.rm-v', r).value;
      if (k) out[k] = v;
    });
    return out;
  }

  function collectForm(el) {
    const data = { role_map: collectMap(el) };
    U.qsa('[data-f]', el).forEach((input) => {
      const k = input.dataset.f;
      if (k === 'protocol') data.protocol = input.value;
      else if (k === 'jwt_source' || k === 'default_role') data[k] = input.value;
      else if (input.type === 'checkbox') data[k] = input.checked;
      else if (k === 'session_hours') data[k] = Number(input.value || 0);
      else data[k] = input.value.trim();
    });
    return data;
  }

  WB.views.sso = async function (root) {
    let data = await api.ssoAdmin();
    let protocols = (data.protocols || []).map((x) => ({ value: x.value, label: x.label }));
    let roles = data.roles || [];

    function cardHtml(p) {
      const badge = p.enabled
        ? '<span class="sso-tag ok">已启用</span>'
        : '<span class="sso-tag">已停用</span>';
      return `
        <section class="sso-card ${p.enabled ? 'on' : ''}">
          <div class="sso-c-head">
            <div class="sso-c-title">
              <b>${esc(p.name)}</b>
              <span class="sso-proto">${esc(p.protocol_label)}</span>
              ${badge}
            </div>
            <div class="sso-c-actions">
              <button class="btn btn-sm" data-act="toggle">${p.enabled ? '停用' : '启用'}</button>
              <button class="btn btn-sm" data-act="test">连通性测试</button>
              <button class="btn btn-sm btn-primary" data-act="edit">编辑</button>
              <button class="btn btn-sm btn-danger" data-act="del">删除</button>
            </div>
          </div>
          <div class="sso-c-row">
            <span class="sso-c-k">回调地址（填到门户）</span>
            <code class="sso-c-url" data-url="${esc(p.callback_url)}">${esc(p.callback_url)}</code>
            <button class="btn btn-xs" data-act="copy">复制</button>
          </div>
          <div class="sso-c-row">
            <span class="sso-c-k">默认角色</span><span class="sso-c-v">${esc(p.default_role)}</span>
            <span class="sso-c-k">会话时长</span><span class="sso-c-v">${p.session_hours ? p.session_hours + ' 小时' : '跟随全局'}</span>
            <span class="sso-c-k">自动开户</span><span class="sso-c-v">${p.auto_create ? '开' : '关'}</span>
          </div>
          ${p.remark ? `<div class="sso-c-remark">${esc(p.remark)}</div>` : ''}
        </section>`;
    }

    function metricsHtml() {
      const total = data.items.length;
      const on = data.items.filter((x) => x.enabled).length;
      const used = Array.from(new Set(data.items.map((x) => x.protocol_label))).join(' / ') || '—';
      return `
        <div class="set-metrics">
          <div class="set-metric m-blue"><span class="sm-ico">◈</span>
            <div class="sm-body"><div class="sm-label">身份源</div><div class="sm-value">${total}</div>
            <div class="sm-foot">已启用 ${on} 个</div></div></div>
          <div class="set-metric m-purple"><span class="sm-ico">⇄</span>
            <div class="sm-body"><div class="sm-label">登录入口</div><div class="sm-value">${on}</div>
            <div class="sm-foot">出现在登录页的按钮数</div></div></div>
          <div class="set-metric m-orange"><span class="sm-ico">⌘</span>
            <div class="sm-body"><div class="sm-label">协议</div><div class="sm-value sso-protocol-val">${esc(used)}</div>
            <div class="sm-foot">同一系统可并存多协议来源</div></div></div>
        </div>`;
    }

    function render() {
      root.innerHTML = `
        <div class="card set-card">
          <div class="card-head">
            <h3>单点登录身份源</h3>
            <span class="hint">支持 OAuth2 / OIDC / CAS 3.0 / JWT 令牌直通，多来源可并存</span>
            <div class="right"><button class="btn btn-sm btn-primary" id="sso-new">+ 新增身份源</button></div>
          </div>
          <div class="set-body">
            ${metricsHtml()}
            ${data.items.length ? data.items.map(cardHtml).join('') : `
              <div class="empty" style="padding:44px">
                <div>还没有配置任何统一门户</div>
                <div class="muted" style="font-size:12.5px;margin-top:6px">
                  新增后把页面给出的「回调地址」登记到门户侧的应用配置里即可生效
                </div>
              </div>`}
          </div>
        </div>`;
      bind();
    }

    function bind() {
      U.qs('#sso-new', root).onclick = () => openEditor(null);
      U.qsa('.sso-card', root).forEach((card, idx) => {
        const cur = data.items[idx];
        if (!cur) return;
        card.querySelector('[data-act="toggle"]').onclick = async () => {
          try {
            await api.ssoToggle(cur.id, !cur.enabled);
            U.toast(cur.enabled ? '已停用' : '已启用', 'success');
            await reload();
          } catch (e) {}
        };
        card.querySelector('[data-act="test"]').onclick = async () => {
          try {
            const r = await api.ssoTest(cur.id);
            const lines = (r.checks || []).map((c) => `${c.ok ? '✓' : '✗'} ${c.name}：${c.detail}`).join('\n');
            const notes = (r.notes || []).join('\n');
            U.modal({
              title: `连通性测试 · ${cur.name}`,
              width: 640,
              body: `<pre class="sso-test">${U.esc(lines || '无检查项')}${notes ? '\n\n' + U.esc(notes) : ''}</pre>`,
              footer: `<button class="btn" data-close>关闭</button>`,
              onMount(mApi) { U.qs('[data-close]', mApi.el).onclick = () => mApi.close(); },
            });
          } catch (e) {}
        };
        card.querySelector('[data-act="edit"]').onclick = () => openEditor(cur);
        card.querySelector('[data-act="del"]').onclick = async () => {
          const ok = await U.confirm(`删除身份源「${cur.name}」？已通过该来源登录过的账号不会被删除，但会失去绑定关系。`, { okText: '删除' });
          if (!ok) return;
          try {
            await api.ssoDelete(cur.id);
            U.toast('已删除', 'success');
            await reload();
          } catch (e) {}
        };
        card.querySelector('[data-act="copy"]').onclick = () => {
          const url = card.querySelector('.sso-c-url').dataset.url;
          navigator.clipboard?.writeText(url).then(
            () => U.toast('回调地址已复制', 'success'),
            () => U.toast(url, 'info')
          );
        };
      });
    }

    async function reload() {
      data = await api.ssoAdmin();
      protocols = (data.protocols || []).map((x) => ({ value: x.value, label: x.label }));
      roles = data.roles || [];
      render();
    }

    /* ---------------- 编辑弹窗 ---------------- */
    function openEditor(p) {
      const isNew = !p;
      const body = formHtml(p, protocols, roles);
      const m = U.modal({
        title: isNew ? '新增身份源' : `编辑 · ${p.name}`,
        width: 880,
        body: `
          ${p ? `<div class="sso-cb"><span>回调地址</span><code>${esc(p.callback_url)}</code></div>` : ''}
          ${isNew ? `<div class="sso-cb sso-cb-inline"><span>issuer 自动发现</span>
            <input id="sso-disc-issuer" placeholder="https://portal.corp.com">
            <button class="btn btn-sm" id="sso-disc-btn">拉取端点</button></div>` : ''}
          ${body}
          <div class="sso-tools">
            <div class="sso-tools-head">排障工具：字段映射预览</div>
            <textarea id="sso-raw" rows="5" placeholder='把门户返回的 userinfo / id_token 载荷贴进来，例如 {"sub":"1001","username":"zhangsan","name":"张三","groups":["财务组"]}'></textarea>
            <div class="sso-tools-row">
              <button class="btn btn-sm" id="sso-preview">预览映射结果</button>
              <span class="muted" id="sso-preview-out" style="font-size:12.5px"></span>
            </div>
          </div>`,
        footer: `<button class="btn" data-close>取消</button><button class="btn btn-primary" data-ok>${isNew ? '创建' : '保存'}</button>`,
        onMount(api2) {
          const el = api2.el;
          bindRoleMap(el, p && p.role_map, roles);

          // 切协议要重画弹窗（不同协议的字段集合不同）
          U.qs('#sso-protocol', el).onchange = async () => {
            const cur = collectForm(el);
            const next = Object.assign({}, p || {}, cur, { protocol: U.qs('#sso-protocol', el).value });
            api2.close();
            openEditor(Object.assign({ id: p ? p.id : 0, callback_url: p ? p.callback_url : '' }, next));
            U.toast('已切换协议，请补齐对应端点', 'info');
          };

          const btn = U.qs('#sso-disc-btn', el);
          if (btn) {
            btn.onclick = async () => {
              const issuer = U.qs('#sso-disc-issuer', el).value.trim();
              if (!issuer) return U.toast('请填写 issuer', 'warn');
              try {
                const info = await api.ssoDiscover(issuer, { verifySsl: true });
                ['authorize_url', 'token_url', 'userinfo_url', 'jwks_url'].forEach((k) => {
                  const input = U.qs(`[data-f="${k}"]`, el);
                  if (input && info[k]) input.value = info[k];
                });
                const iss = U.qs('[data-f="issuer"]', el);
                if (iss && info.issuer) iss.value = info.issuer;
                U.toast('已回填端点，请核对后保存', 'success');
              } catch (e) {}
            };
          }

          U.qs('#sso-preview', el).onclick = async () => {
            if (!p) return U.toast('请先创建成功后再做映射预览', 'warn');
            let raw = {};
            try {
              raw = JSON.parse(U.qs('#sso-raw', el).value || '{}');
            } catch (_) {
              return U.toast('JSON 解析失败，请检查格式', 'warn');
            }
            try {
              const r = await api.ssoMapPreview(p.id, raw);
              U.qs('#sso-preview-out', el).innerHTML =
                `登录名 <b>${esc(r.profile.username)}</b> · 姓名 <b>${esc(r.profile.name)}</b> · 工号 <b>${esc(r.profile.employee_no || '未取到')}</b> · 角色 <b>${esc(r.role)}</b>`;
            } catch (e) {}
          };

          U.qs('[data-ok]', el).onclick = async () => {
            const payload = collectForm(el);
            if (!payload.name) return U.toast('请填写显示名称', 'warn');
            // 密钥没改就回 KEEP，避免把掩码当初值保存回去
            if (!isNew) {
              ['client_secret', 'jwt_secret'].forEach((k) => {
                if (!payload[k]) payload[k] = 'KEEP';
              });
            }
            try {
              if (isNew) await api.ssoCreate(payload);
              else await api.ssoUpdate(p.id, payload);
              U.toast(isNew ? '身份源已创建' : '已保存', 'success');
              api2.close();
              await reload();
            } catch (e) {}
          };
          U.qs('[data-close]', el).onclick = () => api2.close();
        },
      });
      return m;
    }

    render();

    return {
      title: '单点登录',
      sub: '对接统一门户：多协议 SSO 配置',
      toolbar: `<button class="btn btn-sm" id="btn-refresh">刷新</button>`,
      onToolbar(tb) {
        U.qs('#btn-refresh', tb).onclick = () => WB.rerender();
      },
    };
  };
})();
