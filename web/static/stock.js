// 个股页图表:价格(收盘+均线+支撑/压力/止盈止损)+ 成交量
(function () {
  const kl = JSON.parse(document.getElementById("klineData").textContent);
  const predEl = document.getElementById("predData");
  const pred = predEl ? JSON.parse(predEl.textContent) : null;
  if (!kl.dates || !kl.dates.length) return;

  const n = kl.dates.length;
  const flat = (v) => (v == null ? null : Array(n).fill(v));
  const line = (data, color, label, opts = {}) => Object.assign({
    label, data, borderColor: color, borderWidth: 1.4, pointRadius: 0,
    tension: 0.15, fill: false,
  }, opts);

  const priceSets = [
    line(kl.close, "#e6e6e6", "收盘", { borderWidth: 2 }),
    line(kl.ma5, "#f5a623", "MA5"),
    line(kl.ma20, "#4a90e2", "MA20"),
    line(kl.ma60, "#bd10e0", "MA60"),
  ];
  const dash = { borderDash: [5, 4], borderWidth: 1.2, pointRadius: 0 };
  if (pred) {
    (pred.支撑位 || []).forEach((s, i) =>
      priceSets.push(line(flat(s), "#2ecc71", "支撑" + (i + 1), dash)));
    (pred.压力位 || []).forEach((s, i) =>
      priceSets.push(line(flat(s), "#e74c3c", "压力" + (i + 1), dash)));
    if (pred.止损 != null) priceSets.push(line(flat(pred.止损), "#c0392b", "止损(5日)", dash));
    if (pred.止盈 != null) priceSets.push(line(flat(pred.止盈), "#27ae60", "止盈(5日)", dash));
  }

  new Chart(document.getElementById("priceChart"), {
    type: "line",
    data: { labels: kl.dates, datasets: priceSets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { intersect: false, mode: "index" },
      plugins: { legend: { labels: { color: "#aaa", boxWidth: 12, font: { size: 10 } } } },
      scales: {
        x: { ticks: { color: "#777", maxTicksLimit: 10 }, grid: { color: "#222" } },
        y: { ticks: { color: "#777" }, grid: { color: "#222" } },
      },
    },
  });

  new Chart(document.getElementById("volChart"), {
    type: "bar",
    data: {
      labels: kl.dates,
      datasets: [{
        label: "成交量", data: kl.volume,
        backgroundColor: kl.close.map((c, i) =>
          i > 0 && c >= kl.close[i - 1] ? "#c0392b88" : "#27ae6088"),
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { display: false },
        y: { ticks: { color: "#777", maxTicksLimit: 4 }, grid: { color: "#222" } },
      },
    },
  });
})();
