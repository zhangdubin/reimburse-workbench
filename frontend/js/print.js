/* =====================================================================
 * 打印模块（v2.7.13）—— 正式财务凭证版式 + Data Matrix
 *   后端 /api/print/dm（pylibdmtx，ISO/IEC 16022 标准实现）
 *   v2.7.13：左下角只留 DM 码图，去掉「扫码核验」标签 / 编号姓名 / 系统名（含配套 CSS 清理）。
 *   v2.7.12：DM payload 精简为单号（≤26×26 单数据区，不再出现 40×40 四区拼块）；
 *            打印尺寸 22mm → 16mm。
 *   v2.7.11：DM 图片改用绝对 URL —— Blob 打印窗口无法解析相对路径，
 *            此前 <img src="/api/print/dm"> 从不发请求，故打印预览里始终看不到码图。
 *   v2.7.10：auto-print 等所有 <img> 加载完（避免 DM 异步图没出来就弹打印）
 * 提供三类 A4 打印：
 *   WB.print.reimbursement(d)   报销单（含明细/关联发票/流转记录/签字栏）
 *   WB.print.invoices(rows)     发票清单
 *   WB.print.invoice(v)         单张发票
 *
 * 设计原则：
 *  - 独立窗口 + 内联样式，与 app.css 完全隔离；
 *  - Blob URL 开窗，避开 document.write 竞态；
 *  - 弹窗被拦时降级隐藏 iframe，保证一定出纸；
 *  - 金额右对齐 + 人民币大写，财务凭证标准格式。
 * ===================================================================== */
