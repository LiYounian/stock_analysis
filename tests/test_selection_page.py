"""选股结果页单测(重构后:① 自选股 →【综合选股】→ 策略0(合议全A)→ 策略1(趋势深跌反包))。

锁死:
  - 区块② 每日筛选(形态选股)已移除;页面不再出现「每日筛选」「今日精选」「S01」字样。
  - 策略0 读 view「策略0合议」top;缺失走兜底(present=False)。
  - 综合选股 = 各策略入选代码并集(后端给全并集 + 命中来源,前端按勾选实时重算)。
  - 策略1 读 view「趋势深跌反包」;区块顺序;名称回退。
data-independent(monkeypatch)。
"""
from fastapi.testclient import TestClient

from tools.analysis import council
from web import data_access as da
from web.app import app

client = TestClient(app)


def _rec(code, bull=True):
    if bull:
        sig = {"trend": {"评级": "偏多", "得分": 80, "依据": ["多头"]},
               "ob_os": {"verdict": "超卖", "resonance": 3},
               "reversal": {"拐点标签": "反弹启动", "拐点评分": 80}}
    else:
        sig = {"trend": {"评级": "偏空", "得分": -80, "依据": ["空头"]},
               "ob_os": {"verdict": "超买", "resonance": 3},
               "reversal": {"拐点标签": "无", "拐点评分": 0}}
    rec = {"schema_version": "1.0",
           "meta": {"code": code, "name": "T" + code, "sector": "半导体", "industry": "芯片", "as_of": "2026-08-08"},
           "snapshot": None, "valuation": None, "fundamental": None, "signals": sig,
           "prediction": None, "sentiment": None, "fundflow": None, "events": [],
           "timeseries_refs": {}, "provenance": {}}
    rec["council"] = council.build_council_block(rec)
    return rec


def _strategy0_view(codes=("000001", "000002")):
    """一份「策略0合议」view(每项带 council 信封),字段口径同 screen_council 产出。"""
    top = []
    for i, c in enumerate(codes):
        rec = _rec(c, bull=(i == 0))
        top.append({"code": c, "行业": "芯片", "综合方向": rec["council"]["default"]["综合方向"],
                    "综合分": rec["council"]["default"]["综合分"], "council": rec["council"]})
    return {"as_of": "2026-08-08", "策略": "策略0 · 多专家合议(全A)",
            "扫描数": 5206, "有效": 5191, "top_n": len(top), "top": top}


def _s01_view():
    return {
        "as_of": "2026-08-08", "策略": "趋势深跌反包(S01)",
        "扫描数": 5539, "有效样本": 5124, "跳过数(历史不足)": 415, "入选数": 2,
        "入选清单": [
            {"code": "300311", "明细": {
                "MA": {"5": 6.16, "10": 5.649, "20": 5.414, "30": 5.3593, "60": 5.3325, "200": 5.1149},
                "close": 6.69, "H52": 7.16, "近强_涨/跌": [9, 1], "当日跌幅": -0.1257, "收阳": True}},
            {"code": "603221", "明细": {
                "MA": {"5": 22.254, "10": 18.191, "20": 14.6835, "30": 13.3203, "60": 12.8087, "200": 12.5568},
                "close": 24.82, "H52": 24.79, "近强_涨/跌": [1, 0], "当日跌幅": -0.0891, "收阳": True}},
        ],
    }


def _dispatch(strategy0=None, s01=None):
    """按 view 名分派:策略0合议 / 趋势深跌反包;None → 抛 FileNotFoundError。"""
    def _fn(name, date="latest"):
        v = strategy0 if name == "策略0合议" else (s01 if name == "趋势深跌反包" else None)
        if v is None:
            raise FileNotFoundError(name)
        return v
    return _fn


def _patch(monkeypatch, recs, pool=None, get_view=None):
    monkeypatch.setattr(da, "_load_all", lambda date="latest": recs)
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    monkeypatch.setattr(da, "available_dates", lambda: ["2026-08-08"])
    monkeypatch.setattr(da, "_pool_codes", lambda: set(recs.keys()) if pool is None else pool)
    monkeypatch.setattr(da, "_code_name_map", lambda: {})
    monkeypatch.setattr(da.store, "get_view", get_view or _dispatch())


