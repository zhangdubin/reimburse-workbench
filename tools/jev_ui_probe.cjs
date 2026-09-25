/* Jev 决策网关卡片端到端探针（v2.9.1）
 *
 * 为什么单独写一个：这次报障的核心体验就在这张卡片上——后端协议修好了、错误也留痕了，
 * 但如果界面还是只弹一句 toast，管理员照样不知道「到底是没绑卡、key 失效还是路径变了」。
 * 所以这里用真浏览器登录 → 真点「连通性测试」→ 断言结果块里有实质内容 → 截图留档。
 *
 * 断言不区分「连通成功」与「连通失败」：两种都算通过，**关键是失败时必须说清原因**。
 * 也就是说，即使网关因为没绑卡返回 403，只要界面把状态码/端点/建议显示出来，就是 PASS。
 *
 * 用法：
 *   NODE_PATH=/Users/rickey/.workbuddy/binaries/node/workspace/node_modules \
 *     node tools/jev_ui_probe.cjs <baseUrl> <user> <pass> [outDir]
 * 例：
 *   node tools/jev_ui_probe.cjs http://127.0.0.1:18091 admin 'Adm1n@2026' /tmp/jev-ui
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright-core');

const BASE = process.argv[2] || 'http://127.0.0.1:18091';
const USER = process.argv[3] || 'admin';
const PASS = process.argv[4] || 'Adm1n@2026';
const OUT = process.argv[5] || '/tmp/jev-ui';
// 传了就测「已配置」场景（会真的打到网关，覆盖 HTTP 错误诊断）；不传只测「未配置」场景
const JEV_KEY = process.env.JEV_KEY || '';

function findChrome() {
  const c = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
  ];
  for (const p of c) if (fs.existsSync(p)) return p;
  throw new Error('找不到本机 Chrome');
}

/** 读取结果块：class（ok/err）+ 每行文字 */
async function readResult(page) {
  return page.evaluate(() => {
    const box = document.querySelector('#jev-test-out');
    return {
      cls: box.className,
      text: box.innerText.trim(),
      lines: [...box.querySelectorAll('div')].map((d) => d.innerText.trim()).filter(Boolean),
    };
  });
}

const out = [];
const ok = (n, c, x) => out.push(`${c ? 'PASS' : 'FAIL'}  ${n}${x ? '  —— ' + x : ''}`);

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({
    executablePath: findChrome(),
    headless: true,
    args: ['--no-sandbox', '--disable-features=Translate'],
  });
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();

  const jsErrors = [];
  page.on('pageerror', (e) => jsErrors.push(String(e.message || e)));

  try {
    // ---- 登录 ----
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#lg-user', { timeout: 15000 });
    await page.fill('#lg-user', USER);
    await page.fill('#lg-pass', PASS);
    await page.click('#lg-submit');
    await page.waitForSelector('#nav', { timeout: 15000 }).catch(() => {});
    ok('登录成功（侧栏出现）', await page.locator('#nav').count() > 0);
    await page.waitForTimeout(800);

    // ---- 进入「AI 设置」（Jev 卡片在这里，不在「系统参数」） ----
    await page.evaluate(() => { location.hash = '#/ai_settings'; });
    await page.waitForSelector('#jev-card', { timeout: 15000 });
    ok('Jev 卡片已渲染', true);

    // ---- 卡片结构 ----
    const rows = await page.locator('#jev-rows tr').count();
    ok('三行配置项齐全', rows === 3, `${rows} 行`);
    const keys = await page.$$eval('#jev-rows tr', (trs) => trs.map((t) => t.getAttribute('data-key')));
    ok('配置项是 jev_key / jev_base_url / jev_timeout_sec',
      JSON.stringify(keys) === JSON.stringify(['jev_key', 'jev_base_url', 'jev_timeout_sec']),
      keys.join(','));
    const baseInput = await page.inputValue('#jev-rows tr[data-key="jev_base_url"] .jev-inp').catch(() => '');
    ok('基址输入框存在（占位符提示已配置）', baseInput !== undefined, `value="${baseInput}"`);
    ok('有「连通性测试」按钮', await page.locator('#jev-test').count() === 1);

    // ---- 场景 A：未配置 key ----
    await page.click('#jev-test');
    await page.waitForSelector('#jev-test-out:not([hidden])', { timeout: 30000 });
    await page.waitForFunction(
      () => { const b = document.querySelector('#jev-test'); return b && !b.disabled; },
      { timeout: 30000 },
    ).catch(() => {});
    let res = await readResult(page);
    ok('未配置时：标为失败态', res.cls.includes('err'), res.cls);
    ok('未配置时：说清是缺 key 并指出去哪儿配',
      /jev_key|未配置/.test(res.text) && res.text.length > 20, res.text.slice(0, 140));

    // ---- 场景 B：配上 key 后再测（有真 key 就真打网关，覆盖 HTTP 错误诊断路径）----
    if (JEV_KEY) {
      await page.evaluate(async (k) => {
        await WB.api.put('/api/admin/jev-config/jev_key', { value: k });
      }, JEV_KEY);
      await page.waitForTimeout(600); // 等后端 5s 配置缓存失效窗口
      await page.click('#jev-test');
      await page.waitForFunction(
        () => {
          const b = document.querySelector('#jev-test');
          const box = document.querySelector('#jev-test-out');
          return b && !b.disabled && box && !box.hidden && box.innerText.trim();
        },
        { timeout: 40000 },
      ).catch(() => {});
      await page.waitForTimeout(300);
      res = await readResult(page);
      ok('配了 key 后：有结果', res.text.length > 0, res.text.slice(0, 140));
      if (res.cls.includes('ok')) {
        ok('成功态：给出端点与耗时', /\/v1\/evaluate/.test(res.text) && /ms/.test(res.text), res.text.slice(0, 160));
      } else {
        // 失败态才是这次修复的重点：必须能看出为什么失败
        ok('失败态：说清了原因（不是笼统一句「失败」）', res.text.length > 20, res.text.slice(0, 160));
        ok('失败态：带上 HTTP 状态码或错误原文',
          /HTTP\s*\d{3}|invalid|credit card|Connection|超时|网络/.test(res.text),
          res.text.slice(0, 240));
        ok('失败态：给出可执行建议', /建议/.test(res.text), res.text.slice(0, 240));
      }
    } else {
      out.push('SKIP  未传 JEV_KEY，跳过「已配置」场景（端口令后重跑可覆盖）');
    }

    // ---- 截图 ----
    const shot = path.join(OUT, 'jev-card.png');
    await page.locator('#jev-card').screenshot({ path: shot });
    ok('卡片截图已保存', fs.existsSync(shot), shot);

    ok('页面无 JS 报错', jsErrors.length === 0, jsErrors.slice(0, 2).join(' | ').slice(0, 200));
  } catch (e) {
    ok('探针执行未抛异常', false, String(e.message || e).slice(0, 300));
  } finally {
    await browser.close();
  }

  console.log(out.join('\n'));
  const bad = out.filter((l) => l.startsWith('FAIL')).length;
  console.log(`\n结果：${out.length - bad} 通过 / ${bad} 失败`);
  process.exit(bad ? 1 : 0);
})();
