/* 专家合议:前端勾选重合成(F5·D7)。
 *
 * 数据流:后端把各专家信封 + config(tau/ε/权重/分母模式)随记录落库(council.build_council_block);
 * 前端勾选专家后,**按同一公式、同一 config 常量重新合成**——不触网、不触发后端重算(防前后端漂移)。
 * 合成公式与 tools/analysis/council.py::convene 完全一致:
 *   contrib_i = 强度_i × 置信度_i × 权重_i ; S = Σcontrib / 分母 ; S≥τ看多 / ≤−τ看空 / 其间中性。
 *   分母 = Σ(权重×置信度)(置信度加权,默认;弃权者退出)或 Σ权重(等权),由落库 config.分母模式 决定。
 *   冲突:正反两方各自 |Σ贡献| ≥ ε(仅标注,不改判)。
 * 非投资建议。
 *
 * councilSynth 是**唯一公式实现**:个股页(本文件 IIFE)与选股结果页(selection.html)都调它,别再写第二份。
 */
window.councilSynth = function (experts, isChecked, cfg) {
  cfg = cfg || {};
  var tau = cfg.tau != null ? cfg.tau : 0.2;
  var eps = cfg.conflict_epsilon != null ? cfg.conflict_epsilon : 0.05;
  var confWeighted = (cfg["分母模式"] || "置信度加权") === "置信度加权";
  var denom = 0, sumC = 0, rows = [], n = 0;
  (experts || []).forEach(function (e) {
    if (!isChecked(e["专家"])) return;
    n += 1;
    var w = e["默认权重"] != null ? e["默认权重"] : 1.0;
    var conf = e["置信度"] || 0;
    var contrib = (e["强度"] || 0) * conf * w;
    denom += confWeighted ? (w * conf) : w;       // 弃权 conf=0 → 不入分母,不稀释
    sumC += contrib;
    rows.push({ n: e["专家"], dir: e["方向"], s: e["强度"] || 0, conf: conf, w: w,
                c: contrib, 依据: (e["依据"] || []).join("、"), suff: e["数据充分度"] });
  });
  var S = denom > 0 ? sumC / denom : 0;
  var dir = S >= tau ? "看多" : (S <= -tau ? "看空" : "中性");
  var pos = 0, neg = 0;
  rows.forEach(function (r) { if (r.c > 0) pos += r.c; else if (r.c < 0) neg += -r.c; });
  rows.sort(function (a, b) { return Math.abs(b.c) - Math.abs(a.c); });
  return { S: S, dir: dir, conflict: pos >= eps && neg >= eps, checkedCount: n,
           rows: rows, confWeighted: confWeighted, tau: tau, eps: eps };
};

window.councilDirClass = { "看多": "buy", "看空": "sell", "中性": "wait", "不适用": "wait" };
window.round4 = function (x) { return Math.round(x * 1e4) / 1e4; };

/* 个股页:单票合议卡的勾选重合成(复用 councilSynth) */
(function () {
  var el = document.getElementById("councilData");
  if (!el) return;
  var data;
  try { data = JSON.parse(el.textContent); } catch (e) { return; }
  var experts = data.experts || [];
  var cfg = data.config || {};

  function recompute() {
    var checkedSet = {};
    document.querySelectorAll(".expert-cb").forEach(function (cb) {
      if (cb.checked) checkedSet[cb.value] = true;
    });
    var r = window.councilSynth(experts, function (n) { return !!checkedSet[n]; }, cfg);

    var dEl = document.getElementById("councilDir");
    dEl.textContent = r.dir;
    dEl.className = "badge big " + (window.councilDirClass[r.dir] || "wait");
    document.getElementById("councilScore").textContent =
      " 综合分 " + window.round4(r.S) + " · 参与 " + r.checkedCount + " 专家";
    document.getElementById("councilConflict").style.display = r.conflict ? "" : "none";

    var tb = document.getElementById("councilAttr");
    tb.innerHTML = "";
    r.rows.forEach(function (row) {
      var cls = row.c > 0 ? "pos" : (row.c < 0 ? "neg" : "muted");
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + row.n + (row.suff === "缺失" ? " <span class='tiny muted'>(弃权)</span>" : "") + "</td>" +
        "<td class='" + cls + "'>" + row.dir + "</td>" +
        "<td class='" + cls + "'>" + window.round4(row.c) + "</td>" +
        "<td>" + window.round4(row.conf) + "</td>" +
        "<td>" + row.w + "</td>" +
        "<td class='ai-comment muted'>" + row.依据 + "</td>";
      tb.appendChild(tr);
    });
    var denomText = r.confWeighted ? "Σ(权重×置信度)" : "Σ权重";
    document.getElementById("councilCaption").textContent =
      "口径:S=Σ(强度×置信度×权重)/" + denomText + " · τ=" + r.tau +
      " · 仲裁=加权求和(冲突仅标注) · ε=" + r.eps;
  }

  document.querySelectorAll(".expert-cb").forEach(function (cb) {
    cb.addEventListener("change", recompute);
  });
  recompute();
})();
