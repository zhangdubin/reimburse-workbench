/* 认证、会话与权限。
 *
 * 与后端一一对应：
 *   POST /api/auth/login    登录，拿 token
 *   POST /api/auth/logout   登出，服务端吊销 token
 *   GET  /api/auth/me       当前用户 + 审批阈值
 *   POST /api/auth/password 改密码（服务端会踢掉所有会话）
 *
 * 权限只做「界面可见性」这层过滤，真正的拦截一律由后端负责——
 * 前端隐藏按钮只是体验优化，不是安全边界。
 */
window.WB = window.WB || {};

(function () {
  const TOKEN_KEY = 'wb.token';
  const USER_KEY = 'wb.user';

  /* 角色 -> 权限集合。'*' 表示全部。
   *
   * 与后端路由守卫对应：
   *   _staff = 财务 + 管理员  -> inbox.view / inbox.import
   *   _admin = 仅管理员        -> inbox.account（邮箱账号增删改）/ inbox.purge
   *   AI 侧：所有登录用户都能用助手（数据范围由后端按其可见范围裁剪），
   *          但模型配置 / 用量（admin.ai）只有管理员能碰。
   */
  const ROLE_PERMS = {
    申请人: new Set(['reimb.create', 'reimb.edit', 'ai.use']),
    审批人: new Set([
      'reimb.create', 'reimb.edit', 'reimb.approve', 'reimb.reject', 'stats.analyst',
      'ai.use',
    ]),
    财务: new Set([
      'reimb.create', 'reimb.edit', 'reimb.pay',
      'invoice.write',
      'master.category.write', 'master.budget.write',
      'stats.analyst', 'settings.view',
      'inbox.view', 'inbox.import',
      'ai.use',
    ]),
    管理员: new Set(['*']),
  };

  const ROLE_TONE = {
    管理员: 'r-admin', 财务: 'r-finance', 审批人: 'r-approver', 申请人: 'r-applicant',
  };

  const auth = {
    token: null,
    user: null,

    /** 是否具备某项权限 */
    can(perm) {
      const s = ROLE_PERMS[this.user && this.user.role];
      if (!s) return false;
      return s.has('*') || s.has(perm);
    },

    is(...roles) {
      return !!this.user && roles.includes(this.user.role);
    },

    roleTone() {
      return ROLE_TONE[(this.user && this.user.role) || ''] || 'r-applicant';
    },

    /** 角色标签：审批人带上审批级别 */
    roleLabel() {
      const u = this.user;
      if (!u) return '';
      if (u.role === '审批人' && u.approval_level) return `审批人 · ${u.approval_level} 级`;
      return u.role;
    },

    readStorage() {
      this.token = localStorage.getItem(TOKEN_KEY) || null;
      try {
        this.user = JSON.parse(localStorage.getItem(USER_KEY) || 'null');
      } catch (_) {
        this.user = null;
      }
    },

    save(token, user) {
      this.token = token;
      this.user = user;
      if (token) localStorage.setItem(TOKEN_KEY, token);
      else localStorage.removeItem(TOKEN_KEY);
      if (user) localStorage.setItem(USER_KEY, JSON.stringify(user));
      else localStorage.removeItem(USER_KEY);
    },

    clear() {
      this.save(null, null);
    },

    async login(username, password) {
      // 登录接口自身会返回 401（密码错），这里刻意不走全局 401 跳转
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `登录失败 (${res.status})`);
      this.save(data.token, data.user);
      return data.user;
    },

    async logout() {
      try {
        if (this.token) {
          await fetch('/api/auth/logout', {
            method: 'POST',
            headers: { Authorization: 'Bearer ' + this.token },
          });
        }
      } catch (_) {
        /* 网络异常也要让本地登出，否则用户被卡住 */
      }
      this.clear();
    },

    async changePassword(oldPassword, newPassword) {
      const res = await fetch('/api/auth/password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + this.token },
        body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `修改失败 (${res.status})`);
      return data;
    },

    /** 用本地 token 换一次 /me，确认会话还有效 */
    async restore() {
      this.readStorage();
      if (!this.token) return null;
      try {
        const res = await fetch('/api/auth/me', {
          headers: { Authorization: 'Bearer ' + this.token },
        });
        if (!res.ok) {
          this.clear();
          return null;
        }
        const data = await res.json();
        this.user = data.user;
        localStorage.setItem(USER_KEY, JSON.stringify(data.user));
        WB.approval = data.approval || null;
        return data.user;
      } catch (_) {
        // 网络不通时不销毁 token，让页面自行提示
        return this.user;
      }
    },

    /** 会话失效（401）：清干净并弹回登录页 */
    onUnauthorized(message) {
      if (!this.token) return;
      this.clear();
      WB.util.toast(message || '登录状态已失效，请重新登录', 'warn');
      window.location.hash = '';
      this.showLogin();
    },

    /* -------------------- 登录页 -------------------- */
    showLogin(prefillError) {
      let root = document.getElementById('login-root');
      if (root) {
        root.style.display = '';
        const err = document.getElementById('lg-error');
        if (prefillError && err) {
          err.textContent = prefillError;
          err.style.display = '';
        }
        return;
      }
      root = document.createElement('div');
      root.id = 'login-root';
      root.innerHTML = `
        <div class="login-wrap">
          <div class="login-card">
            <div class="login-brand">
              <div class="brand-logo">销</div>
              <div>
                <b>销售费用报销管理工作台</b>
                <span>内网部署版</span>
              </div>
            </div>
            <form id="lg-form" autocomplete="on">
              <div class="form-item">
                <label for="lg-user">用户名</label>
                <input id="lg-user" name="username" autocomplete="username" placeholder="请输入用户名" required>
              </div>
              <div class="form-item">
                <label for="lg-pass">密码</label>
                <input id="lg-pass" name="password" type="password" autocomplete="current-password" placeholder="请输入密码" required>
              </div>
              <div class="lg-error" id="lg-error" style="display:none"></div>
              <button class="btn btn-primary lg-submit" type="submit" id="lg-submit">登 录</button>
            </form>
            <div class="login-foot">
              <span class="spin-hint" id="lg-status">检查服务状态…</span>
            </div>
          </div>
        </div>`;
      document.body.appendChild(root);

      const form = root.querySelector('#lg-form');
      const errBox = root.querySelector('#lg-error');
      const btn = root.querySelector('#lg-submit');

      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        errBox.style.display = 'none';
        const username = root.querySelector('#lg-user').value.trim();
        const password = root.querySelector('#lg-pass').value;
        if (!username || !password) {
          errBox.textContent = '请输入用户名和密码';
          errBox.style.display = '';
          return;
        }
        btn.disabled = true;
        btn.textContent = '登录中…';
        try {
          const user = await this.login(username, password);
          if (user.must_change_password) {
            sessionStorage.setItem('wb.force_change', '1');
          }
          root.style.display = 'none';
          WB.util.toast(`欢迎回来，${user.name}`, 'success');
          WB.start();
        } catch (err) {
          errBox.textContent = err.message || '登录失败';
          errBox.style.display = '';
          btn.disabled = false;
          btn.textContent = '登 录';
        }
      });

      // 顺带探一下后端是否可达，省得用户对着登录框猜
      fetch('/api/health')
        .then((r) => r.json())
        .then((h) => {
          root.querySelector('#lg-status').textContent = `服务正常 · ${h.app} ${h.version}`;
        })
        .catch(() => {
          root.querySelector('#lg-status').textContent = '⚠ 无法连接后端服务，请确认服务已启动';
        });

      root.querySelector('#lg-pass').focus();
    },

    /** 首次登录（管理员代设密码）时强制改密 */
    promptForceChange(onDone) {
      const U = WB.util;
      const m = U.modal({
        title: '请修改初始密码',
        width: 460,
        body: `
          <p class="muted" style="font-size:12.5px;margin:0 0 12px">
            管理员为你设置的初始密码需要立即更换，修改后所有登录状态会失效，需重新登录。
          </p>
          <div class="form-item"><label>原密码</label><input id="pw-old" type="password"></div>
          <div class="form-item"><label>新密码</label><input id="pw-new" type="password" placeholder="至少 8 位，含字母与数字两类"></div>
          <div class="form-item"><label>确认新密码</label><input id="pw-new2" type="password"></div>`,
        footer: `<button class="btn btn-primary" data-ok>确认修改</button>`,
        onMount(api) {
          U.qs('[data-ok]', api.el).onclick = async () => {
            const o = U.qs('#pw-old', api.el).value;
            const n = U.qs('#pw-new', api.el).value;
            const n2 = U.qs('#pw-new2', api.el).value;
            if (n !== n2) return U.toast('两次输入的新密码不一致', 'warn');
            try {
              await auth.changePassword(o, n);
              api.close();
              auth.clear();
              U.toast('密码已更新，请用新密码重新登录', 'success');
              window.location.hash = '';
              auth.showLogin();
            } catch (e) {
              U.toast(e.message, 'error');
            }
          };
        },
      });
      return m;
    },
  };

  WB.auth = auth;
  WB.can = (perm) => auth.can(perm);

  // 兼容既有视图：各处的 { operator: WB.user } 现在取登录用户真名，
  // 不再让用户手填“当前操作人”。
  Object.defineProperty(WB, 'user', {
    configurable: true,
    get: () => (auth.user ? auth.user.name : ''),
  });
})();
