/* 前端渲染核验配置（配合 ~/.workbuddy/skills/frontend-render-verify 的 render_check.js 使用）。
 *
 * 目的：证明页面**真的画出来了**，而不只是「接口 200 / 控制台没报错」。
 * 覆盖三类静默故障：视图为空、图表画布 0×0、同类控件样式漂移。
 *
 * 用法：
 *   NODE_PATH=/Users/rickey/.workbuddy/binaries/node/workspace/node_modules \
 *     node ~/.workbuddy/skills/frontend-render-verify/scripts/render_check.js tools/render-check.config.js
 *
 * 前置：目标实例已跑起来、且已用 ui_fixture 铺过数据（否则列表全空，看不出排版好坏）。
 */

module.exports = {
  baseUrl: process.env.WB_BASE_URL || 'http://127.0.0.1:8793',
  outDir: 'docs/screenshots',

  viewport: { width: 1600, height: 1000 },
  deviceScaleFactor: 2,

  login: {
    userSelector: '#lg-user',
    passSelector: '#lg-pass',
    submitSelector: '#lg-submit',
    errorSelector: '#lg-error',
    username: process.env.ADMIN_USERNAME || 'admin',
    password: process.env.ADMIN_PASSWORD || 'Adm1n@2026',
    // ui_fixture 已清掉初始管理员的强制改密标记，这里不启用改密流程
  },

  appSelector: '#app',
  readySelector: '.kpi',        // 看板首屏就绪标志
  viewRoot: '#view-inner',      // 视图挂载点
  chartSelector: '.chart',      // echarts 的挂载 div（不是 .chart-box）

  errorBannerSelector: '.banner.danger',
  bannerFailPattern: '渲染失败|加载失败|请求失败|加载异常|渲染异常',

  fullPageScreenshots: true,

  routes: [
    { hash: '#/dashboard', label: '统计看板' },
    { hash: '#/reimbursements', label: '报销单管理' },
    { hash: '#/invoices', label: '发票管理' },
    { hash: '#/inbox', label: '发票收件箱' },
    { hash: '#/expenses', label: '费用管理' },
    { hash: '#/alerts', label: '异常与预警' },
    { hash: '#/users', label: '用户管理' },
    { hash: '#/settings', label: '系统参数' },
    { hash: '#/ai_settings', label: 'AI设置' },
    { hash: '#/audit', label: '操作审计' },
  ],

  customChecks: async (page, { record, log, shot }) => {
    const wait = (ms) => page.waitForTimeout(ms);
    /* 每段开始前清掉残留弹窗。自定义断言是一段接一段跑的，前一段忘了收尾，
     * 后一段的点击就会被遮罩吃掉 —— 报出来的是「点不动」，真因却是「没关窗」。 */
    const closeModal = async () => {
      for (let i = 0; i < 3; i++) {
        const x = page.locator('.modal-x').first();
        if (!(await x.count().catch(() => 0))) break;
        await x.click({ timeout: 3000 }).catch(() => {});
        await wait(400);
      }
      if (await page.locator('.modal-mask').count().catch(() => 0)) {
        await page.keyboard.press('Escape');
        await wait(400);
      }
    };

    /* ---------- ① 同类控件样式一致性：抓「控件掉回浏览器默认样式」 ----------
     * 发票表单里 #v-no 显式写了 type，#v-code / #v-buyer 之类没写。
     * 若 CSS 用 input[type=text] 这类属性选择器，没写 type 的框就会掉样式、
     * 和旁边的框对不齐 —— 肉眼扫一眼常看不出来，但就是坏的。 */
    await page.evaluate(() => { location.hash = '#/invoices'; });
    await wait(1500);
    const newBtn = page.locator('#btn-new');
    if (await newBtn.count()) {
      await newBtn.click();
      await wait(1200);
    } else {
      record('发票管理页找不到「+ 登记发票」按钮（#btn-new），无法核验表单控件一致性');
    }

    const KEYS = ['paddingTop', 'paddingLeft', 'borderTopWidth', 'borderLeftWidth',
      'borderTopStyle', 'borderRadius', 'fontSize', 'fontFamily', 'backgroundColor',
      'lineHeight', 'height'];
    const snap = (sel) => page.evaluate(({ sel, keys }) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const cs = getComputedStyle(el);
      return Object.fromEntries(keys.map((k) => [k, cs[k]]));
    }, { sel, keys: KEYS }).catch(() => null);

    // 显式写了 type 的框 vs 没写 type 的框，理应完全一致
    const a = await snap('#v-no');       // type 未显式声明（属「没写 type」的一类）
    const b = await snap('#v-seller');
    const c = await snap('#v-buyer');
    if (!a || !b || !c) {
      record('发票表单未取到 #v-no / #v-seller / #v-buyer，控件一致性未核验');
    } else {
      const diff = (x, y) => KEYS.filter((k) => x[k] !== y[k]);
      const d1 = diff(a, b);
      const d2 = diff(b, c);
      if (d1.length) record('发票表单控件样式不一致 #v-no vs #v-seller：' + d1.map((k) => `${k}=${a[k]}/${b[k]}`).join(' '));
      else log('· 发票表单同类输入框样式一致（10 项属性逐一比对通过）');
      if (d2.length) record('发票表单控件样式不一致 #v-seller vs #v-buyer：' + d2.map((k) => `${k}=${b[k]}/${c[k]}`).join(' '));
    }
    await shot(page, 'c1-发票表单控件一致性');
    await closeModal();

    /* ---------- ② 勾选框必须还是「勾选框」 ----------
     * 基础 input 规则若没排除 checkbox，会把边框/背景/固定高度套上去，
     * 勾选框看起来就变成一个白色小方框，且被拉高与表格行错位。 */
    await page.evaluate(() => { location.hash = '#/expenses'; });
    await wait(1500);
    const ck = await page.evaluate(() => {
      const el = document.querySelector('.ck-row, #ck-all');
      if (!el) return null;
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return { type: el.type, w: Math.round(r.width), h: Math.round(r.height), accent: cs.accentColor };
    });
    if (!ck) {
      record('费用管理页没有勾选框（批量选择列缺失）');
    } else if (ck.type !== 'checkbox') {
      record(`批量选择控件不是 checkbox：type=${ck.type}（漏写 type 会退化成文本输入框）`);
    } else if (ck.h > 22 || ck.w > 22) {
      record(`勾选框被拉伸成 ${ck.w}×${ck.h}，已不是原生外观（基础 input 规则没排除 checkbox）`);
    } else {
      log(`· 勾选框外观正常（${ck.w}×${ck.h}，type=checkbox）`);
    }

    /* ---------- ③ 弹窗：点「图标/按钮」要真的能关，点内容区不许误关 ---------- */
    await closeModal();
    await page.evaluate(() => { location.hash = '#/reimbursements'; });
    await wait(1500);
    const row = page.locator('#view-inner tbody tr.clickable').first();
    if (await row.count()) {
      await row.click();
      await wait(1400);
    } else {
      record('报销单列表没有可点击的行（.clickable），无法核验弹窗交互');
    }
    const modalOpen = await page.locator('.modal-mask').count();
    if (!modalOpen) {
      record('点报销单行没有打开详情弹窗');
    } else {
      await shot(page, 'c2-报销单详情弹窗');

      // 反向断言：点弹窗内容区**不该**被关掉
      await page.locator('.modal-body').first().click({ position: { x: 30, y: 30 } }).catch(() => {});
      await wait(400);
      if (!(await page.locator('.modal-mask').count())) {
        record('点弹窗内容区把弹窗关掉了（关闭判定过宽，用户没法选中内容）');
      } else {
        log('· 点弹窗内容区未误关');
      }

      // 正向断言：点右上角 × 必须关得掉，且关闭后遮罩不残留（否则下次打不开）
      const x = page.locator('.modal-x').first();
      if (await x.count()) {
        const box = await x.boundingBox();
        const hit = await page.evaluate(([px, py]) => {
          const el = document.elementFromPoint(px, py);
          return { tag: el ? el.tagName.toLowerCase() : null, inX: !!(el && el.closest('.modal-x')) };
        }, [box.x + box.width / 2, box.y + box.height / 2]);
        await x.click();
        await wait(700);
        const left = await page.evaluate(() => {
          const w = document.querySelector('.modal-mask');
          if (!w) return 'removed';
          const cs = getComputedStyle(w);
          return `still-in-dom display=${cs.display} visibility=${cs.visibility} opacity=${cs.opacity}`;
        });
        log(`· 关闭按钮命中元素：${hit.tag}（inX=${hit.inX}）`);
        if (left !== 'removed') record(`点 × 后遮罩仍残留：${left}（会挡住后续点击）`);
        else log('· 点 × 后遮罩已移除，无残留');
      } else {
        record('弹窗没有关闭按钮 .modal-x');
      }
    }

    /* ---------- ④ AI 助手浮窗：开→发→收流式回复→关 ---------- */
    await closeModal();
    const fab = page.locator('#ai-fab');
    if (!(await fab.count())) {
      record('页面上没有 AI 助手浮窗（#ai-fab）');
    } else {
      // 浮窗的 off 态表示「没配模型」。要判断它对不对，得跟后端比，
      // 不能假定实例一定配了模型——干净库上本来就没模型，那种情况下
      // 浮窗进配置引导态是**正确**行为，把对话断言硬跑反而会误报。
      const aiState = await page.evaluate(async () => {
        const t = localStorage.getItem('wb.token') || '';
        let configured = null;
        try {
          const r = await fetch('/api/ai/status', { headers: { Authorization: 'Bearer ' + t } });
          configured = !!((await r.json()) || {}).configured;
        } catch (e) { configured = null; }
        const fab = document.getElementById('ai-fab');
        return { configured, off: !!fab && fab.classList.contains('off') };
      });
      if (aiState.configured === null) {
        record('取不到 /api/ai/status，无法判定浮窗状态是否正确');
      } else if (aiState.configured === aiState.off) {
        // configured=true 却显示未配置，或 configured=false 却显示可用 —— 前端没消费该接口
        record(`AI 浮窗状态与 /api/ai/status 不一致（configured=${aiState.configured} off=${aiState.off}）`);
      } else {
        log(`· AI 浮窗状态与后端一致（configured=${aiState.configured}）`);
      }

      await fab.click();
      await wait(900);
      const panel = await page.evaluate(() => {
        const el = document.getElementById('ai-panel');
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { w: Math.round(r.width), h: Math.round(r.height) };
      });
      if (!panel || panel.h < 50) {
        record('AI 助手面板没打开或尺寸异常（' + JSON.stringify(panel) + '）');
      } else {
        log(`· AI 助手面板已打开 ${panel.w}×${panel.h}`);
        await shot(page, 'c3-AI助手面板');

        const input = page.locator('#ai-input');
        const send = page.locator('#ai-send');
        if (!aiState.configured) {
          // 没配模型的实例：只验「退化成配置引导」，并明确记为未验证
          const guide = await page.evaluate(() => ({
            unset: !!document.querySelector('#ai-panel .ai-unset'),
            goto: !!document.getElementById('ai-go-set'),
          }));
          if (!guide.unset) record('未配置模型时浮窗没有给出「尚未配置大模型」的说明');
          else log(`· 未配置模型的引导态正常（去配置按钮：${guide.goto ? '有' : '无'}）`);
          log('· 未验证：对话/流式/关闭三条断言需要实例已配置模型（本次未配）');
          await shot(page, 'c4-AI助手未配置引导');
        } else if (!(await input.count()) || !(await send.count())) {
          record('AI 助手面板缺输入框或发送按钮（模型未就绪时会退化成配置引导页）');
        } else {
          await input.fill('这个月有哪些待审批的报销单？');
          await send.click();
          // 等流式回复落地：气泡里出现文字且打字动画消失
          const ok = await page.waitForFunction(() => {
            const bubbles = [...document.querySelectorAll('.ai-msg.a .ai-bubble')];
            const last = bubbles[bubbles.length - 1];
            if (!last) return false;
            return last.textContent.trim().length > 6 && !last.querySelector('.ai-typing');
          }, { timeout: 25000 }).then(() => true).catch(() => false);
          if (!ok) {
            const dump = await page.evaluate(() => (document.getElementById('ai-msgs') || {}).innerHTML || '');
            log('· AI 回复未落地，面板现场：\n' + String(dump).slice(0, 900));
            record('AI 助手未返回内容（流式回复没落到气泡里）');
          } else {
            const txt = await page.locator('.ai-msg.a .ai-bubble').last().textContent();
            log('· AI 回复已渲染：' + txt.trim().slice(0, 60) + '…');
          }
          await shot(page, 'c4-AI助手对话');

          // 关闭：close() 用 display:none（保留在 DOM）。判定标准是「不再可见且不再挡住点击」，
          // 不能只看元素是否被移除 —— 但也不能接受 opacity:0 这种「还在挡」的伪关闭。
          const close = page.locator('#ai-close');
          await close.click();
          await wait(700);
          const gone = await page.evaluate(() => {
            const el = document.getElementById('ai-panel');
            if (!el) return { state: 'removed' };
            const cs = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            // 面板右下角是浮窗所在位置，关闭后这一点的命中元素不该还是面板内部
            const hit = document.elementFromPoint(
              Math.round(r.right - 20), Math.round(r.bottom - 20));
            return {
              state: `display=${cs.display} visibility=${cs.visibility} opacity=${cs.opacity}`,
              w: Math.round(r.width), h: Math.round(r.height),
              blocks: !!(hit && hit.closest && hit.closest('#ai-panel')),
            };
          });
          const hidden = gone.state === 'removed'
            || (gone.w === 0 && gone.h === 0)
            || /display=none|visibility=hidden/.test(gone.state);
          if (!hidden) record(`AI 面板关闭后仍可见：${gone.state}`);
          else if (gone.blocks) record('AI 面板关闭后仍在挡住右下角点击（伪关闭）');
          else log('· AI 面板关闭后不再可见、不再挡住点击');
        }
      }
    }

    /* ---------- ⑤⑥ 两条识别链路：上传 → 识别 → 逐字段回填 ----------
     * 样本是一张字段齐全的数电票 XML，现场写到 /tmp（内容与 tools/verify_ui.mjs 一致）。 */
    const fs = require('fs');
    const SAMPLE = '/tmp/wb-render-check-invoice.xml';
    fs.writeFileSync(SAMPLE, `<?xml version="1.0" encoding="UTF-8"?>
<Invoice>
  <InvoiceNumber>24417000000077889911</InvoiceNumber>
  <IssueTime>2026-09-12</IssueTime>
  <SellerName>深圳市云图科技有限公司</SellerName>
  <SellerIdNum>91440300MA5F1234XA</SellerIdNum>
  <BuyerName>深圳市智联云创科技有限公司</BuyerName>
  <BuyerIdNum>91440300MA5G5678XB</BuyerIdNum>
  <TotalAmtWithTax>2380.00</TotalAmtWithTax>
  <TotalTax>273.81</TotalTax>
  <TaxRate>13</TaxRate>
</Invoice>`, 'utf8');

    // ⑤ 手工入账：识别结果逐字段填进可编辑表单
    await closeModal();
    await page.evaluate(() => { location.hash = '#/inbox'; });
    await wait(1400);
    const tab = page.locator('#ib-tabs .tab[data-tab="upload"]');
    if (!(await tab.count())) {
      record('收件箱缺「手工入账」页签');
    } else {
      await tab.click();
      await wait(900);
      const file = page.locator('#up-file');
      if (!(await file.count())) {
        record('手工入账页没有文件选择入口（#up-file）');
      } else {
        await file.setInputFiles(SAMPLE);
        const ok = await page.waitForSelector('#up-result .recog-grid', { timeout: 20000 })
          .then(() => true).catch(() => false);
        if (!ok) {
          const dump = await page.evaluate(() => (document.getElementById('up-result') || {}).innerHTML || '');
          log('· 手工入账识别预览未出现，现场：\n' + String(dump).slice(0, 600));
          record('手工入账：上传后没出现识别预览表单');
        } else {
          // 核心契约：识别出的字段必须回填进输入框，而不是只给个「识别成功」
          const filled = await page.evaluate(() => {
            const out = {};
            document.querySelectorAll('#up-result input[data-rk]').forEach((el) => { out[el.dataset.rk] = el.value; });
            return out;
          });
          const must = ['invoice_no', 'invoice_date', 'amount', 'seller_name', 'buyer_name'];
          const missed = must.filter((k) => !filled[k]);
          if (missed.length) record('手工入账：关键字段未回填（' + missed.join('、') + '）');
          else log('· 手工入账：号码/日期/金额/购销方均已回填且可编辑');
          await shot(page, 'c5-手工入账识别预览');
        }
      }
    }

    // ⑥ 登记发票：上传自动识别并填入表单
    await closeModal();
    await page.evaluate(() => { location.hash = '#/invoices'; });
    await wait(1400);
    if (!(await page.locator('#btn-new').count())) {
      record('发票管理页找不到 #btn-new');
    } else {
      await page.locator('#btn-new').click();
      await wait(900);
      const vfile = page.locator('#v-file');
      if (!(await vfile.count())) {
        record('登记发票弹窗没有上传识别入口（#v-file）');
      } else {
        await vfile.setInputFiles(SAMPLE);
        const ok = await page.waitForFunction(() => {
          const h = document.getElementById('v-recog-hint');
          return h && /已识别|未能识别|识别失败/.test(h.textContent);
        }, { timeout: 20000 }).then(() => true).catch(() => false);
        if (!ok) {
          const hint = await page.evaluate(() => (document.getElementById('v-recog-hint') || {}).textContent || '');
          record('登记发票：上传识别没有给出结果提示（hint=' + hint.trim().slice(0, 80) + '）');
        } else {
          const hint = await page.evaluate(() => document.getElementById('v-recog-hint').textContent.trim());
          log('· 登记发票识别：' + hint.replace(/\s+/g, ' ').slice(0, 90));
          await shot(page, 'c6-登记发票上传识别');
        }
      }
      await closeModal();
    }
  },
};