# ————————————————————————————————————————————————
# 策略0(全A合议)区块
# ————————————————————————————————————————————————
def test_strategy0_reads_view_ranked(monkeypatch):
    """策略0 读 view「策略0合议」top:present + 规模字段 + 每行带 experts(供前端重排)。"""
    _patch(monkeypatch, {}, get_view=_dispatch(strategy0=_strategy0_view()))
    s0 = da.selection_page()["strategy0"]
    assert s0["present"] is True and s0["扫描数"] == 5206 and s0["有效"] == 5191
    assert [r["code"] for r in s0["rows"]] == ["000001", "000002"]
    assert s0["rows"][0]["experts"]                         # 专家信封供前端勾选重排
    assert s0["rows"][0]["council_dir"] in ("看多", "看空", "中性")
    assert s0["rows"][0]["industry"] == "芯片"
    assert s0["config"]                                     # 带合议 config(前端合成口径)


def test_strategy0_missing_view_fallback(monkeypatch):
    """策略0 view 缺失 → present=False、rows 空,不空页不抛。"""
    _patch(monkeypatch, {}, get_view=_dispatch(strategy0=None))
    s0 = da.selection_page()["strategy0"]
    assert s0["present"] is False and s0["rows"] == []


def test_strategy0_name_fallback(monkeypatch):
    """策略0 名称回退:无中心记录 → code_name.json → code。"""
    monkeypatch.setattr(da, "_code_name_map", lambda: {"000001": "平安银行"})
    monkeypatch.setattr(da, "_load_all", lambda date="latest": {})
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    monkeypatch.setattr(da, "_pool_codes", lambda: set())
    monkeypatch.setattr(da.store, "get_view", _dispatch(strategy0=_strategy0_view(("000001", "999999"))))
    rows = {r["code"]: r for r in da.selection_page()["strategy0"]["rows"]}
    assert rows["000001"]["name"] == "平安银行"             # code_name 命中
    assert rows["999999"]["name"] == "999999"              # 都无 → code


# ————————————————————————————————————————————————
# 综合选股(并集)
# ————————————————————————————————————————————————
def test_combined_union_and_sources(monkeypatch):
    """综合选股:策略0∪策略1 去重,每票命中来源标注;策略2 available=False。"""
    _patch(monkeypatch, {}, get_view=_dispatch(
        strategy0=_strategy0_view(("000001", "300311")), s01=_s01_view()))
    combined = da.selection_page()["combined"]
    rows = {r["code"]: r for r in combined["rows"]}
    # 000001 只在策略0;300311 在策略0∪策略1;603221 只在策略1
    assert rows["000001"]["sources"] == ["策略0"]
    assert set(rows["300311"]["sources"]) == {"策略0", "策略1"}
    assert rows["603221"]["sources"] == ["策略1"]
    strat = {s["key"]: s for s in combined["strategies"]}
    assert strat["策略0"]["available"] and strat["策略1"]["available"]
    assert strat["策略2"]["available"] is False and strat["策略2"]["codes"] == []
    # 全并集去重:000001, 300311, 603221
    assert set(rows.keys()) == {"000001", "300311", "603221"}


def test_combined_empty_when_no_strategies(monkeypatch):
    """两策略 view 均缺 → 并集 rows 空,strategies 仍在(available=False),不炸。"""
    _patch(monkeypatch, {}, get_view=_dispatch())
    combined = da.selection_page()["combined"]
    assert combined["rows"] == []
    assert all(not s["available"] for s in combined["strategies"])


# ————————————————————————————————————————————————
# 策略1(趋势深跌反包,原 S01)
# ————————————————————————————————————————————————
def test_strategy1_parses_view(monkeypatch):
    """策略1 读 view「趋势深跌反包」:present + 扁平化(跌幅%/突破前高/均线多头/收阳)。"""
    _patch(monkeypatch, {"300311": _rec("300311", bull=True)},
           pool=set(), get_view=_dispatch(s01=_s01_view()))
    s1 = da.selection_page()["strategy1"]
    assert s1["present"] is True and s1["入选数"] == 2
    byc = {r["code"]: r for r in s1["rows"]}
    assert byc["300311"]["name"] == "T300311"              # 有记录取 meta 名
    assert byc["300311"]["当日跌幅%"] == -12.57
    assert byc["603221"]["突破前高"] is True                # close 24.82 > H52 24.79


def test_strategy1_missing_view_fallback(monkeypatch):
    """策略1 view 缺失 → present=False,不空页不抛。"""
    _patch(monkeypatch, {}, get_view=_dispatch(s01=None))
    s1 = da.selection_page()["strategy1"]
    assert s1["present"] is False and s1["rows"] == []


# ————————————————————————————————————————————————
# 区块① 自选股 + 全页兜底
# ————————————————————————————————————————————————
def test_block1_filters_to_pool(monkeypatch):
    """区块①「自选股」只展示自选池成员;total 为全量记录口径。"""
    recs = {"000001": _rec("000001", bull=True), "000002": _rec("000002", bull=True)}
    _patch(monkeypatch, recs, pool={"000001"})
    page = da.selection_page()
    assert {r["code"] for r in page["rows"]} == {"000001"}
    assert page["total"] == 2


