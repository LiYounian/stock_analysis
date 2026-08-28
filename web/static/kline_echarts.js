// 个股页 K线图(ECharts):蜡烛图 + 布林带 + 成交量 + MACD + RSI + KDJ
//   主图:蜡烛 + MA5/20/60 + 布林带(上/中/下轨) + 预测支撑/压力/止损止盈
//   子图:成交量 · MACD(柱+DIF/DEA) · RSI(6/12/24) · KDJ(K/D/J),全部与主图联动
// 数据来源:<script id="klineData"> / <script id="predData">(可选)
// A股配色:涨红 #ef232a / 跌绿 #14b143
(function () {
  const elData = document.getElementById("klineData");
  const elChart = document.getElementById("klineChart");
  if (!elData || !elChart || typeof echarts === "undefined") return;

  const kl = JSON.parse(elData.textContent);
  if (!kl.dates || !kl.dates.length) return;

  const predEl = document.getElementById("predData");
  const pred = predEl ? JSON.parse(predEl.textContent) : null;

  const UP = "#ef232a";   // 涨(红)
  const DOWN = "#14b143"; // 跌(绿)
  const GRID = "#222";

  // ECharts candlestick 每项顺序为 [open, close, low, high]
  const candle = kl.dates.map((_, i) => [
    kl.open[i], kl.close[i], kl.low[i], kl.high[i],
  ]);

  // 成交量柱:涨红跌绿(以当日收盘 vs 开盘判断)
  const volBars = kl.volume.map((v, i) => ({
    value: v,
    itemStyle: { color: kl.close[i] >= kl.open[i] ? UP : DOWN },
  }));

  // MACD 柱:正红负绿(2×(dif-dea) 口径已在分析层算好)
  const macdBars = (kl.macd_hist || []).map((v) => ({
    value: v,
    itemStyle: { color: v == null ? "#888" : (v >= 0 ? UP : DOWN) },
  }));

  // 主图折线(MA):默认挂在 grid0/yAxis0
  const maSeries = (name, arr, color) => ({
    name, type: "line", data: arr || [], smooth: true,
    symbol: "none", connectNulls: true,
    lineStyle: { width: 1.2, color },
    z: 3,
  });

  // 主图布林带线(灰调,中轨虚线,不与 MA 抢色)
  const bollSeries = (name, arr, dashed) => ({
    name, type: "line", data: arr || [], smooth: false,
    symbol: "none", connectNulls: true,
    lineStyle: { width: 1, color: "#9aa7b8", type: dashed ? "dashed" : "solid", opacity: 0.9 },
    z: 2,
  });

  // 子图折线(绑定到指定 grid 的 x/y 轴)
  const sub = (name, arr, color, gi, marks) => {
    const s = {
      name, type: "line", data: arr || [], smooth: false,
      symbol: "none", connectNulls: true,
      xAxisIndex: gi, yAxisIndex: gi,
      lineStyle: { width: 1, color }, z: 3,
    };
    if (marks) s.markLine = {
      symbol: "none", silent: true,
      data: marks.map((y) => ({ yAxis: y })),
      lineStyle: { color: "#555", type: "dashed", width: 1 },
      label: { show: true, position: "insideEndTop", formatter: "{c}", color: "#666", fontSize: 9 },
    };
    return s;
  };

  // 预测叠加:支撑/压力/止损/止盈 —— 用 markLine 画水平线(挂主图蜡烛)
  const markLines = [];
  const addLine = (y, label, color, dash) => {
    if (y == null) return;
    markLines.push({
      yAxis: y,
      lineStyle: { color, type: dash ? "dashed" : "solid", width: 1 },
      label: { show: true, position: "insideEndTop", formatter: label, color, fontSize: 10 },
    });
  };
  if (pred) {
    (pred.支撑位 || []).forEach((s, i) => addLine(s, "支撑" + (i + 1), "#2ecc71", true));
    (pred.压力位 || []).forEach((s, i) => addLine(s, "压力" + (i + 1), "#e74c3c", true));
    addLine(pred.止损, "止损(5日)", "#c0392b", true);
    addLine(pred.止盈, "止盈(5日)", "#27ae60", true);
  }

  const chart = echarts.init(elChart, null, { renderer: "canvas" });

  // 子图 category 轴(仅最底部 KDJ 显示日期标签)
  const subXAxis = (gi, showLabel) => ({
    type: "category", data: kl.dates, scale: true, boundaryGap: true,
    gridIndex: gi, axisLine: { lineStyle: { color: "#555" } },
    axisTick: { show: false }, splitLine: { show: false },
    axisLabel: showLabel ? { color: "#777", fontSize: 10 } : { show: false },
  });
  const subYAxis = (gi, opt) => Object.assign({
    scale: true, gridIndex: gi, splitNumber: 2,
    axisLine: { show: false }, axisTick: { show: false },
    axisLabel: { color: "#777", fontSize: 9 },
    splitLine: { show: false },
  }, opt || {});

  const subTitle = (text, top) => ({
    text, left: 54, top, textStyle: { color: "#8a90a0", fontSize: 10, fontWeight: "normal" },
  });

  const option = {
    animation: false,
    backgroundColor: "transparent",
    title: [
      subTitle("成交量", "44.5%"),
      subTitle("MACD (12,26,9)", "56.5%"),
      subTitle("RSI (6,12,24)", "69.5%"),
      subTitle("KDJ (9,3,3)", "82.5%"),
    ],
    legend: {
      data: ["K线", "MA5", "MA20", "MA60", "BOLL上", "BOLL中", "BOLL下"],
      top: 4, textStyle: { color: "#aaa", fontSize: 10 },
    },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
      backgroundColor: "rgba(28,31,38,0.95)",
      borderColor: "#2a2e37",
      textStyle: { color: "#d8dbe0", fontSize: 12 },
    },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [
      { left: 52, right: 16, top: 30, height: "38%" },   // 0 K线 + MA + BOLL
      { left: 52, right: 16, top: "47%", height: "8%" },  // 1 成交量
      { left: 52, right: 16, top: "59%", height: "9%" },  // 2 MACD
      { left: 52, right: 16, top: "72%", height: "9%" },  // 3 RSI
      { left: 52, right: 16, top: "85%", height: "9%" },  // 4 KDJ
    ],
    xAxis: [
      {
        type: "category", data: kl.dates, scale: true, boundaryGap: true,
        axisLine: { lineStyle: { color: "#555" } },
        axisLabel: { show: false }, axisTick: { show: false },
        splitLine: { show: false }, gridIndex: 0,
      },
      subXAxis(1, false),
      subXAxis(2, false),
      subXAxis(3, false),
      subXAxis(4, true),
    ],
    yAxis: [
      {
        scale: true, gridIndex: 0,
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: "#777", fontSize: 10 },
        splitLine: { lineStyle: { color: GRID } },
      },
      subYAxis(1),
      subYAxis(2),
      subYAxis(3, { min: 0, max: 100 }),   // RSI 固定 0~100
      subYAxis(4),                         // KDJ 自适应(J 可越界)
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1, 2, 3, 4], start: 40, end: 100 },
      {
        type: "slider", xAxisIndex: [0, 1, 2, 3, 4], bottom: 6, height: 16,
        start: 40, end: 100,
        textStyle: { color: "#777" }, borderColor: "#2a2e37",
        fillerColor: "rgba(74,144,226,0.15)",
        handleStyle: { color: "#4a90e2" },
      },
    ],
    series: [
      {
        name: "K线", type: "candlestick", data: candle,
        xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: {
          color: UP, color0: DOWN,        // 阳线填充 / 阴线填充
          borderColor: UP, borderColor0: DOWN,
        },
        markLine: markLines.length
          ? { symbol: "none", data: markLines, silent: true }
          : undefined,
        z: 2,
      },
      maSeries("MA5", kl.ma5, "#f5a623"),
      maSeries("MA20", kl.ma20, "#4a90e2"),
      maSeries("MA60", kl.ma60, "#bd10e0"),
      bollSeries("BOLL上", kl.boll_up, false),
      bollSeries("BOLL中", kl.boll_mid, true),
      bollSeries("BOLL下", kl.boll_low, false),
      {
        name: "成交量", type: "bar", data: volBars,
        xAxisIndex: 1, yAxisIndex: 1,
      },
      {
        name: "MACD柱", type: "bar", data: macdBars,
        xAxisIndex: 2, yAxisIndex: 2, barWidth: "60%",
      },
      sub("DIF", kl.dif, "#e0c060", 2),
      sub("DEA", kl.dea, "#4a90e2", 2),
      sub("RSI6", kl.rsi6, "#f5a623", 3, [70, 30]),
      sub("RSI12", kl.rsi12, "#4a90e2", 3),
      sub("RSI24", kl.rsi24, "#bd10e0", 3),
      sub("K", kl.kdj_k, "#f5a623", 4, [80, 20]),
      sub("D", kl.kdj_d, "#4a90e2", 4),
      sub("J", kl.kdj_j, "#e0559a", 4),
    ],
  };

  chart.setOption(option);
  window.addEventListener("resize", () => chart.resize());
})();