(function () {
  'use strict';

    /* ---------- Code128-B 条形码（纯 JS，零依赖） ---------- */
  // 索引 0-95 对应 ASCII 32(space) 到 127(DEL)
  // Code128-B 编码表（索引 0-106，共 107 项）
  // 索引对应：0=空格 … 95=下划线，104=START-B，105=未使用，106=STOP
  const C128B = [
    "11011001100","11001101100","11001100110","10010011000","10010001100",
    "10001001100","10011001000","10011000100","10001100100","11001001000",
    "11001000100","11000100100","10110011100","10011011100","10011001110",
    "10111001100","10011101100","10011100110","11001110010","11001011100",
    "11001001110","11011100100","11001110100","11101101110","11101001100",
    "11100101100","11100100110","11101100100","11100110100","11100110010",
    "11011011000","11011000110","11000110110","10111011000","10111000110",
    "10011101110","11101001100","11100101100","10011100110","11101100100",
    "11010001100","11000101100","11011101000","11011100100","11011101100",
    "11101011000","11101000110","11100010110","11101101000","11100111010",
    "11100101110","11101110010","11101010000","11101001000","11100011010",
    "11101111010","11001000010","11110001010","10100010010","10100001000",
    "10010110000","10010000110","10000110000","10000101100","11000010010",
    "11001010000","11000001000","10110111000","10110001110","10001101100",
    "10011011100","10011001110","10110101100","10001011100","10011010100",
    "10011000110","10000111100","10001111000","10001110100","11100011100",
    "11001011000","11001000110","11110011010","11000011110","10111011000",
    "10111000110","10001011110","10100011100","10001011000","10000111100",
    "11000011100","10001110000","11100111000","11010011100","11001110100",
    "10111110000","10011110010","10011101000","10000100110","10001011000",
    "10001000110","10010111000","10001110010","10001001000",
  ];
  // 注意：索引 104=START-B(11010000100)，索引 106=STOP(1100011101011) 由下面常量提供
  const C128_START_B = 104;
  const C128_STOP    = 106;

  /* ===================================================================
   * QR Code 生成器（纯 JS，零依赖）
   *   - 字节模式（UTF-8，支持中文）+ ECC 等级 M
   *   - 自动选版本 1-10（最大 182 字节数据）
   *   - 输出 SVG 字符串
   * 参考：ISO/IEC 18004；Nayuki 算法紧凑版
   * =================================================================== */
  function qrCode(text, pxSize) {
    pxSize = pxSize || 4;
    // 1) UTF-8 编码
    const bytes = [];
    for (let i = 0; i < text.length; i++) {
      let c = text.charCodeAt(i);
      if (c < 0x80) bytes.push(c);
      else if (c < 0x800) { bytes.push(0xc0 | c >> 6); bytes.push(0x80 | c & 0x3f); }
      else if (c < 0xd800 || c >= 0xe000) { bytes.push(0xe0 | c >> 12); bytes.push(0x80 | (c >> 6) & 0x3f); bytes.push(0x80 | c & 0x3f); }
      else { i++; const cu = 0x10000 + (((c & 0x3ff) << 10) | (text.charCodeAt(i) & 0x3ff)); bytes.push(0xf0 | cu >> 18); bytes.push(0x80 | (cu >> 12) & 0x3f); bytes.push(0x80 | (cu >> 6) & 0x3f); bytes.push(0x80 | cu & 0x3f); }
    }
    // 2) 选版本（ECC M，V1-10）
    const EC_M = [[16,10],[28,16],[44,26],[64,18],[86,24],[108,16],[124,18],[154,22],[182,22],[216,26]];
    let v = 1;
    for (; v <= 10; v++) { const need = Math.ceil((4 + (v <= 9 ? 8 : 16) + bytes.length * 8) / 8); if (need <= EC_M[v - 1][0]) break; }
    if (v > 10) v = 10;
    const dcw = EC_M[v - 1][0], ecLen = EC_M[v - 1][1];
    const sz = 17 + 4 * v;
    // 3) Reed-Solomon GF(256)
    const gfE = new Array(512), gfL = new Array(256);
    (() => { let x = 1; for (let i = 0; i < 255; i++) { gfE[i] = x; gfL[x] = i; x <<= 1; if (x & 0x100) x ^= 0x11d; } for (let i = 255; i < 512; i++) gfE[i] = gfE[i - 255]; })();
    const gM = (a, b) => (a && b) ? gfE[gfL[a] + gfL[b]] : 0;
    function rsGen(d) { let p = [1]; for (let i = 0; i < d; i++) { const n = p.concat([0]); for (let j = 0; j < p.length; j++) n[j + 1] ^= gM(p[j], gfE[i]); p = n; } return p; }
    function rsEnc(data, d) {
      const g = rsGen(d), r = data.concat(new Array(d).fill(0));
      for (let i = 0; i < data.length; i++) { const f = r[i]; if (f) for (let j = 0; j < g.length; j++) r[i + j] ^= gM(g[j], f); }
      return r.slice(data.length);
    }
    // 4) 数据位 + 填充
    const bits = [0,1,0,0];
    const len = bytes.length;
    if (v <= 9) for (let i = 7; i >= 0; i--) bits.push((len >> i) & 1);
    else for (let i = 15; i >= 0; i--) bits.push((len >> i) & 1);
    for (const b of bytes) for (let i = 7; i >= 0; i--) bits.push((b >> i) & 1);
    for (let i = 0; i < 4 && bits.length < dcw * 8; i++) bits.push(0);
    while (bits.length % 8 !== 0) bits.push(0);
    for (let p = 0; bits.length < dcw * 8; p = 0xec ^ 0x11 ^ p) for (let i = 7; i >= 0; i--) bits.push((p >> i) & 1);
    // 5) codewords + EC
    const dataCw = [];
    for (let i = 0; i < dcw; i++) { let cw = 0; for (let j = 0; j < 8; j++) cw = (cw << 1) | bits[i * 8 + j]; dataCw.push(cw); }
    const ecCw = rsEnc(dataCw, ecLen);
    const allCw = dataCw.concat(ecCw);

    // 6) 矩阵 + isFunction 标记
    const m = []; const isFn = [];
    for (let i = 0; i < sz; i++) { m.push(new Array(sz).fill(-1)); isFn.push(new Array(sz).fill(false)); }
    const setM = (x, y, v, fn) => { m[y][x] = v; if (fn) isFn[y][x] = true; };

    // Finder（7x7 黑框白环黑心 + 8x8 隔离带全白）
    function finder(x, y) {
      for (let dy = 0; dy < 7; dy++) for (let dx = 0; dx < 7; dx++) {
        const on = (dx === 0 || dx === 6 || dy === 0 || dy === 6 || (dx >= 2 && dx <= 4 && dy >= 2 && dy <= 4));
        setM(x + dx, y + dy, on ? 1 : 0, true);
      }
      // 隔离带（8x8 区域中除 finder 外的全白）
      for (let i = -1; i <= 7; i++) {
        if (y - 1 >= 0 && x + i >= 0 && x + i < sz && m[y - 1][x + i] === -1) setM(x + i, y - 1, 0, true);
        if (y + 7 < sz && x + i >= 0 && x + i < sz && m[y + 7][x + i] === -1) setM(x + i, y + 7, 0, true);
      }
      for (let i = 0; i < 7; i++) {
        if (x - 1 >= 0 && y + i < sz && m[y + i][x - 1] === -1) setM(x - 1, y + i, 0, true);
        if (x + 7 < sz && y + i < sz && m[y + i][x + 7] === -1) setM(x + 7, y + i, 0, true);
      }
    }
    finder(0, 0); finder(sz - 7, 0); finder(0, sz - 7);

    // Timing patterns
    for (let i = 8; i < sz - 8; i++) {
      setM(i, 6, i % 2 === 0 ? 1 : 0, true);
      setM(6, i, i % 2 === 0 ? 1 : 0, true);
    }

    // Alignment patterns（V2+）
    const aP = {1:[],2:[6,18],3:[6,22],4:[6,26],5:[6,30],6:[6,34],7:[6,22,38],8:[6,24,42],9:[6,26,46],10:[6,28,50]}[v];
    if (aP.length > 0 && v <= 6) {
      // V2-V6：只画 1 个（不在 finder 区域的对角位置）
      const cx = aP[aP.length - 1], cy = aP[aP.length - 1];
      for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) {
        const on = (Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
        setM(cx + dx, cy + dy, on ? 1 : 0, true);
      }
    } else if (aP.length >= 3) {
      // V7+：画 n*n - 3*n + 2 个（跳过 3 个 finder 区域）
      for (let i = 0; i < aP.length; i++) for (let j = 0; j < aP.length; j++) {
        const cx = aP[i], cy = aP[j];
        if ((cx <= 8 && cy <= 8) || (cx >= sz - 9 && cy <= 8) || (cx <= 8 && cy >= sz - 9)) continue;
        for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) {
          const on = (Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
          setM(cx + dx, cy + dy, on ? 1 : 0, true);
        }
      }
    }

    // Dark module
    setM(8, sz - 8, 1, true);

    // Reserve format info 区域
    for (let i = 0; i < 9; i++) {
      if (m[8][i] === -1) setM(i, 8, 0, true);
      if (m[i][8] === -1) setM(8, i, 0, true);
    }
    for (let i = 0; i < 8; i++) {
      if (m[sz - 1 - i][8] === -1) setM(8, sz - 1 - i, 0, true);
      if (m[8][sz - 1 - i] === -1) setM(sz - 1 - i, 8, 0, true);
    }

    // 7) 数据 zigzag（只填未占用的格子）
    let bi = 0, dir = -1, x = sz - 1, y = sz - 1;
    while (x > 6) {
      if (x === 7) x--;
      for (let dy = 0; dy < 2; dy++) for (let dx = 0; dx < 2; dx++) {
        const cx = x - dx, cy = y + dy * dir;
        if (m[cy] && m[cy][cx] === -1) {
          const cw = allCw[bi >> 3] || 0;
          m[cy][cx] = (cw >> (7 - (bi & 7))) & 1;
          bi++;
        }
      }
      y += dir;
      if (y < 0 || y >= sz) { dir = -dir; y += dir; x -= 2; }
    }

    // 8) Mask 0（(x+y)%2==0）—— 只对**数据位**反转，**不能碰**功能图形
    for (let yy = 0; yy < sz; yy++) for (let xx = 0; xx < sz; xx++) {
      if (isFn[yy][xx]) continue;          // 跳过所有功能区
      if ((xx + yy) % 2 === 0) m[yy][xx] ^= 1;
    }

    // 9) Format info（ECC M = 00, mask 0 = 000）：BCH(15,5) + mask = 101010000010010
    const fmt = [1,0,1,0,1,0,0,0,0,0,1,0,0,1,0];
    // 副本 1：左上 finder 旁 15 位（8,0..5,7,8,5..0,8），跳过 timing 列 (6,8)
    const p1 = [[8,0],[8,1],[8,2],[8,3],[8,4],[8,5],[8,7],[8,8],[7,8],[5,8],[4,8],[3,8],[2,8],[1,8],[0,8]];
    for (let i = 0; i < 15; i++) m[p1[i][1]][p1[i][0]] = fmt[i];
    // 副本 2：右上 (8, sz-1..sz-8) + 左下 (sz-1..sz-7, 8)，跳过 dark module
    for (let i = 0; i < 8; i++) m[8][sz - 1 - i] = fmt[i];
    for (let i = 0; i < 7; i++) m[sz - 1 - i][8] = fmt[8 + i];

    // 10) SVG
    const total = sz * pxSize;
    let r = '';
    for (let yy = 0; yy < sz; yy++) for (let xx = 0; xx < sz; xx++) {
      if (m[yy][xx] === 1) r += `<rect x="${xx * pxSize}" y="${yy * pxSize}" width="${pxSize}" height="${pxSize}"/>`;
    }
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${total}" height="${total}" shape-rendering="crispEdges"><rect width="${total}" height="${total}" fill="#fff"/>${r}</svg>`;
  }

  function encode128B(str) {
    // 只编码 ASCII 可打印字符（32-126），超出范围自动跳过
    let bits = C128B[C128_START_B];
    let sum  = C128_START_B;
    let pos  = 1; // 字符在条形码中的位置（用于校验和）
    for (let i = 0; i < str.length; i++) {
      const v = str.charCodeAt(i) - 32;
      if (v >= 0 && v < C128B.length) { bits += C128B[v]; sum += pos * v; pos++; }
    }
    bits += C128B[sum % 103];
    bits += C128B[C128_STOP];
    return bits;
  }

  /**
   * 生成 SVG 条形码（Code128-B）
   * @param {string} code  - 编码内容
   * @param {number} height - 条高度 px
   * @param {number} thin   - 最细条宽度 px
   */
  function barcodeSVG(code, height, thin) {
    if (!code) return '';
    const wide = thin * 2;
    let bars = '';
    let x = 0;
    for (const c of encode128B(code)) {
      bars += c === '1'
        ? `<rect x="${x}" y="0" width="${thin}" height="${height}" fill="black"/>`
        : '';
      x += thin;
    }
    return `<svg xmlns="http://www.w3.org/2000/svg" height="${height}" width="${x}" shape-rendering="crispEdges">
${bars.replace(/^/gm, '  ')}
</svg>`;
  }

  /**
   * 生成 <canvas> 条形码（兼容更老的打印引擎）
   */
  function barcodeCanvas(code, height, thin) {
    if (!code) return '';
    const bits = encode128B(code);
    const w = bits.length * thin;
    const cv = `<canvas width="${w}" height="${height}"></canvas>
<script>var c=document.currentScript.previousElementSibling.getContext('2d');c.fillStyle='white';c.fillRect(0,0,${w},${height});c.fillStyle='black';for(var i=0;i<${bits.length};i++)if('${bits}'[i]==='1')c.fillRect(i*${thin},0,${thin},${height});<\/script>`;
    return cv;
  }

  /**
   * 生成 <img> data-URI 条形码（SVG 转 data-URI，最通用）
   */
  function barcodeImg(code, height, thin) {
    if (!code) return '';
    const bits = encode128B(code);
    const w = bits.length * thin;
    let svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${height}" shape-rendering="crispEdges">`;
    for (let i = 0; i < bits.length; i++) {
      if (bits[i] === '1') svg += `<rect x="${i*thin}" y="0" width="${thin}" height="${height}"/>`;
    }
    svg += '</svg>';
    return 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(svg)));
  }

  /**
   * 统一入口：返回直接内嵌 SVG 条形码（最可靠，无需 data-URI）
   * @param {string} code    - 条形码内容
   * @param {string} display - 下方显示文字，缺省等于 code
   * @param {number} height - 条高度，默认 52
   * @param {number} thin   - 最细条宽度，默认 2
   */
  function barcode(code, display, height, thin) {
    if (!code) return '';
    const h = height || 52, t = thin || 2;
    const label = display != null ? display : code;
    const bits = encode128B(code);
    const W = bits.length * t;
    let rects = '';
    for (let i = 0; i < bits.length; i++) {
      if (bits[i] === '1') rects += `<rect x="${i*t}" y="0" width="${t}" height="${h}"/>`;
    }
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${h}" shape-rendering="crispEdges">${rects}</svg>`;
    return `<div style="text-align:center;margin:6px 0 0">
  ${svg}
  <div style="font-size:9.5px;color:#555;letter-spacing:1.8px;margin-top:4px;font-family:monospace;text-align:center">${esc(label)}</div>
</div>`;
  }

  /**
   * 小条形码 + 标签，挂在页面左下角
   * @param {string} code    - 条形码内容
   * @param {string} display - 下方显示文字（可省略）
   */
  /**
   * v2.7.11 关键修复：
   *   打印窗口是 Blob URL 文档（blob:http://host/uuid）。该文档的 base URL 无法解析
   *   相对路径 `/api/print/dm`，浏览器压根不会发起请求 —— <img> 恒为 0×0 空白。
   *   这正是「打印预览里看不到 Data Matrix」的根因（后端接口一直是好的）。
   *   必须用绝对 URL；iframe 降级路径同样兼容。
   */
  const API_ORIGIN = (function () {
    try {
      if (typeof location !== 'undefined' && /^https?:$/.test(location.protocol)) return location.origin;
    } catch (_) {}
    return '';
  })();
  function dmUrl(payload) {
    const q = '/api/print/dm?text=' + encodeURIComponent(payload) + '&px=8&shape=square';
    return API_ORIGIN ? API_ORIGIN + q : q;
  }

  /**
   * 左下角 Data Matrix 核验码
   * v2.7.13：只留码图本身 —— 去掉「扫码核验/SCAN TO VERIFY」标签、
   *          编号·姓名文字、底部系统名，版面更干净。
   * @param {string} payload - 编码内容（报销单号 / 发票号 / 清单批次号）
   */
  function barcodeCorner(payload) {
    if (!payload) return '';
    return `<div class="bc-corner"><div class="bc-qr"><img class="bc-qr-img" src="${dmUrl(payload)}" alt="Data Matrix"/></div></div>`;
  }

  /* ---------- 人民币大写 ---------- */
  const RMB_DIG = '零壹贰叁肆伍陆柒捌玖';
  function rmbUpper(num) {
    let n = Math.round(Number(num) * 100) / 100;
    if (!isFinite(n) || n === 0) return '零元整';
    let neg = '';
    if (n < 0) { neg = '负'; n = -n; }
    const yuan = Math.floor(n);
    const jiao = Math.floor(Math.round(n * 100) % 100 / 10);
    const fen  = Math.round(n * 100) % 10;
    const UNITS = ['', '拾', '佰', '仟'];
    const GROUPS = ['', '万', '亿', '兆'];

    function section4(x) {
      let s = '', zero = false;
      for (let i = 3; i >= 0; i--) {
        const d = Math.floor(x / Math.pow(10, i)) % 10;
        if (d === 0) { if (s && !zero) { s += '零'; zero = true; } }
        else { s += RMB_DIG[d] + UNITS[i]; zero = false; }
      }
      return s.replace(/零+$/, '');
    }

    let out = '';
    if (yuan > 0) {
      let parts = [], gi = 0, y = yuan;
      while (y > 0) { parts.push({ g: y % 10000, i: gi }); y = Math.floor(y / 10000); gi++; }
      let needZero = false;
      for (let k = parts.length - 1; k >= 0; k--) {
        const p = parts[k];
        if (p.g === 0) { needZero = parts.length > 1; continue; }
        if (needZero || (k < parts.length - 1 && p.g < 1000)) out += '零';
        out += section4(p.g) + GROUPS[p.i];
        needZero = false;
      }
      out += '元';
    }
    if (jiao === 0 && fen === 0) { if (!out) out = '零元'; out += '整'; }
    else {
      if (yuan > 0 && jiao === 0 && fen > 0) out += '零';
      if (jiao > 0) out += RMB_DIG[jiao] + '角';
      if (fen > 0) out += RMB_DIG[fen] + '分';
    }
    return neg + out;
  }

  /* ---------- 工具函数 ---------- */
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function money(n) {
    return (Number(n) || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function date(s)  { return s ? String(s).slice(0, 10) : ''; }
  function dt(s)   { return s ? String(s).replace('T', ' ').slice(0, 16) : ''; }
  function now() {
    const d = new Date(), p = x => String(x).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`;
  }

  /* ---------- 打印样式 ---------- */
  // 配色：#1a3c6e 深蓝（标题栏/表头）、#c00 红色强调、#f5f5f5 斑马条纹
  const STYLE = `
    @page { size: A4 portrait; margin: 0; }   /* 0 边距，让 body 撑满整张 A4，bc-corner 才能贴纸角 */
    *, *::before, *::after { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; font-size: 13px; line-height: 1.65; color: #1a1a1a;
      font-family: "PingFang SC","Microsoft YaHei","Helvetica Neue",sans-serif; }

    /* ---- 页面容器 ---- */
    .page { width: 190mm; margin: 0 auto; padding: 12mm 14mm 10mm; position: relative; }

    /* ---- 公司抬头 ---- */
    .co-head {
      display: flex; align-items: center; gap: 10px;
      border-bottom: 2.5px solid #1a3c6e; padding-bottom: 7px; margin-bottom: 10px;
    }
    .co-logo {
      width: 34px; height: 34px; background: #1a3c6e; border-radius: 6px;
      display: flex; align-items: center; justify-content: center;
      color: #fff; font-size: 18px; font-weight: 700; flex-shrink: 0;
      font-family: "SimHei","Microsoft YaHei",sans-serif; letter-spacing: -1px;
    }
    .co-name { font-size: 14px; font-weight: 700; color: #1a3c6e; letter-spacing: 2px; }
    .co-tag  { font-size: 10px; color: #888; margin-top: 2px; }

    /* ---- 单据标题栏 ---- */
    .doc-title {
      background: #1a3c6e; color: #fff; text-align: center;
      padding: 7px 0 6px; margin: 0 0 10px; letter-spacing: 5px;
      font-size: 17px; font-weight: 700; border-radius: 3px 3px 0 0;
    }

    /* ---- 单据信息条 ---- */
    .doc-meta {
      display: flex; gap: 0; border: 1.5px solid #1a3c6e; border-radius: 0 0 4px 4px;
      margin-bottom: 10px; overflow: hidden; font-size: 12px;
    }
    .doc-meta .item { flex: 1; padding: 5px 9px; border-right: 1px solid #c8d8ee; }
    .doc-meta .item:last-child { border-right: none; }
    .doc-meta .lbl { color: #5a7aa8; font-size: 10.5px; margin-bottom: 1px; }
    .doc-meta .val { font-weight: 600; font-size: 12.5px; }
    .doc-meta .val.draft   { color: #b07000; }
    .doc-meta .val.pending { color: #1a3c6e; }
    .doc-meta .val.ok      { color: #1a7a1a; }
    .doc-meta .val.paid    { color: #555; }
    .doc-meta .val.rejected { color: #c00; }

    /* ---- 信息区（使用 .tbl class） ---- */
    .info-block {
      border: 1.5px solid #1a3c6e; border-radius: 4px; margin-bottom: 8px; overflow: hidden;
    }
    .info-block table.tbl { width: 100%; border-collapse: collapse; }
    .info-block table.tbl th { background: #1a3c6e; color: #fff; padding: 5px 9px; font-size: 11.5px; font-weight: 600; letter-spacing: 1px; text-align: left; white-space: nowrap; }
    .info-block table.tbl td { padding: 5px 9px; border-bottom: 1px solid #e0e8f4; font-size: 12.5px; vertical-align: middle; }
    .info-block table.tbl tr:last-child td { border-bottom: none; }
    /* 信息区不需斑马条纹，保持干净 */
    .info-block .lbl { color: #dde6f5; font-size: 11px; }
    .info-block .val { font-weight: 600; }
    .info-block .span { color: #dde6f5; font-size: 11px; padding: 1px 5px; background: #2a5c9e; border-radius: 10px; margin-left: 5px; font-weight: 400; white-space: nowrap; }

    /* ---- 明细表 ---- */
    .tbl-wrap { border: 1.5px solid #1a3c6e; border-radius: 4px; overflow: hidden; margin-bottom: 8px; }
    .tbl-head {
      background: #1a3c6e; color: #fff; padding: 5px 9px;
      font-size: 11.5px; font-weight: 600; letter-spacing: 1px;
    }
    table.tbl { width: 100%; border-collapse: collapse; font-size: 12.5px; }
    table.tbl th { background: #eef2f9; color: #1a3c6e; padding: 5px 7px; font-size: 11.5px; font-weight: 600; text-align: center; border-bottom: 1.5px solid #c8d8ee; white-space: nowrap; }
    table.tbl td { padding: 4.5px 7px; border-bottom: 1px solid #e8eef8; vertical-align: middle; }
    table.tbl tr:last-child td { border-bottom: none; }
    /* 斑马条纹只在 tbody，thead 不受影响 */
    table.tbl tbody tr:nth-child(even) td { background: #f4f7fd; }
    table.tbl td.num { text-align: right; font-variant-numeric: tabular-nums; }
    table.tbl td.ctr { text-align: center; }
    table.tbl td.mid { text-align: center; }
    table.tbl tfoot td { background: #1a3c6e; color: #fff; font-weight: 700; padding: 6px 9px; font-size: 13px; }
    table.tbl tfoot td.num { text-align: right; }
    table.tbl tfoot .cap { font-size: 12px; font-weight: 400; color: #dde6f5; letter-spacing: .5px; }

    /* ---- 合计突出 ---- */
    .total-bar {
      display: flex; align-items: baseline; gap: 10px;
      background: #fff3f3; border: 1.5px solid #c00;
      border-radius: 4px; padding: 7px 12px; margin-bottom: 8px;
    }
    .total-bar .cap { font-size: 12px; color: #888; white-space: nowrap; }
    .total-bar .amt { font-size: 22px; font-weight: 900; color: #c00; letter-spacing: 1px; }
    .total-bar .amt small { font-size: 11px; font-weight: 400; color: #888; margin-left: 4px; letter-spacing: 0; }

    /* ---- 签字栏 ---- */
    .sign-wrap { border: 1.5px solid #1a3c6e; border-radius: 4px; overflow: hidden; margin-top: 14px; }
    .sign-head {
      background: #1a3c6e; color: #fff; padding: 5px 9px;
      font-size: 11.5px; font-weight: 600; letter-spacing: 1px;
    }
    .sign-grid { display: grid; grid-template-columns: repeat(4, 1fr); }
    .sign-box { padding: 8px 10px; border-right: 1px solid #c8d8ee; }
    .sign-box:last-child { border-right: none; }
    .sign-box .role { font-size: 11px; color: #5a7aa8; margin-bottom: 3px; font-weight: 600; }
    .sign-box .name { font-size: 13px; font-weight: 700; color: #1a1a1a; min-height: 18px; }
    .sign-box .line {
      border-top: 1px dashed #aaa; margin-top: 20px;
      padding-top: 4px; font-size: 11px; color: #888;
      display: flex; justify-content: space-between; align-items: center; gap: 4px;
    }
    .sign-box .line::before { content: '日期'; }
    .sign-box .line .ymd {
      display: flex; gap: 2px; align-items: center; font-size: 11px; color: #666;
      font-family: monospace; letter-spacing: 0;
    }
    .sign-box .line .ymd span { display: inline-block; border-bottom: 1px solid #999; min-width: 22px; text-align: center; }

    /* ---- 左下角 Data Matrix 核验码（v2.7.13：只留码图，无文字）----
     *   @page margin: 0，body 撑满整张 A4
     *   left:0/bottom:0 贴纸面绝对左下角；保留 padding 让码不贴纸边（防裁切/扫不到） */
    .bc-corner {
      position: fixed; left: 0; bottom: 0;
      padding: 10mm 0 10mm 10mm;
      display: none;        /* 屏幕预览时隐藏，打印时才显示 */
      z-index: 999;
    }
    .bc-corner .bc-qr { display: block; line-height: 0; }
    .bc-corner .bc-qr-img { display: block; width: 16mm; height: 16mm; }

    /* ---- 打印工具条 ---- */
    .toolbar { text-align: center; padding: 6px; border-bottom: 1px solid #e0e0e0; background: #f8f8f8; }
    .toolbar button { font-size: 13px; padding: 5px 20px; cursor: pointer; border: 1px solid #aaa; border-radius: 4px; background: #fff; }
    .toolbar button:hover { background: #f0f0f0; }

    @media print {
      body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
      .no-print { display: none !important; }
      .co-head { display: none !important; }   /* 打印时不显示公司抬头 */
      /* .page 在打印时保持原本的 padding（12/14/10mm）作为内容视觉边距
         不覆盖成 0，让正文不要贴到纸边 */
      .bc-corner { display: block; }   /* 打印时显示在纸面最左下 */
    }
  `;

  /* ---------- 开窗送纸 ---------- */
  function deliver(html) {
    try {
      const url = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
      const w = window.open(url, '_blank');
      if (w && !w.closed) {
        setTimeout(() => URL.revokeObjectURL(url), 60000);
        return;
      }
    } catch (_) { /* 降级 */ }
    // 降级隐藏 iframe
    let f = document.getElementById('_wb_pf');
    if (f) f.remove();
    f = document.createElement('iframe');
    f.id = '_wb_pf';
    Object.assign(f.style, { position: 'fixed', right: '0', bottom: '0', width: '0', height: '0', border: '0', visibility: 'hidden' });
    document.body.appendChild(f);
    const doc = f.contentWindow.document;
    doc.open(); doc.write(html); doc.close();
    // 等 iframe 内容完全加载（含其 own onload 处理的 DM 图像等待）。
    // 注意：不能再用 setTimeout 硬触发，DM 异步图还没出来就打印会留空白。
    const onLoaded = () => {
      try { f.contentWindow.focus(); f.contentWindow.print(); } catch (_) {}
    };
    // iframe 自身 load 事件 = 内容 DOM + 嵌入脚本已就绪（但图仍可能加载中）
    // 再等图加载：iframe 文档的图片加载完后才真正打印。
    if (f.contentDocument && f.contentDocument.readyState === 'complete') {
      onLoaded();
    } else {
      f.addEventListener('load', () => {
        // 等 DM <img> 加载（最慢的可视资源），最多 3s 兜底
        const iw = f.contentWindow;
        const imgs = iw.document ? Array.from(iw.document.images) : [];
        const pending = imgs.filter(i => !i.complete);
        if (pending.length === 0) { onLoaded(); return; }
        const tid = setTimeout(onLoaded, 3000);
        pending.forEach(i => {
          i.addEventListener('load', () => { if (pending.every(p => p.complete)) { clearTimeout(tid); onLoaded(); } }, { once: true });
          i.addEventListener('error', () => { if (pending.every(p => p.complete)) { clearTimeout(tid); onLoaded(); } }, { once: true });
        });
      }, { once: true });
    }
  }

  function shell(title, body, corner) {
    return `<!DOCTYPE html><html lang="zh-CN"><head>
<meta charset="utf-8"><title>${esc(title)}</title>
<style>${STYLE}</style>
</head><body>
<div class="toolbar no-print">
  <button onclick="window.print()">打 印</button>
</div>
${body}
${corner || ''}
<script>
window.onload = () => {
  // 等所有 <img>（Data Matrix）加载完再触发自动打印，
  // 避免 <img src="/api/print/dm"> 异步加载慢于 400ms 触发窗口时图还没出来。
  const imgs = Array.from(document.images);
  const pending = imgs.filter(i => !i.complete);
  const trigger = () => setTimeout(() => { try { window.print(); } catch(e){} }, 200);
  if (pending.length === 0) { trigger(); return; }
  // 每个未完成图最多等 3s，3s 后强制打印（不让用户久等）
  const timeoutId = setTimeout(trigger, 3000);
  pending.forEach(i => { i.addEventListener('load', () => {
      if (pending.every(p => p.complete)) { clearTimeout(timeoutId); trigger(); }
    }, { once: true });
    i.addEventListener('error', () => {
      if (pending.every(p => p.complete)) { clearTimeout(timeoutId); trigger(); }
    }, { once: true });
  });
};
<\/script>
</body></html>`;
  }

  /* ---------- 报销单 ---------- */
  function reimbursement(d) {
    const items = d.items || [];
    const invs  = d.invoices || [];
    const logs  = d.logs || [];

    const statusCls = { '草稿': 'draft', '待审批': 'pending', '已通过': 'ok', '已付款': 'paid', '已驳回': 'rejected', '已取消': 'rejected' };
    const cls = statusCls[d.status] || '';

    // 三行信息区：标题行 + 明细行 + 报销事由行
    const metaRows = `
      <tr>
        <th class="lbl">申请部门</th><td class="val">${esc(d.department_name || '—')}</td>
        <th class="lbl">申请人</th><td class="val">${esc(d.applicant_name || '—')}</td>
        <th class="lbl">费用期间</th><td class="val">${date(d.occur_start)}${d.occur_end && d.occur_end !== d.occur_start ? ' ~ ' + date(d.occur_end) : ''}</td>
      </tr>
      <tr>
        <th class="lbl">客户名称</th><td class="val">${esc(d.customer_name || '—')}</td>
        <th class="lbl">项目名称</th><td class="val">${esc(d.project_name || '—')}</td>
        <th class="lbl">提交日期</th><td class="val">${dt(d.submit_at) || '—'}</td>
      </tr>
      <tr>
        <th class="lbl">报销事由</th>
        <td colspan="5" class="val" style="font-size:13px;font-weight:700">${esc(d.title || '')}${d.purpose ? ' <span class="span">' + esc(d.purpose) + '</span>' : ''}</td>
      </tr>`;

    const itemRows = items.map((i, k) => `
      <tr>
        <td class="ctr">${k + 1}</td>
        <td>${esc(i.category_name || '未分类')}</td>
        <td class="ctr">${date(i.occur_date)}</td>
        <td>${esc(i.description || '—')}</td>
        <td class="num">${money(i.tax_amount)}</td>
        <td class="num">${money(i.amount)}</td>
        <td class="ctr">${i.invoice_count ? i.invoice_count + ' 张' : '<span style="color:#c00">缺</span>'}</td>
      </tr>`).join('') || '<tr><td colspan="7" class="ctr" style="color:#888">无明细</td></tr>';

    const invRows = invs.map((v, k) => `
      <tr>
        <td class="ctr">${k + 1}</td>
        <td>${esc(v.invoice_no)}</td>
        <td>${esc(v.invoice_type)}</td>
        <td class="ctr">${date(v.invoice_date)}</td>
        <td>${esc(v.seller_name || '—')}</td>
        <td class="num">${money(v.amount)}</td>
        <td class="ctr">${esc(v.check_status)}</td>
      </tr>`).join('') || '<tr><td colspan="7" class="ctr" style="color:#888">无关联发票</td></tr>';

    const logRows = logs.map(l => `
      <tr>
        <td class="ctr" style="white-space:nowrap">${dt(l.created_at)}</td>
        <td>${esc(l.operator || '系统')}</td>
        <td>${esc(l.action)}${l.from_status ? `<br><span style="font-size:10.5px;color:#888">${esc(l.from_status)} → ${esc(l.to_status)}</span>` : ''}</td>
        <td>${esc(l.comment || '')}</td>
      </tr>`).join('') || '';

    function ymdLine(s) {
      if (!s) return '<div class="ymd"><span>____</span><span>__</span><span>__</span></div>';
      const d = new Date(s);
      const p = x => String(x).padStart(2, '0');
      return `<div class="ymd"><span>${d.getFullYear()}</span><span>${p(d.getMonth()+1)}</span><span>${p(d.getDate())}</span></div>`;
    }

    const isDone = ['已通过', '已付款'].includes(d.status);

    const body = `
<div class="page">

  <!-- 公司抬头 -->
  <div class="co-head">
    <div class="co-logo">&#9632;</div>
    <div>
      <div class="co-name">销售费用报销管理工作台</div>
      <div class="co-tag">Reimburse Management System · EXPENSE REIMBURSEMENT VOUCHER</div>
    </div>
  </div>

  <!-- 单据标题 -->
  <div class="doc-title">费 用 报 销 单</div>

  <!-- 信息条 -->
  <div class="doc-meta">
    <div class="item"><div class="lbl">单据编号</div><div class="val">${esc(d.code)}</div></div>
    <div class="item"><div class="lbl">单据状态</div><div class="val ${cls}">${esc(d.status)}</div></div>
    <div class="item"><div class="lbl">申请部门</div><div class="val">${esc(d.department_name || '—')}</div></div>
    <div class="item"><div class="lbl">申请人</div><div class="val">${esc(d.applicant_name || '—')}</div></div>
    <div class="item"><div class="lbl">费用期间</div><div class="val">${date(d.occur_start)} ~ ${date(d.occur_end)}</div></div>
  </div>

  <!-- 详细信息 -->
  <div class="info-block">
    <table class="tbl"><tbody>
      ${metaRows}
    </tbody></table>
  </div>

  <!-- 费用明细 -->
  <div class="tbl-wrap">
    <div class="tbl-head">费用明细</div>
    <table class="tbl">
      <thead><tr>
        <th style="width:26px">#</th>
        <th style="width:90px">费用类型</th>
        <th style="width:76px">发生日期</th>
        <th>费用摘要</th>
        <th style="width:68px">税额</th>
        <th style="width:84px">金额</th>
        <th style="width:42px">发票</th>
      </tr></thead>
      <tbody>${itemRows}</tbody>
      <tfoot><tr>
        <td colspan="5" class="cap">合计（人民币大写）</td>
        <td class="num">¥${money(d.total_amount)}</td>
        <td></td>
      </tr></tfoot>
    </table>
  </div>

  <!-- 合计金额突出条 -->
  <div class="total-bar">
    <div class="cap">报销金额（人民币大写）</div>
    <div class="amt">¥${money(d.total_amount)} <small>（${esc(rmbUpper(d.total_amount))}）</small></div>
  </div>

  <!-- 关联发票 -->
  ${invs.length ? `
  <div class="tbl-wrap">
    <div class="tbl-head">关联发票（${invs.length} 张）</div>
    <table class="tbl">
      <thead><tr>
        <th style="width:26px">#</th>
        <th style="width:158px">发票号码</th>
        <th style="width:78px">类型</th>
        <th style="width:74px">开票日期</th>
        <th>销售方</th>
        <th style="width:82px">价税合计</th>
        <th style="width:52px">查验</th>
      </tr></thead>
      <tbody>${invRows}</tbody>
    </table>
  </div>` : ''}

  <!-- 流转记录 -->
  ${logs.length ? `
  <div class="tbl-wrap">
    <div class="tbl-head">流转记录</div>
    <table class="tbl">
      <thead><tr>
        <th style="width:120px">时间</th>
        <th style="width:72px">操作人</th>
        <th style="width:100px">操作</th>
        <th>备注</th>
      </tr></thead>
      <tbody>${logRows}</tbody>
    </table>
  </div>` : ''}

  <!-- 签字栏 -->
  <div class="sign-wrap">
    <div class="sign-head">签 字 确 认 栏</div>
    <div class="sign-grid">
      <div class="sign-box">
        <div class="role">申请人</div>
        <div class="name">${esc(d.applicant_name || '')}</div>
        <div class="line">${ymdLine(d.submit_at)}</div>
      </div>
      <div class="sign-box">
        <div class="role">部门负责人</div>
        <div class="name">${isDone ? esc(d.approver || '') : ''}</div>
        <div class="line">${isDone ? ymdLine(d.approve_at) : ymdLine(null)}</div>
      </div>
      <div class="sign-box">
        <div class="role">财务审核</div>
        <div class="name"></div>
        <div class="line">${ymdLine(null)}</div>
      </div>
      <div class="sign-box">
        <div class="role">${isDone ? '审批人' : '最终审批'}</div>
        <div class="name">${isDone ? esc(d.approver || '') : ''}</div>
        <div class="line">${isDone ? ymdLine(d.approve_at) : ymdLine(null)}</div>
      </div>
    </div>
  </div>

  <!-- 页脚（移到 bc-corner 卡片内） -->

</div>`;

    // v2.7.13：左下角只留 DM 码（无任何文字），码里只放单号。
    // 全量信息会把 DM 撑到 40×40 拆成 2×2 四个数据区（像"4 块拼凑"）；
    // ≤26×26 保持单数据区，规整易扫。姓名/部门/金额纸面明文已有。
    const corner = barcodeCorner(String(d.code || ''));
    deliver(shell(`费用报销单 ${d.code}`, body, corner));
  }

  /* ---------- 发票清单 / 单张 ---------- */
  function invoices(rows, subtitle) {
    const list  = rows || [];
    const total = list.reduce((a, b) => a + (Number(b.amount) || 0), 0);
    const tax   = list.reduce((a, b) => a + (Number(b.tax_amount) || 0), 0);
    const invCount = list.filter(v => v.check_status === '查验成功').length;

    const invRows = list.map((v, k) => `
      <tr>
        <td class="ctr">${k + 1}</td>
        <td>${esc(v.invoice_no)}</td>
        <td class="ctr">${esc(v.invoice_type)}</td>
        <td class="ctr">${date(v.invoice_date)}</td>
        <td>${esc(v.seller_name || '—')}</td>
        <td>${esc(v.buyer_name || '—')}</td>
        <td class="num">${money(v.amount)}</td>
        <td class="num">${money(v.tax_amount)}</td>
        <td class="ctr"><span style="color:${v.check_status==='查验成功'?'#1a7a1a':'#c00'}">${esc(v.check_status)}</span></td>
        <td>${esc(v.reimbursement_code || '—')}</td>
      </tr>`).join('') || '<tr><td colspan="10" class="ctr" style="color:#888">无记录</td></tr>';

    const body = `
<div class="page">

  <div class="co-head">
    <div class="co-logo">&#9633;</div>
    <div>
      <div class="co-name">销售费用报销管理工作台</div>
      <div class="co-tag">INVOICE LISTING · 发票清单</div>
    </div>
  </div>

  <div class="doc-title">发 票 清 单</div>

  <div class="doc-meta">
    <div class="item"><div class="lbl">单据类型</div><div class="val">${esc(subtitle || '批量清单')}</div></div>
    <div class="item"><div class="lbl">发票数量</div><div class="val">${list.length} 张</div></div>
    <div class="item"><div class="lbl">查验通过</div><div class="val" style="color:${invCount===list.length&&list.length>0?'#1a7a1a':'#b07000'}">${invCount} 张</div></div>
    <div class="item"><div class="lbl">打印日期</div><div class="val">${now()}</div></div>
  </div>

  <div class="tbl-wrap">
    <div class="tbl-head">发票明细（共 ${list.length} 张）</div>
    <table class="tbl">
      <thead><tr>
        <th style="width:24px">#</th>
        <th style="width:142px">发票号码</th>
        <th style="width:64px">类型</th>
        <th style="width:70px">开票日期</th>
        <th>销售方</th>
        <th>购买方</th>
        <th style="width:74px">价税合计</th>
        <th style="width:62px">税额</th>
        <th style="width:58px">查验</th>
        <th style="width:102px">关联单号</th>
      </tr></thead>
      <tbody>${invRows}</tbody>
      <tfoot><tr>
        <td colspan="6" class="cap">合计（${list.length} 张）&nbsp;&nbsp;人民币大写：${esc(rmbUpper(total))}</td>
        <td class="num">¥${money(total)}</td>
        <td class="num">¥${money(tax)}</td>
        <td colspan="2"></td>
      </tr></tfoot>
    </table>
  </div>

  <div class="total-bar">
    <div class="cap">价税合计（人民币大写）</div>
    <div class="amt">¥${money(total)} <small>（${esc(rmbUpper(total))}）</small></div>
  </div>

  </div>`;

    const corner = subtitle === '单张发票' && list[0]
      ? barcodeCorner(String(list[0].invoice_no || ''))
      : barcodeCorner('INV-LIST-' + now().replace(/-/g,'') + '-N' + list.length);
    deliver(shell('发票清单', body, corner));
  }

  function invoice(v) { return invoices([v], '单张发票'); }

  /* ---------- 暴露接口 ---------- */
  window.WB = window.WB || {};
  window.WB.print = { reimbursement, invoices, invoice, rmbUpper };
})();