def test_block1_ranks_by_council_desc(monkeypatch):
    """区块① 按合议综合分降序;带专家信封 + config。"""
    recs = {"000001": _rec("000001", bull=True), "000002": _rec("000002", bull=False)}
    _patch(monkeypatch, recs)
    page = da.selection_page()
    assert page["rows"][0]["code"] == "000001"
    assert page["rows"][0]["council_score"] >= page["rows"][1]["council_score"]
    assert page["rows"][0]["experts"] and page["config"]


def test_empty_day_no_crash(monkeypatch):
    """全空(无记录 + 无 view)→ 各区块兜底,不炸。"""
    _patch(monkeypatch, {})
    page = da.selection_page()
    assert page["total"] == 0 and page["rows"] == []
    assert page["strategy0"]["present"] is False and page["strategy1"]["present"] is False
    assert page["combined"]["rows"] == []


# ————————————————————————————————————————————————
# 路由渲染:区块顺序 + 无「每日筛选/S01/今日精选」+ 策略1 命名
# ————————————————————————————————————————————————
def test_route_renders_order_and_no_removed_blocks(monkeypatch):
    _patch(monkeypatch, {"000001": _rec("000001", bull=True)}, pool={"000001"},
           get_view=_dispatch(strategy0=_strategy0_view(), s01=_s01_view()))
    r = client.get("/selection")
    assert r.status_code == 200
    t = r.text
    # 区块顺序:① 自选股 → 综合选股 → 策略0 → 策略1
    i_pool = t.find("<!-- 区块①")
    i_comb = t.find("<!-- 综合选股")
    i_s0 = t.find("<!-- 策略0")
    i_s1 = t.find("<!-- 策略1")
    assert -1 < i_pool < i_comb < i_s0 < i_s1
    # 移除的区块 / 旧字样不再出现
    assert "每日筛选" not in t and "今日精选" not in t and "S01" not in t
    # 策略1 命名(不再是 S01)
    assert "策略1 · 趋势深跌反包" in t and "策略0 · 多专家合议(全A)" in t
    assert "/static/council.js" in t
    assert 'id="strat0Body"' in t and 'id="combinedBody"' in t


def test_route_strategy0_missing_shows_hint(monkeypatch):
    """策略0 view 缺失 → 显示「策略0 待运行」,不空页。"""
    _patch(monkeypatch, {}, get_view=_dispatch(strategy0=None, s01=None))
    r = client.get("/selection")
    assert r.status_code == 200
    assert "策略0 待运行" in r.text and "策略1 待运行" in r.text


# ————————————————————————————————————————————————
# 止盈止损防空 + 名称回退(沿用旧口径,防回归)
# ————————————————————————————————————————————————
def _pred_full():
    return {"现价": 12.34,
            "持有期建议": {"5日": {"止损位": 11.50, "最大亏损%": 6.8,
                                  "止盈位": 13.60, "目标盈利%": 10.2, "风险收益比": 1.5}},
            "情景预测": {"5日": {"上涨概率%": 58.0, "样本数": 120}},
            "买卖倾向": {"结论": "偏买入", "依据": ["多头"]}}


def test_stops_view_guards_missing_and_error():
    assert da.stops_view({})["止损位"] is None
    err = da.stops_view({"prediction": {"error": "数据不足", "n": 5}})
    assert all(v is None for v in err.values())
    ok = da.stops_view({"prediction": _pred_full()})
    assert ok["现价"] == 12.34 and ok["止损位"] == 11.50 and ok["上涨概率%"] == 58.0


def test_selection_row_carries_stops(monkeypatch):
    r = _rec("000001", bull=True); r["prediction"] = _pred_full()
    _patch(monkeypatch, {"000001": r}, pool={"000001"})
    page = da.selection_page()
    assert page["rows"][0]["stops"]["止损位"] == 11.50
    resp = client.get("/selection")
    assert resp.status_code == 200 and "5日涨概率" in resp.text and "11.5" in resp.text


def test_name_fallback_three_levels(monkeypatch):
    monkeypatch.setattr(da, "_code_name_map", lambda: {"600519": "贵州茅台"})
    recs = {"000001": {"meta": {"code": "000001", "name": "平安银行"}}}
    assert da._name(recs, "000001") == "平安银行"
    assert da._name(recs, "600519") == "贵州茅台"
    assert da._name(recs, "999999") == "999999"
