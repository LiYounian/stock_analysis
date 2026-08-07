/* 专家合议:前端勾选重合成(F5·D7)。
 *
 * 数据流:后端把各专家信封 + config(tau/ε/权重)随记录落库(council.build_council_block);
 * 前端勾选专家后,**按同一公式、同一 config 常量重新合成**——不触网、不触发后端重算(防前后端漂移)。
 * 合成公式与 tools/analysis/council.py::convene 完全一致:
 *   contrib_i = 强度_i × 置信度_i × 权重_i ; S = Σcontrib / Σ权重 ; S≥τ看多 / ≤−τ看空 / 其间中性。
 *   冲突:正反两方各自 |Σ贡献| ≥ ε(仅标注,不改判)。
 * 非投资建议。
 */
(function () {
  var el = document.getElementById("councilData");
  if (!el) return;
  var data;
  try { data = JSON.parse(el.textContent); } catch (e) { return; }
  var experts = data.experts || [];
  var cfg = data.config || {};
  var tau = cfg.tau != null ? cfg.tau : 0.2;
  var eps = cfg.conflict_epsilon != null ? cfg.conflict_epsilon : 0.05;

  var byName = {};
  experts.forEach(function (e) { byName[e["专家"]] = e; });

  var dirClass = { "看多": "buy", "看空": "sell", "中性": "wait", "不适用": "wait" };

  function round4(x) { return Math.round(x * 1e4) / 1e4; }

  function recompute() {
    var checked = [];
    document.querySelectorAll(".expert-cb").forEach(function (cb) {
      if (cb.checked && byName[cb.value]) checked.push(byName[cb.value]);
    });
    var sumW = 0, sumC = 0, rows = [];
    checked.forEach(function (e) {
      var w = e["默认权重"] != null ? e["默认权重"] : 1.0;
      var contrib = (e["强度"] || 0) * (e["置信度"] || 0) * w;
      sumW += w; sumC += contrib;
      rows.push({ n: e["专家"], dir: e["方向"], s: e["强度"] || 0,
                  conf: e["置信度"] || 0, w: w, c: contrib,
                  依据: (e["依据"] || []).join("、"), suff: e["数据充分度"] });
    });
    var S = sumW > 0 ? sumC / sumW : 0;
    var dir = S >= tau ? "看多" : (S <= -tau ? "看空" : "中性");
    var pos = 0, neg = 0;
    rows.forEach(function (r) { if (r.c > 0) pos += r.c; else if (r.c < 0) neg += -r.c; });
    var conflict = pos >= eps && neg >= eps;
    rows.sort(function (a, b) { return Math.abs(b.c) - Math.abs(a.c); });

    var dEl = document.getElementById("councilDir");
    dEl.textContent = dir;
    dEl.className = "badge big " + (dirClass[dir] || "wait");
    document.getElementById("councilScore").textContent =
      " 综合分 " + round4(S) + " · 参与 " + checked.length + " 专家";
    document.getElementById("councilConflict").style.display = conflict ? "" : "none";

    var tb = document.getElementById("councilAttr");
    tb.innerHTML = "";
    rows.forEach(function (r) {
      var cls = r.c > 0 ? "pos" : (r.c < 0 ? "neg" : "muted");
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + r.n + (r.suff === "缺失" ? " <span class='tiny muted'>(弃权)</span>" : "") + "</td>" +
        "<td class='" + cls + "'>" + r.dir + "</td>" +
        "<td class='" + cls + "'>" + round4(r.c) + "</td>" +
        "<td>" + round4(r.conf) + "</td>" +
        "<td>" + r.w + "</td>" +
        "<td class='ai-comment muted'>" + r.依据 + "</td>";
      tb.appendChild(tr);
    });
    document.getElementById("councilCaption").textContent =
      "口径:S=Σ(强度×置信度×权重)/Σ权重 · τ=" + tau + " · 仲裁=加权求和(冲突仅标注) · ε=" + eps;
  }

  document.querySelectorAll(".expert-cb").forEach(function (cb) {
    cb.addEventListener("change", recompute);
  });
  recompute();
})();
