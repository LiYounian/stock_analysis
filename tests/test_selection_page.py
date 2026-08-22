"""选股结果页单测(5 策略全读全A view:① 自选股 →【综合选股】→ 策略0 → 策略1 → 策略2 → 策略3 → 策略4)。

锁死:
  - 策略0 读 view「策略0合议」top;策略1 读 view「趋势深跌反包」入选清单。
  - 策略2/3/4 改为读全A screener 预落盘 view(放量后缩量回踩 / 箱体形态 / 动量组合)的入选清单,
    **不再** web 端实时算(不调用 screen_s02.signal_at / pattern.detect_box / momentum 组合函数)。
  - 某 view 缺失 → 该策略 present=False 走「待运行」降级,页面不空、不抛。
  - 综合选股 = 各策略入选代码并集(后端给全并集 + 命中来源,前端按勾选实时重算)。
  - 区块顺序 + 5 策略标题 + 策略2/3 保留「待验证」标识。
data-independent(monkeypatch get_view + hermetic 不触网、不读盘)。
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


def _s02_view(codes=("000001",)):
    """全A view「放量后缩量回踩」(screen_s02 产出):入选清单 [{code, 明细}]。"""
    return {"as_of": "2026-08-08", "策略": "放量后缩量回踩(S02)",
            "扫描数": 5539, "有效样本": 5100, "跳过数(历史不足)": 439, "入选数": len(codes),
            "入选清单": [{"code": c, "明细": {}} for c in codes]}


def _box_view(codes=("000002",)):
    """全A view「箱体形态」:入选清单 [{code, 行业}]。"""
    return {"as_of": "2026-08-08", "策略": "箱体形态",
            "扫描数": 5539, "有效样本": 5000, "入选数": len(codes),
            "入选清单": [{"code": c, "行业": "芯片"} for c in codes]}


def _momentum_view(codes=("000003",), combos="动量组合"):
    """全A view「动量组合」:入选清单 [{code, 组合}](组合标注命中"动量组合"/"红利动量组合")。"""
    return {"as_of": "2026-08-08", "策略": "动量组合",
            "扫描数": 5539, "有效样本": 4800, "入选数": len(codes),
            "入选清单": [{"code": c, "组合": [combos]} for c in codes]}


def _dispatch(strategy0=None, s01=None, s02=None, box=None, momentum=None, semi=None):
    """按 view 名分派 6 个 view;未提供 → 抛 FileNotFoundError(触发该策略「待运行」降级)。"""
    mapping = {"策略0合议": strategy0, "趋势深跌反包": s01,
               "放量后缩量回踩": s02, "箱体形态": box, "动量组合": momentum,
               "半导体多因子": semi}

    def _fn(name, date="latest"):
        v = mapping.get(name)
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
    """综合选股:策略0∪策略1 去重,每票命中来源标注;策略2/3/4 view 缺 → 均 available=False。"""
    _patch(monkeypatch, {}, get_view=_dispatch(
        strategy0=_strategy0_view(("000001", "300311")), s01=_s01_view()))
    combined = da.selection_page()["combined"]
    rows = {r["code"]: r for r in combined["rows"]}
    # 000001 只在策略0;300311 在策略0∪策略1;603221 只在策略1
    assert rows["000001"]["sources"] == ["策略0"]
    assert set(rows["300311"]["sources"]) == {"策略0", "策略1"}
    assert rows["603221"]["sources"] == ["策略1"]
    strat = {s["key"]: s for s in combined["strategies"]}
    # 11 个策略勾选框都在(PR#15 增 策略7/8/9;策略10 反转低换手候选·前向观测中);key/label 对齐编号
    assert [s["key"] for s in combined["strategies"]] == [
        "策略0", "策略1", "策略2", "策略3", "策略4", "策略5", "策略6", "策略7", "策略8", "策略9", "策略10"]
    assert strat["策略2"]["label"] == "放量后缩量回踩"
    assert strat["策略3"]["label"] == "箱体形态"
    assert strat["策略4"]["label"] == "动量组合"
    assert strat["策略5"]["label"] == "自选池小市值"
    assert strat["策略6"]["label"] == "半导体多因子"
    assert strat["策略7"]["label"] == "最大范围选股"
    assert strat["策略8"]["label"] == "量价放量"
    assert strat["策略9"]["label"] == "最强选股"
    assert strat["策略10"]["label"] == "反转低换手(前向观测中)"
    # 策略10 view 缺(本测试未注入)→ present=False → available=False、codes=[]
    assert strat["策略10"]["available"] is False and strat["策略10"]["codes"] == []
    assert strat["策略0"]["available"] and strat["策略1"]["available"]
    # 策略2/3/4 view 缺 → present=False → available=False、codes=[]
    for k in ("策略2", "策略3", "策略4"):
        assert strat[k]["available"] is False and strat[k]["codes"] == []
    # 策略5/6 无自选池 records → present=False(本用例 recs 空)
    for k in ("策略5", "策略6"):
        assert strat[k]["available"] is False and strat[k]["codes"] == []
    # 全并集去重:000001, 300311, 603221
    assert set(rows.keys()) == {"000001", "300311", "603221"}


def test_combined_union_includes_view_strategies(monkeypatch):
    """综合选股并集含策略2/3/4 全A view 入选(各来源正确标注)。"""
    recs = {c: _rec(c) for c in ("000001", "000002", "000003")}
    _patch(monkeypatch, recs, get_view=_dispatch(
        strategy0=_strategy0_view(("000001",)), s01=_s01_view(),
        s02=_s02_view(("000001",)), box=_box_view(("000002",)),
        momentum=_momentum_view(("000003",))))
    combined = da.selection_page()["combined"]
    rows = {r["code"]: r for r in combined["rows"]}
    # 000001 同时命中策略0 + 策略2
    assert set(rows["000001"]["sources"]) == {"策略0", "策略2"}
    assert rows["000002"]["sources"] == ["策略3"]
    assert rows["000003"]["sources"] == ["策略4"]
    strat = {s["key"]: s for s in combined["strategies"]}
    assert strat["策略2"]["codes"] == ["000001"]
    assert strat["策略3"]["codes"] == ["000002"]
    assert strat["策略4"]["codes"] == ["000003"]


def test_combined_empty_when_no_strategies(monkeypatch):
    """5 view 均缺 → 并集 rows 空,strategies 仍在(available=False),不炸。"""
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
# 策略2/3/4:读全A view(不再 web 实时算)
# ————————————————————————————————————————————————
def test_strategy2_reads_view_not_realtime(monkeypatch):
    """策略2 = 放量后缩量回踩:读 view 入选清单,**不调用** screen_s02.signal_at 实时算。"""
    import tools.pipeline.screen_s02 as s02
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        return {"SELECT": True}
    monkeypatch.setattr(s02, "signal_at", _boom)

    recs = {"000001": _rec("000001")}
    _patch(monkeypatch, recs, get_view=_dispatch(s02=_s02_view(("000001",))))
    page = da.selection_page()
    s2 = page["strategy2"]
    assert s2["present"] is True and s2["picks"] == ["000001"]
    assert {r["code"] for r in s2["rows"]} == {"000001"}
    assert s2["扫描数"] == 5539 and s2["入选数"] == 1
    assert calls["n"] == 0                                  # 全程不实时算
    strat = {s["key"]: s for s in page["combined"]["strategies"]}
    assert strat["策略2"]["available"] is True and strat["策略2"]["codes"] == ["000001"]
    crows = {r["code"]: r for r in page["combined"]["rows"]}
    assert "策略2" in crows["000001"]["sources"]


def test_strategy2_missing_view_fallback(monkeypatch):
    """策略2 view 缺失 → present=False、picks/rows 空,不炸。"""
    _patch(monkeypatch, {}, get_view=_dispatch(s02=None))
    s2 = da.selection_page()["strategy2"]
    assert s2["present"] is False and s2["picks"] == [] and s2["rows"] == []


def test_strategy3_reads_view_not_realtime(monkeypatch):
    """策略3 = 箱体形态:读 view 入选清单,**不调用** pattern.detect_box 实时算。"""
    from tools.analysis.pattern_screener import pattern
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        return {"达标": True}
    monkeypatch.setattr(pattern, "detect_box", _boom)

    recs = {"000002": _rec("000002")}
    _patch(monkeypatch, recs, get_view=_dispatch(box=_box_view(("000002",))))
    page = da.selection_page()
    s3 = page["strategy3"]
    assert s3["present"] is True and s3["picks"] == ["000002"]
    assert {r["code"] for r in s3["rows"]} == {"000002"}
    assert calls["n"] == 0                                  # 全程不实时算
    strat = {s["key"]: s for s in page["combined"]["strategies"]}
    assert strat["策略3"]["available"] is True and strat["策略3"]["codes"] == ["000002"]


def test_strategy3_missing_view_fallback(monkeypatch):
    """策略3 view 缺失 → present=False,不炸。"""
    _patch(monkeypatch, {}, get_view=_dispatch(box=None))
    s3 = da.selection_page()["strategy3"]
    assert s3["present"] is False and s3["picks"] == [] and s3["rows"] == []


def test_strategy4_reads_view_not_realtime(monkeypatch):
    """策略4 = 动量组合:读 view 入选清单(含「组合」标注),**不调用** momentum 组合函数实时算。"""
    import tools.strategy.momentum as mm
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        return []
    monkeypatch.setattr(mm, "combo_momentum_screen", _boom)
    monkeypatch.setattr(mm, "combo_dividend_momentum_screen", _boom)

    recs = {"000003": _rec("000003")}
    _patch(monkeypatch, recs, get_view=_dispatch(momentum=_momentum_view(("000003",))))
    page = da.selection_page()
    s4 = page["strategy4"]
    assert s4["present"] is True and s4["picks"] == ["000003"]
    assert s4["rows"][0]["combos"] == ["动量组合"]           # 组合标注透传到展示
    assert calls["n"] == 0                                  # 全程不实时算
    strat = {s["key"]: s for s in page["combined"]["strategies"]}
    assert strat["策略4"]["label"] == "动量组合" and strat["策略4"]["available"] is True
    assert strat["策略4"]["codes"] == ["000003"]


def test_strategy4_missing_view_fallback(monkeypatch):
    """策略4 view 缺失 → present=False,不炸。"""
    _patch(monkeypatch, {}, get_view=_dispatch(momentum=None))
    s4 = da.selection_page()["strategy4"]
    assert s4["present"] is False and s4["picks"] == [] and s4["rows"] == []


# ————————————————————————————————————————————————
# 策略5(自选池小市值,web 层实时跑,不读 view)
# ————————————————————————————————————————————————
def _mkt_rec(code, mktcap_yi, pct_chg=0.5, sector="半导体"):
    """策略5 用最小 record:valuation.mktcap_yi + snapshot.pct_chg 即可。"""
    return {
        "meta": {"code": code, "name": "T" + code, "sector": sector, "industry": "芯片"},
        "valuation": {"mktcap_yi": mktcap_yi},
        "snapshot": {"close": 10.0, "pct_chg": pct_chg},
    }


def test_strategy5_runs_in_web_layer(monkeypatch):
    """策略5 web 层实时跑:自选池 records 直接调 strategy D,不依赖 view。"""
    recs = {
        "002001": _mkt_rec("002001", 30.0),
        "002002": _mkt_rec("002002", 20.0),      # 最小市值
        "002003": _mkt_rec("002003", 50.0),
        "300001": _mkt_rec("300001", 40.0),      # 创业板 D 不剥
    }
    _patch(monkeypatch, recs)
    page = da.selection_page()
    s5 = page["strategy5"]
    assert s5["present"] is True and s5["扫描数"] == 4
    assert s5["picks"] == ["002002", "002001", "300001"]          # 市值升序 top_k=3
    assert [r["mktcap_yi"] for r in s5["rows"]] == [20.0, 30.0, 40.0]
    strat = {s["key"]: s for s in page["combined"]["strategies"]}
    assert strat["策略5"]["label"] == "自选池小市值" and strat["策略5"]["available"] is True
    assert set(strat["策略5"]["codes"]) == {"002002", "002001", "300001"}


def test_strategy5_still_filters_limit_up(monkeypatch):
    """策略5 web 层仍剥触涨跌停(与策略D 一致)。"""
    recs = {
        "002001": _mkt_rec("002001", 20.0, pct_chg=9.9),   # 涨停 → 剔
        "002002": _mkt_rec("002002", 30.0),
        "002003": _mkt_rec("002003", 40.0),
    }
    _patch(monkeypatch, recs)
    assert da.selection_page()["strategy5"]["picks"] == ["002002", "002003"]


def test_strategy5_empty_pool(monkeypatch):
    """自选池无 records → present=False,不炸,combined 里 available=False。"""
    _patch(monkeypatch, {})
    s5 = da.selection_page()["strategy5"]
    assert s5["present"] is False and s5["rows"] == []


# ————————————————————————————————————————————————
# 策略6(半导体多因子,限申万二级 801081 半导体池,web 层实时跑)
# ————————————————————————————————————————————————
def _sf_rec(code, rd_pct, rev_yoy_pct, 营收, mktcap_yi, pct_chg=0.5):
    """策略6 用最小 record:financial.derived + 利润表摘要 + valuation + snapshot。"""
    return {
        "meta": {"code": code, "name": "T" + code, "sector": "半导体", "industry": "电子"},
        "financial": {"derived": {"研发费用率": rd_pct, "营收增速": rev_yoy_pct},
                      "利润表摘要": {"营业总收入": 营收}},
        "valuation": {"mktcap_yi": mktcap_yi},
        "snapshot": {"close": 10.0, "pct_chg": pct_chg},
    }


def _patch_semi_universe(monkeypatch, universe: set[str]):
    """monkeypatch tools.strategy.semi_factor._load_universe → 自造半导体池。"""
    from tools.strategy import semi_factor as _sf
    monkeypatch.setattr(_sf, "_load_universe", lambda: universe)


def test_strategy6_runs_in_web_layer(monkeypatch):
    """策略6 web 层实时跑:限半导体池 + 3 因子加权。"""
    _patch_semi_universe(monkeypatch, {"A", "B", "C"})
    recs = {
        "A": _sf_rec("A", 15.0, 45.0, 5e9, 569.0),                # 高 rd/rev → 排头
        "B": _sf_rec("B", 3.3, 190.0, 1.9e10, 10113.29),
        "C": _sf_rec("C", 1.4, 105.0, 1e10, 5571.0),              # 低 rd/rev → 垫底
    }
    _patch(monkeypatch, recs)
    page = da.selection_page()
    s6 = page["strategy6"]
    assert s6["present"] is True and s6["universe_size"] == 3 and s6["样本数"] == 3
    assert s6["picks"][0] == "A" and s6["picks"][-1] == "C"       # 高研发排头,低研发垫底
    r = s6["rows"][0]
    for k in ("综合分", "rd_rev", "rd_mcap", "rev_yoy"):
        assert r[k] is not None
    strat = {s["key"]: s for s in page["combined"]["strategies"]}
    assert strat["策略6"]["label"] == "半导体多因子" and strat["策略6"]["available"] is True
    assert strat["策略6"]["codes"][0] == "A"


def test_strategy6_universe_filters_non_semi(monkeypatch):
    """半导体池外的票即使因子完美也剔。"""
    _patch_semi_universe(monkeypatch, {"A", "B"})                  # 只 A/B 在池
    recs = {
        "A": _sf_rec("A", 15.0, 45.0, 5e9, 569.0),
        "B": _sf_rec("B", 8.0, 30.0, 1e10, 500.0),
        "C": _sf_rec("C", 30.0, 200.0, 1e10, 500.0),                # 池外 → 剔
    }
    _patch(monkeypatch, recs)
    s6 = da.selection_page()["strategy6"]
    assert set(s6["picks"]) == {"A", "B"}


def test_strategy6_missing_financial_derived(monkeypatch):
    """financial.derived 缺失 → 该票剔;剩余 <2 样本无法标准化 → 空 picks + note。"""
    _patch_semi_universe(monkeypatch, {"NO_FIN", "OK"})
    recs = {
        "NO_FIN": {"meta": {"code": "NO_FIN"}, "financial": None,
                   "valuation": {"mktcap_yi": 500.0},
                   "snapshot": {"pct_chg": 0.5}},
        "OK": _sf_rec("OK", 5.0, 30.0, 1e10, 500.0),
    }
    _patch(monkeypatch, recs)
    s6 = da.selection_page()["strategy6"]
    assert s6["picks"] == [] and s6["present"] is True
    assert s6["note"]                                              # 有降级说明


def test_strategy6_empty_records(monkeypatch):
    """无 records → present=False,不炸,combined 里 available=False。"""
    _patch(monkeypatch, {})
    s6 = da.selection_page()["strategy6"]
    assert s6["present"] is False and s6["rows"] == []


def _semi_view(picks=("688981", "603986")):
    """全A view「半导体多因子」(screen_semi_factor 产出):入选清单 [{code, 行业, 组合, 明细}]。"""
    items = []
    for i, c in enumerate(picks):
        items.append({
            "code": c, "行业": "半导体(申万二级 801081)",
            "组合": ["半导体多因子"],
            "明细": {"综合分": round(1.0 - i * 0.3, 4),
                    "rd_rev": 0.1 + i * 0.02, "rd_mcap": 0.005 - i * 0.001,
                    "rev_yoy": 0.5 - i * 0.1,
                    "rd_rev_z": 1.0 - i, "rd_mcap_z": 0.5, "rev_yoy_z": 0.2},
        })
    return {"as_of": "2026-08-19", "策略": "半导体多因子",
            "扫描数": 178, "universe_size": 178, "有效样本": 100,
            "跳过数(缺数据)": 78, "入选数": len(picks), "top_k": 8,
            "权重": {"rd_rev": 0.6, "rd_mcap": 0.2, "rev_yoy": 0.2},
            "入选清单": items}


def test_strategy6_reads_view_first(monkeypatch):
    """view 存在时优先读 view(不做实时算),source=view。"""
    _patch(monkeypatch, {}, get_view=_dispatch(semi=_semi_view()))
    page = da.selection_page()
    s6 = page["strategy6"]
    assert s6["present"] is True and s6.get("source") == "view"
    assert s6["picks"] == ["688981", "603986"]
    assert s6["universe_size"] == 178 and s6["样本数"] == 100
    r = s6["rows"][0]
    assert r["综合分"] == 1.0 and r["rd_rev"] == 0.1        # view 明细透传
    strat = {s["key"]: s for s in page["combined"]["strategies"]}
    assert strat["策略6"]["available"] is True
    assert strat["策略6"]["codes"] == ["688981", "603986"]


def test_view_picks_top_n_cap(monkeypatch):
    """全A view 入选可达几十只 → 展示 rows 与 picks 都截到 cap(30),入选数仍报 view 真值。"""
    codes = [f"{600000 + i:06d}" for i in range(45)]
    _patch(monkeypatch, {}, get_view=_dispatch(momentum=_momentum_view(tuple(codes))))
    s4 = da.selection_page()["strategy4"]
    assert len(s4["rows"]) == 30 and len(s4["picks"]) == 30   # 展示与并集口径一致(cap=30)
    assert s4["入选数"] == 45                                  # 规模字段报 view 真实入选数


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
    """全空(无记录 + 无 view)→ 5 策略全兜底 present=False,不炸。"""
    _patch(monkeypatch, {})
    page = da.selection_page()
    assert page["total"] == 0 and page["rows"] == []
    for k in ("strategy0", "strategy1", "strategy2", "strategy3", "strategy4"):
        assert page[k]["present"] is False
    assert page["combined"]["rows"] == []


# ————————————————————————————————————————————————
# 路由渲染:区块顺序 + 5 策略标题 + 待验证标识
# ————————————————————————————————————————————————
def test_route_renders_order_and_no_removed_blocks(monkeypatch):
    """锁 5 策略新结构:区块顺序 ① 自选股 → 综合选股 → 策略0 → 策略1 → 策略2 → 策略3 → 策略4;
    无已删区块/旧字样;策略2/3 带「待验证」;策略4 = 动量组合。"""
    _patch(monkeypatch, {"000001": _rec("000001", bull=True)}, pool={"000001"},
           get_view=_dispatch(strategy0=_strategy0_view(), s01=_s01_view(),
                              s02=_s02_view(("000001",)), box=_box_view(("000001",)),
                              momentum=_momentum_view(("000001",))))
    r = client.get("/selection")
    assert r.status_code == 200
    t = r.text
    # 区块顺序:① 自选股 → 综合选股 → 策略0 → 策略1 → 策略2 → 策略3 → 策略4
    i_pool = t.find("<!-- 区块①")
    i_comb = t.find("<!-- 综合选股")
    i_s0 = t.find("<!-- 策略0")
    i_s1 = t.find("<!-- 策略1")
    i_s2 = t.find("<!-- 策略2")
    i_s3 = t.find("<!-- 策略3")
    i_s4 = t.find("<!-- 策略4")
    assert -1 < i_pool < i_comb < i_s0 < i_s1 < i_s2 < i_s3 < i_s4
    # 移除的区块 / 旧字样不再出现
    assert "每日筛选" not in t and "今日精选" not in t and "S01" not in t
    # 5 策略标题(策略1 命名已改为「筛选低吸股票」,不再是 S01)
    assert "策略0 · 多专家合议(全A)" in t
    assert "策略1 · 筛选低吸股票" in t
    assert "策略2 · 放量后缩量回踩" in t
    assert "策略3 · 箱体形态" in t
    assert "策略4 · 动量组合" in t
    # 策略2/3 带「待验证」中性标识(策略1 也有,至少 3 处)
    assert t.count("待验证") >= 3
    assert "/static/council.js" in t
    assert 'id="strat0Body"' in t and 'id="combinedBody"' in t


def test_route_missing_views_show_hints(monkeypatch):
    """5 view 缺失 → 各策略显示「待运行」提示,页面 200 不空。"""
    _patch(monkeypatch, {}, get_view=_dispatch())
    r = client.get("/selection")
    assert r.status_code == 200
    t = r.text
    assert "策略0 待运行" in t and "策略1 待运行" in t
    assert "策略2 待运行" in t and "策略3 待运行" in t and "策略4 待运行" in t


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
