/* ECharts 封装：统一浅色主题、人民币格式、自适应缩放 */
window.WB = window.WB || {};

(function () {
  const U = WB.util;
  const instances = new Set();

  const PALETTE = ['#1677ff', '#00b42a', '#ff7d00', '#722ed1', '#f53f3f', '#13c2c2',
                   '#faad14', '#eb2f96', '#2f54eb', '#a0d911', '#fa541c', '#08979c'];

  const TEXT = '#1b2430';
  const MUTED = '#7a8699';
  const LINE = '#eef1f6';

  const axisBase = {
    axisLine: { lineStyle: { color: '#e7ebf2' } },
    axisTick: { show: false },
    axisLabel: { color: MUTED, fontSize: 11 },
  };
  const catAxis = { ...axisBase, type: 'category', splitLine: { show: false } };
  const valAxis = {
    ...axisBase,
    type: 'value',
    splitLine: { lineStyle: { color: LINE } },
    axisLine: { show: false },
  };

  function tip(extra) {
    return Object.assign(
      {
        trigger: 'axis',
        backgroundColor: 'rgba(255,255,255,.97)',
        borderColor: '#e7ebf2',
        borderWidth: 1,
        padding: [8, 12],
        textStyle: { color: TEXT, fontSize: 12 },
        extraCssText: 'box-shadow:0 6px 24px rgba(16,24,40,.12);border-radius:8px;',
        axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(22,119,255,.06)' } },
      },
      extra || {}
    );
  }

  const F = {
    money: (v) => U.money(v),
    moneyShort: (v) => (v == null ? '—' : U.moneyShort(v)),
  };

  const C = {};

  C.set = function (el, option) {
    if (!el) return null;
    let inst = echarts.getInstanceByDom(el);
    if (!inst) {
      inst = echarts.init(el, null, { renderer: 'canvas' });
      instances.add(inst);
    }
    inst.setOption(option, true);
    return inst;
  };

  C.disposeAll = function () {
    instances.forEach((i) => {
      try { i.dispose(); } catch (e) {}
    });
    instances.clear();
  };

  window.addEventListener('resize', U.debounce(() => instances.forEach((i) => i.resize()), 120));

  /* ---------------- 图表工厂 ---------------- */

  /* 金额柱 + 单量折线 双轴趋势 */
  C.trend = function (el, rows) {
    const months = rows.map((r) => r.month.slice(2));
    const now = new Date();
    const curKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
    const hasPartial = rows.some((r) => r.month === curKey);
    return C.set(el, {
      color: ['#1677ff', '#ff7d00'],
      tooltip: tip({
        formatter(ps) {
          const i = ps[0].dataIndex;
          const partial = rows[i].month === curKey ? '（本月进行中）' : '';
          return `<b>${rows[i].month}</b>${partial}<br/>报销金额：<b>${U.money(rows[i].amount)}</b>` +
            `<br/>已批金额：${U.money(rows[i].approved_amount)}<br/>报销单量：${rows[i].count} 单`;
        },
      }),
      legend: { right: 10, top: 0, itemWidth: 10, itemHeight: 10, textStyle: { color: MUTED, fontSize: 11 } },
      grid: { left: 6, right: 10, top: 32, bottom: 4, containLabel: true },
      xAxis: {
        ...catAxis,
        data: months,
        boundaryGap: true,
        axisLabel: {
          color: MUTED, fontSize: 11,
          formatter: (v, i) => (rows[i] && rows[i].month === curKey ? `{cur|${v}}` : v),
          rich: { cur: { color: '#1677ff', fontSize: 11, fontWeight: 600 } },
        },
      },
      yAxis: [
        { ...valAxis, name: '金额', nameTextStyle: { color: MUTED, fontSize: 11 }, axisLabel: { color: MUTED, fontSize: 11, formatter: (v) => U.moneyShort(v) } },
        { ...valAxis, name: '单量', nameTextStyle: { color: MUTED, fontSize: 11 }, splitLine: { show: false } },
      ],
      series: [
        {
          name: '报销金额', type: 'bar', barMaxWidth: 26,
          data: rows.map((r) => ({
            value: r.amount,
            itemStyle: {
              borderRadius: [4, 4, 0, 0],
              color: r.month === curKey ? 'rgba(22,119,255,.38)' : '#1677ff',
              borderColor: r.month === curKey ? '#1677ff' : 'transparent',
              borderWidth: r.month === curKey ? 1 : 0,
              borderType: 'dashed',
            },
          })),
        },
        {
          name: '报销单量', type: 'line', yAxisIndex: 1, smooth: true,
          symbolSize: 6, lineStyle: { width: 2 }, data: rows.map((r) => r.count),
        },
      ],
    });
  };

  C.hasPartialMonth = function () {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
  };

  /* 环形占比图 */
  C.donut = function (el, rows, { unit = '金额', valueKey = 'amount' } = {}) {
    const data = rows.map((r, i) => ({
      name: r.name, value: r[valueKey],
      itemStyle: { color: PALETTE[i % PALETTE.length] },
    }));
    const total = data.reduce((s, d) => s + d.value, 0) || 1;
    return C.set(el, {
      tooltip: tip({
        trigger: 'item',
        formatter: (p) => `${p.name}<br/>${unit}：<b>${unit === '金额' ? U.money(p.value) : U.num(p.value)}</b><br/>占比：${p.percent}%`,
      }),
      legend: {
        type: 'scroll', orient: 'vertical', right: 6, top: 6, bottom: 6,
        itemWidth: 9, itemHeight: 9, textStyle: { color: MUTED, fontSize: 11 },
        formatter: (n) => (n.length > 12 ? n.slice(0, 12) + '…' : n),
      },
      series: [
        {
          type: 'pie', radius: ['48%', '72%'], center: ['34%', '52%'],
          avoidLabelOverlap: true, minAngle: 2,
          label: { show: true, formatter: '{d}%', color: TEXT, fontSize: 11, fontWeight: 600 },
          labelLine: { length: 6, length2: 6, lineStyle: { color: '#d6dde8' } },
          data,
        },
      ],
    });
  };

  /* 横向条形排名 */
  C.hbar = function (el, rows, { valueKey = 'amount', unit = '金额', color = '#1677ff', topN = 10 } = {}) {
    const data = rows.slice(0, topN).slice().reverse();
    return C.set(el, {
      tooltip: tip({
        trigger: 'axis',
        formatter: (ps) => {
          const r = data[ps[0].dataIndex];
          const val = unit === '金额' ? U.money(r[valueKey]) : U.num(r[valueKey]);
          return `<b>${r.name}</b><br/>${unit}：<b>${val}</b><br/>单量：${r.count} 单 · 占比 ${r.ratio}%`;
        },
      }),
      grid: { left: 6, right: 62, top: 8, bottom: 4, containLabel: true },
      xAxis: { ...valAxis, axisLabel: { color: MUTED, fontSize: 11, formatter: (v) => (unit === '金额' ? U.moneyShort(v) : v) } },
      yAxis: {
        ...catAxis,
        data: data.map((r) => (r.name.length > 11 ? r.name.slice(0, 11) + '…' : r.name)),
      },
      series: [
        {
          type: 'bar', barMaxWidth: 15,
          data: data.map((r) => r[valueKey]),
          itemStyle: { borderRadius: [0, 4, 4, 0], color },
          label: {
            show: true, position: 'right', color: MUTED, fontSize: 11,
            formatter: (p) => (unit === '金额' ? U.moneyShort(p.value) : U.num(p.value)),
          },
        },
      ],
    });
  };

  /* 堆叠柱状 */
  C.stacked = function (el, months, series, { unit = '金额' } = {}) {
    return C.set(el, {
      color: PALETTE,
      tooltip: tip({
        formatter(ps) {
          const total = ps.reduce((s, p) => s + (p.value || 0), 0);
          const lines = ps
            .filter((p) => p.value)
            .sort((a, b) => b.value - a.value)
            .map((p) => `${p.marker}${p.seriesName}：${U.money(p.value)}`)
            .join('<br/>');
          return `<b>${ps[0].axisValue}</b><br/>${lines}<br/><b>合计：${U.money(total)}</b>`;
        },
      }),
      legend: { type: 'scroll', top: 0, right: 6, itemWidth: 9, itemHeight: 9, textStyle: { color: MUTED, fontSize: 11 } },
      grid: { left: 6, right: 10, top: 34, bottom: 4, containLabel: true },
      xAxis: { ...catAxis, data: months.map((m) => m.slice(2)) },
      yAxis: valAxis,
      series: series.slice(0, 8).map((s) => ({
        name: s.name, type: 'bar', stack: 'total', barMaxWidth: 24, data: s.data,
      })),
    });
  };

  /* 状态分布环形 */
  C.ring = function (el, rows, statusColor) {
    const data = rows.map((r) => ({
      name: r.name, value: r.amount,
      itemStyle: { color: statusColor[r.name] || '#1677ff' },
    }));
    return C.set(el, {
      tooltip: tip({
        trigger: 'item',
        formatter: (p) => {
          const r = rows[p.dataIndex];
          return `${p.name}<br/>金额：<b>${U.money(r.amount)}</b><br/>单量：${r.count} 单<br/>占比：${p.percent}%`;
        },
      }),
      legend: { bottom: 0, itemWidth: 9, itemHeight: 9, textStyle: { color: MUTED, fontSize: 11 } },
      series: [
        {
          type: 'pie', radius: ['52%', '74%'], center: ['50%', '45%'],
          label: { show: false }, labelLine: { show: false },
          data,
        },
      ],
    });
  };

  /* 费用大类：金额条 + 占比 */
  C.groupBar = function (el, rows) {
    return C.set(el, {
      tooltip: tip({
        formatter: (ps) => {
          const r = rows[ps[0].dataIndex];
          return `<b>${r.name}</b><br/>金额：<b>${U.money(r.amount)}</b><br/>笔数：${r.count} · 占比 ${r.ratio}%`;
        },
      }),
      grid: { left: 6, right: 12, top: 20, bottom: 4, containLabel: true },
      xAxis: { ...catAxis, data: rows.map((r) => r.name), axisLabel: { color: MUTED, fontSize: 11, interval: 0, rotate: rows.length > 6 ? 22 : 0 } },
      yAxis: { ...valAxis, axisLabel: { color: MUTED, fontSize: 11, formatter: (v) => U.moneyShort(v) } },
      series: [
        {
          type: 'bar', barMaxWidth: 34,
          data: rows.map((r, i) => ({
            value: r.amount,
            itemStyle: { borderRadius: [5, 5, 0, 0], color: PALETTE[i % PALETTE.length] },
          })),
        },
      ],
    });
  };

  WB.charts = C;
  WB.palette = PALETTE;
  WB.statusColor = {
    草稿: '#929fb2', 待审批: '#ff7d00', 已通过: '#1677ff', 已付款: '#00b42a', 已驳回: '#f53f3f',
  };
})();
