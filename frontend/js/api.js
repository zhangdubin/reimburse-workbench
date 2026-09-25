/* 后端 API 封装。
 *
 * 统一在这里挂载 Bearer token、处理 401 会话失效，
 * 以及把二进制下载（发票影像）转成可预览的 blob URL。
 */
window.WB = window.WB || {};

(function () {
  const BASE = '';

  function qs(params) {
    if (!params) return '';
    const p = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v === null || v === undefined || v === '') return;
      p.append(k, v);
    });
    const s = p.toString();
    return s ? '?' + s : '';
  }

  function authHeaders(extra) {
    const h = Object.assign({}, extra || {});
    const t = WB.auth && WB.auth.token;
    if (t) h.Authorization = 'Bearer ' + t;
    return h;
  }

  /** 把后端错误体转成一句人话 */
  async function errorMessage(res) {
    let msg = `请求失败 (${res.status})`;
    try {
      const data = await res.json();
      if (typeof data.detail === 'string') msg = data.detail;
      else if (Array.isArray(data.detail)) msg = data.detail.map((d) => d.msg).join('；');
    } catch (_) {}
    return msg;
  }

  async function request(method, path, { params, body, skipAuthRedirect } = {}) {
    const url = BASE + path + qs(params);
    const opt = { method, headers: authHeaders() };
    if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(url, opt);
    } catch (e) {
      WB.util.toast('无法连接后端服务，请确认服务已启动', 'error');
      throw e;
    }
    if (res.status === 401 && !skipAuthRedirect) {
      // 会话过期/被踢：清干净并回到登录页，不要在每个调用点重复处理
      WB.auth.onUnauthorized();
      throw new Error('未登录或登录已过期');
    }
    if (!res.ok) {
      const msg = await errorMessage(res);
      WB.util.toast(msg, 'error');
      throw new Error(msg);
    }
    if (res.status === 204) return null;
    const ct = res.headers.get('content-type') || '';
    return ct.includes('application/json') ? res.json() : res.text();
  }

  /** 带鉴权的原始请求（上传/下载用，不能走 JSON 序列化分支） */
  async function raw(method, path, { body, headers, params } = {}) {
    const url = BASE + path + qs(params);
    const res = await fetch(url, {
      method,
      headers: authHeaders(headers),
      body,
    });
    if (res.status === 401) {
      WB.auth.onUnauthorized();
      throw new Error('未登录或登录已过期');
    }
    if (!res.ok) {
      const msg = await errorMessage(res);
      WB.util.toast(msg, 'error');
      throw new Error(msg);
    }
    return res;
  }

  const api = {
    get: (p, params) => request('GET', p, { params }),
    post: (p, body, params) => request('POST', p, { body, params }),
    put: (p, body) => request('PUT', p, { body }),
    del: (p) => request('DELETE', p),

    meta: () => api.get('/api/meta'),
    health: () => api.get('/api/health'),
    serverTime: () => api.get('/api/auth/server-time'),

    /* 账号 / 参数 / 审计（管理后台） */
    users: (params) => api.get('/api/users', params),
    createUser: (b) => api.post('/api/users', b),
    updateUser: (id, b) => api.put(`/api/users/${id}`, b),
    deleteUser: (id) => api.del(`/api/users/${id}`),
    resetUserPassword: (id, newPassword) =>
      api.post(`/api/users/${id}/reset-password`, { new_password: newPassword }),
    settings: () => api.get('/api/settings'),
    saveSetting: (key, value) => api.put(`/api/settings/${key}`, { value }),
    auditLogs: (params) => api.get('/api/audit-logs', params),

    /* 报销单 */
    reimbursements: (params) => api.get('/api/reimbursements', params),
    reimbursement: (id) => api.get(`/api/reimbursements/${id}`),
    createReimbursement: (b) => api.post('/api/reimbursements', b),
    updateReimbursement: (id, b) => api.put(`/api/reimbursements/${id}`, b),
    deleteReimbursement: (id) => api.del(`/api/reimbursements/${id}`),
    action: (id, act, body) => api.post(`/api/reimbursements/${id}/${act}`, body || {}),

    /* 发票 */
    invoices: (params) => api.get('/api/invoices', params),
    invoice: (id) => api.get(`/api/invoices/${id}`),
    invoiceSummary: () => api.get('/api/invoices/summary'),
    invoiceMonthly: (params) => api.get('/api/invoices/monthly', params),
    createInvoice: (b) => api.post('/api/invoices', b),
    updateInvoice: (id, b) => api.put(`/api/invoices/${id}`, b),
    deleteInvoice: (id) => api.del(`/api/invoices/${id}`),
    checkInvoice: (id) => api.post(`/api/invoices/${id}/check`, {}),
    batchCheck: (ids) => api.post('/api/invoices/batch-check', ids || null),
    linkInvoice: (id, params) => api.post(`/api/invoices/${id}/link`, {}, params),

    /* 发票影像 */
    attachments: (invoiceId) => api.get(`/api/invoices/${invoiceId}/attachments`),
    uploadAttachment: (invoiceId, file) => {
      const fd = new FormData();
      fd.append('file', file, file.name);
      // 交给浏览器自己带 multipart boundary，不能手写 Content-Type
      return raw('POST', `/api/invoices/${invoiceId}/attachments`, { body: fd }).then((r) => r.json());
    },
    deleteAttachment: (id) => api.del(`/api/attachments/${id}`),
    /** 下载受保护的影像并转成 blob URL（图片可直接进 <img>，PDF 可新窗口打开） */
    attachmentBlobUrl: async (id) => {
      const res = await raw('GET', `/api/attachments/${id}/raw`);
      return URL.createObjectURL(await res.blob());
    },

    /* 发票收件箱：邮箱收票与自动识别 */
    mailAccounts: () => api.get('/api/mail-accounts'),
    createMailAccount: (b) => api.post('/api/mail-accounts', b),
    updateMailAccount: (id, b) => api.put(`/api/mail-accounts/${id}`, b),
    deleteMailAccount: (id) => api.del(`/api/mail-accounts/${id}`),
    testMailAccount: (id, b) => api.post(`/api/mail-accounts/${id}/test`, b || {}),
    syncMailAccount: (id, params) => api.post(`/api/mail-accounts/${id}/sync`, {}, params),
    syncAllMail: () => api.post('/api/inbox/sync-all', {}),
    inboxMessages: (params) => api.get('/api/inbox/messages', params),
    deleteInboxMessages: (ids) => api.post('/api/inbox/messages/batch-delete', { ids }),
    purgeInboxMessages: (confirm) => api.post('/api/inbox/messages/purge', { confirm }),
    /** 只识别不落库：上传前先让人看一眼识别得准不准 */
    recognizeFile: (file, subject, body, opts) => {
      const fd = new FormData();
      fd.append('file', file, file.name);
      if (subject) fd.append('subject', subject);
      if (body) fd.append('body', body);
      // use_ai=true 时，本地规则 + OCR 都没覆盖到的关键字段会交给大模型再读一遍；
      // 没配模型时后端自动跳过这一层，不会报错
      fd.append('use_ai', opts && opts.useAi === false ? 'false' : 'true');
      return raw('POST', '/api/inbox/recognize', { body: fd }).then((r) => r.json());
    },
    /** 入账：表单字段一律提交，空字符串也算「人工确认为空」，后端据此覆盖识别结果 */
    importFile: (file, extra, opts) => {
      const fd = new FormData();
      fd.append('file', file, file.name);
      Object.entries(extra || {}).forEach(([k, v]) => {
        if (v === undefined || v === null) return;
        fd.append(k, String(v));
      });
      fd.append('use_ai', opts && opts.useAi === false ? 'false' : 'true');
      return raw('POST', '/api/inbox/import', { body: fd }).then((r) => r.json());
    },

    /* AI 智能：模型配置、助手、分析与记账草稿
     *
     * 注意：这里**没有**任何直接写业务数据的 AI 接口。助手只回动作方案，
     * 由前端在用户确认后调用既有业务接口落库（见 js/ai.js 的 EXECUTORS）。
     */
    aiStatus: () => api.get('/api/ai/status'),
    aiPresets: () => api.get('/api/ai/presets'),
    aiProviders: () => api.get('/api/ai/providers'),
    createAiProvider: (b) => api.post('/api/ai/providers', b),
    updateAiProvider: (id, b) => api.put(`/api/ai/providers/${id}`, b),
    deleteAiProvider: (id) => api.del(`/api/ai/providers/${id}`),
    testAiProvider: (id, b) => api.post(`/api/ai/providers/${id}/test`, b || {}),
    aiUsage: (days) => api.get('/api/ai/usage', { days }),
    aiAnalyze: (id) => api.post(`/api/ai/analyze/${id}`, {}),
    aiApproval: (id) => api.post(`/api/ai/approval/${id}`, {}),
    aiBookkeeping: (b) => api.post('/api/ai/bookkeeping', b),
    aiActions: () => api.get('/api/ai/actions'),

    /* 批量删除与清空（危险操作，后端还会再要一次确认词） */
    batchDelete: (res, ids) => api.post(`/api/${res}/batch-delete`, { ids }),
    batchDeleteInvoices: (ids) => api.post('/api/invoices/batch-delete', { ids }),
    batchDeleteReimbursements: (ids) => api.post('/api/reimbursements/batch-delete', { ids }),
    purgeInvoices: (confirm, params) => api.post('/api/invoices/purge-all', { confirm }, params),
    purgeReimbursements: (confirm) => api.post('/api/reimbursements/purge-all', { confirm }),
    deleteAuditLogs: (ids) => api.post('/api/audit-logs/batch-delete', { ids }),
    purgeAuditLogs: (confirm, params) => api.post('/api/audit-logs/purge', { confirm }, params),

    /* 主数据 */
    list: (res, params) => api.get(`/api/${res}`, params),
    create: (res, b) => api.post(`/api/${res}`, b),
    update: (res, id, b) => api.put(`/api/${res}/${id}`, b),
    remove: (res, id) => api.del(`/api/${res}/${id}`),

    /* 统计 */
    overview: (params) => api.get('/api/stats/overview', params),
    trend: (params) => api.get('/api/stats/trend', params),
    byCategory: (params) => api.get('/api/stats/by-category', params),
    byGroup: (params) => api.get('/api/stats/by-group', params),
    byDepartment: (params) => api.get('/api/stats/by-department', params),
    byEmployee: (params) => api.get('/api/stats/by-employee', params),
    byCustomer: (params) => api.get('/api/stats/by-customer', params),
    byProject: (params) => api.get('/api/stats/by-project', params),
    byMonthDept: (params) => api.get('/api/stats/by-month-department', params),
    categoryTrend: (params) => api.get('/api/stats/category-trend', params),
    budgetExecution: (params) => api.get('/api/stats/budget-execution', params),
    alerts: (params) => api.get('/api/stats/alerts', params),

    /* 导出：导出接口现在也要鉴权，不能再直接用 <a href> */
    exportUrl: (name) => `/api/export/${name}`,
    downloadExport: async (name, params) => {
      const res = await raw('GET', `/api/export/${name}`, { params });
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
    },
  };

  WB.api = api;
})();
