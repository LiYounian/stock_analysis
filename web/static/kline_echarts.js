// 个股页 K线图(ECharts):蜡烛图 + 成交量 + MA5/20/60 + 十字光标 + dataZoom
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

  // ECharts candlestick 每项顺序为 [open, close, low, high]
  const candle = kl.dates.map((_, i) => [
    kl.open[i], kl.close[i], kl.low[i], kl.high[i],
  ]);

  // 成交量柱:涨红跌绿(以当日收盘 vs 开盘判断)
  const volBars = kl.volume.map((v, i) => ({
    value: v,
    itemStyle: { color: kl.close[i] >= kl.open[i] ? UP : DOWN },
  }));

  const maSeries = (name, arr, color) => ({
    name, type: "line", data: arr, smooth: true,
    symbol: "none", connectNulls: true,
    lineStyle: { width: 1.2, color },
    z: 3,
  });

  // 预测叠加:支撑/压力/止损/止盈 —— 用 markLine 画水平线
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

  const option = {
    animation: false,
    backgroundColor: "transparent",
    legend: {
      data: ["K线", "MA5", "MA20", "MA60"],
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
      { left: 52, right: 16, top: 34, height: "58%" },   // 上:K线 + MA
      { left: 52, right: 16, top: "74%", height: "16%" }, // 下:成交量
    ],
    xAxis: [
      {
        type: "category", data: kl.dates, scale: true, boundaryGap: true,
        axisLine: { lineStyle: { color: "#555" } },
        axisLabel: { color: "#777", fontSize: 10 },
        splitLine: { show: false },
        gridIndex: 0,
      },
      {
        type: "category", data: kl.dates, scale: true, boundaryGap: true,
        gridIndex: 1, axisLine: { lineStyle: { color: "#555" } },
        axisLabel: { show: false }, axisTick: { show: false },
        splitLine: { show: false },
      },
    ],
    yAxis: [
      {
        scale: true, gridIndex: 0,
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: "#777", fontSize: 10 },
        splitLine: { lineStyle: { color: "#222" } },
      },
      {
        scale: true, gridIndex: 1, splitNumber: 2,
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: "#777", fontSize: 10 },
        splitLine: { show: false },
      },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1], start: 40, end: 100 },
      {
        type: "slider", xAxisIndex: [0, 1], bottom: 6, height: 16,
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
      {
        name: "成交量", type: "bar", data: volBars,
        xAxisIndex: 1, yAxisIndex: 1,
      },
    ],
  };

  chart.setOption(option);
  window.addEventListener("resize", () => chart.resize());
})();
