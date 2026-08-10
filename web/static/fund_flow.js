// 资金流向前端逻辑:直连东财 push2 接口,30 秒轮询,不走本项目后端。
// 数据接口来自东财公开 push2 集群(delay 优先,主域回退);全前端 fetch + JSONP 风格 GET。
// 页面 DOM 由 templates/fund_flow.html 提供,样式在 static/style.css 的 "资金流向" 段。
(function () {
  var REFRESH_MS = 30 * 1000;
  var HOSTS = [
    "https://push2delay.eastmoney.com",
    "https://push2.eastmoney.com"
  ];
  var FAV_KEY = "fund_flow_favs_v1";

  var sectorType = "all";        // "all" | "2" | "3" | "fav"
  var searchQuery = "";
  var allSectors = [];
  var favs = loadFavs();
  var refreshTimer = null;

  // —— 收藏(localStorage)——
  function loadFavs() {
    try {
      var raw = localStorage.getItem(FAV_KEY);
      var arr = raw ? JSON.parse(raw) : [];
      return new Set(Array.isArray(arr) ? arr : []);
    } catch (e) { return new Set(); }
  }
  function saveFavs() {
    try { localStorage.setItem(FAV_KEY, JSON.stringify(Array.from(favs))); } catch (e) {}
  }
  function toggleFav(name) {
    if (favs.has(name)) favs.delete(name); else favs.add(name);
    saveFavs(); updateFavCount(); applyFilter();
  }
  function updateFavCount() {
    var el = document.getElementById("ff-fav-count");
    if (!el) return;
    if (favs.size > 0) { el.style.display = "inline-block"; el.textContent = String(favs.size); }
    else { el.style.display = "none"; }
  }

  // —— 网络:多域名轮询 ——
  function getJson(path, params) {
    var qs = Object.keys(params).map(function (k) {
      return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]);
    }).join("&");
    var lastErr = null, i = 0;
    function tryHost() {
      if (i >= HOSTS.length) return Promise.reject(lastErr || new Error("all hosts failed"));
      return fetch(HOSTS[i] + path + "?" + qs, { credentials: "omit", cache: "no-store" })
        .then(function (r) { if (!r.ok) throw new Error("http " + r.status); return r.json(); })
        .then(function (d) { if (!d || d.rc !== 0) throw new Error("bad rc=" + (d && d.rc)); return d; })
        .catch(function (err) { lastErr = err; i++; return tryHost(); });
    }
    return tryHost();
  }

  // —— 格式化 ——
  function fmtYi(v) {
    if (v == null || isNaN(v)) return "--";
    var y = v / 1e8;
    var sign = y > 0 ? "+" : (y < 0 ? "-" : "");
    return sign + Math.abs(y).toFixed(2) + "亿";
  }
  function fmtTime() {
    var d = new Date();
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map(function (n) { return String(n).padStart(2, "0"); }).join(":");
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c];
    });
  }

  // —— 拉大盘 5 单(沪深指数汇总)——
  async function fetchMarketFlow() {
    var data = await getJson("/api/qt/ulist.np/get", {
      fltt: 2, secids: "1.000001,0.399001",
      fields: "f62,f66,f72,f78,f84,f184",
      ut: "b2884a393a59ad64002292a3e90d46a5"
    });
    if (!data || !data.data || !data.data.diff || !data.data.diff.length) throw new Error("empty market data");
    var sum = { main: 0, huge: 0, big: 0, mid: 0, small: 0 };
    data.data.diff.forEach(function (r) {
      sum.main  += Number(r.f62) || 0;
      sum.huge  += Number(r.f66) || 0;
      sum.big   += Number(r.f72) || 0;
      sum.mid   += Number(r.f78) || 0;
      sum.small += Number(r.f84) || 0;
    });
    return sum;
  }

  // —— 拉板块榜(行业 t:2 / 概念 t:3,双端合并)——
  async function fetchOneSectorType(type) {
    function fetchSide(po) {
      return getJson("/api/qt/clist/get", {
        pn: 1, pz: 100, po: po, np: 1, fltt: 2, invt: 2,
        fid: "f62", fs: "m:90+t:" + type,
        fields: "f12,f14,f62",
        ut: "b2884a393a59ad64002292a3e90d46a5"
      }).catch(function () { return null; });
    }
    var res = await Promise.all([fetchSide(1), fetchSide(0)]);
    var out = [];
    res.forEach(function (d) {
      if (!d || !d.data || !d.data.diff) return;
      var diff = d.data.diff;
      var list = Array.isArray(diff) ? diff : Object.keys(diff).map(function (k) { return diff[k]; });
      list.forEach(function (r) {
        if (r && r.f14) out.push({ code: r.f12, name: r.f14, main: parseFloat(r.f62) || 0, kind: type });
      });
    });
    return out;
  }
  async function fetchAllSectors() {
    var res = await Promise.all([fetchOneSectorType("2"), fetchOneSectorType("3")]);
    var lists = res[0].concat(res[1]).filter(function (r) { return !/[ⅡⅢ]/.test(r.name); });
    // 同名合并:行业+概念可能同名,保留绝对值大的净额,kind 兼有则记两个
    var byName = {};
    lists.forEach(function (r) {
      var cur = byName[r.name];
      if (!cur) { byName[r.name] = { code: r.code, name: r.name, main: r.main, kinds: [r.kind] }; return; }
      if (cur.kinds.indexOf(r.kind) < 0) cur.kinds.push(r.kind);
      if (Math.abs(r.main) > Math.abs(cur.main)) { cur.main = r.main; cur.code = r.code; }
    });
    return Object.keys(byName).map(function (k) { return byName[k]; });
  }

  // —— 渲染:大盘 5 单 ——
  function renderSummary(m) {
    var map = { small: m.small, mid: m.mid, big: m.big, huge: m.huge, main: m.main };
    document.querySelectorAll(".ff-cell").forEach(function (c) {
      var el = c.querySelector(".ff-val");
      var k = el.getAttribute("data-k");
      var v = map[k];
      el.textContent = fmtYi(v);
      c.classList.remove("up", "down");
      if (v > 0) c.classList.add("up");
      else if (v < 0) c.classList.add("down");
    });
  }

  // —— 渲染:板块榜(按 tab + 搜索过滤 + 排序 + 双列铺排)——
  function applyFilter() {
    var wrap = document.getElementById("ff-list");
    if (!allSectors.length) { wrap.innerHTML = ""; return; }

    var view = allSectors.slice();
    if (sectorType === "2" || sectorType === "3") {
      view = view.filter(function (s) { return s.kinds.indexOf(sectorType) >= 0; });
    } else if (sectorType === "fav") {
      view = view.filter(function (s) { return favs.has(s.name); });
    }
    if (searchQuery) {
      var q = searchQuery.toLowerCase();
      view = view.filter(function (s) { return s.name.toLowerCase().indexOf(q) >= 0; });
    }
    view.sort(function (a, b) { return b.main - a.main; });

    if (!view.length) {
      var msg;
      if (sectorType === "fav" && !searchQuery)
        msg = '<div class="ff-empty">还没有收藏的板块<br/><span class="muted tiny">在其他 tab 里点击板块前的星标即可收藏</span></div>';
      else if (searchQuery)
        msg = '<div class="ff-empty">没有匹配「' + escapeHtml(searchQuery) + '」的板块</div>';
      else
        msg = '<div class="ff-empty">暂无数据</div>';
      wrap.innerHTML = msg;
      return;
    }

    var maxAbs = 0;
    view.forEach(function (s) { var a = Math.abs(s.main); if (a > maxAbs) maxAbs = a; });
    if (maxAbs <= 0) maxAbs = 1;

    // 双列:1..half 左列,half+1..end 右列(视觉上"上到下再下一列")
    var n = view.length, half = Math.ceil(n / 2);
    var reordered = new Array(n);
    for (var i = 0; i < half; i++) {
      reordered[i * 2] = view[i];
      if (i + half < n) reordered[i * 2 + 1] = view[i + half];
    }
    reordered = reordered.filter(Boolean);

    wrap.innerHTML = reordered.map(function (s) {
      var rank = view.indexOf(s) + 1;
      var up = s.main >= 0;
      var pct = Math.min(100, Math.abs(s.main) / maxAbs * 100);
      var isFav = favs.has(s.name);
      var cls = "ff-row " + (up ? "up" : "down");
      return '<div class="' + cls + '">' +
        '<div class="ff-rank">' + rank + '</div>' +
        '<div class="ff-name">' + escapeHtml(s.name) + '</div>' +
        '<div class="ff-star ' + (isFav ? 'on' : '') + '" data-name="' + escapeHtml(s.name) + '" title="' + (isFav ? '取消收藏' : '收藏') + '">' + (isFav ? '★' : '☆') + '</div>' +
        '<div class="ff-track"><div class="ff-fill" style="width:' + pct.toFixed(1) + '%"></div></div>' +
        '<div class="ff-amount">' + fmtYi(s.main) + '</div>' +
        '</div>';
    }).join("");
  }

  function setError(msg) {
    var el = document.getElementById("ff-error");
    if (!msg) { el.style.display = "none"; el.textContent = ""; return; }
    el.style.display = "block"; el.textContent = msg;
  }

  // —— 主刷新循环 ——
  async function tick() {
    document.body.classList.add("ff-updating");
    try {
      var results = await Promise.allSettled([fetchMarketFlow(), fetchAllSectors()]);
      var mkt = results[0], sec = results[1];
      if (mkt.status === "fulfilled") renderSummary(mkt.value);
      if (sec.status === "fulfilled") { allSectors = sec.value; applyFilter(); }

      function reason(r) { return (r && r.reason && r.reason.message) || "unknown"; }
      if (mkt.status === "rejected" && sec.status === "rejected")
        setError("数据获取失败:大盘 " + reason(mkt) + ";板块 " + reason(sec));
      else if (mkt.status === "rejected")
        setError("大盘数据失败:" + reason(mkt));
      else if (sec.status === "rejected")
        setError("板块数据失败:" + reason(sec));
      else if (sec.status === "fulfilled" && (!sec.value || !sec.value.length))
        setError("板块接口返回空,可能被限流,30 秒后自动重试。");
      else
        setError("");
      document.getElementById("ff-updated").textContent = fmtTime();
    } catch (e) {
      setError("刷新出错:" + e.message);
    } finally {
      document.body.classList.remove("ff-updating");
    }
  }

  function startLoop() {
    if (refreshTimer) clearInterval(refreshTimer);
    tick();
    refreshTimer = setInterval(tick, REFRESH_MS);
  }

  // —— 事件 ——
  document.getElementById("ff-tabs").addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-t]");
    if (!btn) return;
    document.querySelectorAll("#ff-tabs button").forEach(function (b) { b.classList.remove("active"); });
    btn.classList.add("active");
    sectorType = btn.getAttribute("data-t");
    applyFilter();
  });

  var searchEl = document.getElementById("ff-search");
  searchEl.addEventListener("input", function () {
    searchQuery = searchEl.value.trim();
    applyFilter();
  });

  document.getElementById("ff-list").addEventListener("click", function (e) {
    var star = e.target.closest(".ff-star");
    if (star) { var name = star.getAttribute("data-name"); if (name) toggleFav(name); }
  });

  // 页面隐藏时暂停轮询(切走 tab 不再打接口)
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
    } else { startLoop(); }
  });

  updateFavCount();
  startLoop();
})();
