// SEPA+VCP 收缩结构参考图:K线 + MA50/150/200 + 每轮高→低线段
// 文案不含「VCP 完成」。数据:<script id="sepaChartData">
(function () {
  const elData = document.getElementById("sepaChartData");
  const elChart = document.getElementById("sepaChart");
  if (!elData || !elChart || typeof echarts === "undefined") return;
  const kl = JSON.parse(elData.textContent);
  if (!kl.dates || !kl.dates.length) return;

  const UP = "#ef232a";
  const DOWN = "#14b143";
  const candle = kl.dates.map((_, i) => [kl.open[i], kl.close[i], kl.low[i], kl.high[i]]);
  const volBars = (kl.volume || []).map((v, i) => ({
    value: v,
    itemStyle: { color: kl.close[i] >= kl.open[i] ? UP : DOWN },
  }));
  const maSeries = (name, arr, color) => ({
    name, type: "line", data: arr || [], smooth: true,
    symbol: "none", connectNulls: true,
    lineStyle: { width: 1.2, color }, z: 3,
  });

  const markLines = [];
  (kl.rounds || []).forEach((r, i) => {
    const label = "第" + (i + 1) + "轮 " + r["回撤%"] + "% / " + r["天数"] + "日"
      + (r["段均量"] != null && r["五十日均量"] != null
        ? " 量" + Math.round(r["段均量"] / r["五十日均量"] * 100) + "%均"
        : "")
      + (r["进行中"] ? " 进行中" : "");
    markLines.push([
      { coord: [r.start_date, r.high], lineStyle: { color: "#f5a623", width: 1.6 } },
      { coord: [r.end_date, r.low],
        label: { show: true, formatter: label, color: "#f5a623", fontSize: 10 } },
    ]);
  });

  const chart = echarts.init(elChart, null, { renderer: "canvas" });
  chart.setOption({
    animation: false,
    backgroundColor: "transparent",
    legend: {
      data: ["K线", "MA50", "MA150", "MA200"],
      top: 4, textStyle: { color: "#aaa", fontSize: 10 },
    },
    tooltip: {
      trigger: "axis", axisPointer: { type: "cross" },
      backgroundColor: "rgba(28,31,38,0.95)",
      textStyle: { color: "#d8dbe0", fontSize: 12 },
    },
    grid: [
      { left: 52, right: 16, top: 34, height: "58%" },
      { left: 52, right: 16, top: "74%", height: "16%" },
    ],
    xAxis: [
      { type: "category", data: kl.dates, scale: true, boundaryGap: true,
        axisLabel: { color: "#777", fontSize: 10 }, gridIndex: 0 },
      { type: "category", data: kl.dates, gridIndex: 1, axisLabel: { show: false } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, axisLabel: { color: "#777", fontSize: 10 },
        splitLine: { lineStyle: { color: "#222" } } },
      { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { color: "#777", fontSize: 10 } },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1], start: 30, end: 100 },
      { type: "slider", xAxisIndex: [0, 1], bottom: 6, height: 16, start: 30, end: 100 },
    ],
    series: [
      {
        name: "K线", type: "candlestick", data: candle,
        itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
        markLine: markLines.length
          ? { symbol: "none", data: markLines, silent: true }
          : undefined,
      },
      maSeries("MA50", kl.ma50, "#f5a623"),
      maSeries("MA150", kl.ma150, "#4a90e2"),
      maSeries("MA200", kl.ma200, "#bd10e0"),
      { name: "成交量", type: "bar", data: volBars, xAxisIndex: 1, yAxisIndex: 1 },
    ],
  });
  window.addEventListener("resize", () => chart.resize());
})();
