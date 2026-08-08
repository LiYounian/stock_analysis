"""两阶段流水线单测:先便宜筛(只 K线)、再只对候选做贵活(护栏/新闻/LLM)。

锁语义(全A 可扩展前提):
  · screen_pattern:护栏数据(基本面/公告)只对便宜门候选采,不对全A提前全采。
  · run.collect_values_missing:skip-if-cached,只补缺失子集,不重采已缓存。
  · run.run_two_stage:screen(便宜筛)排在 sentiment(LLM)之前;新闻/LLM 只对(达标∪自选)候选。
"""
import pandas as pd

from tools import run
from tools.collectors import board, index, market
from tools.pipeline import screen_pattern as sp
from tools.store import repo as store


def _breakout_df(last=108.0):
    base = [100 + (2 if i % 2 else -2) for i in range(20)]
    closes = base + [last]
    vols = [1000.0] * 20 + [2500.0]
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=21, freq="D"),
        "open": closes, "high": [c * 1.005 for c in closes],
        "low": [c * 0.995 for c in closes], "close": closes, "volume": vols})


def _flat_df():
    flat = [100 + (0.5 if i % 2 else -0.5) for i in range(21)]
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=21, freq="D"),
        "open": flat, "high": flat, "low": flat, "close": flat,
        "volume": [1000.0] * 21})


def _bench_df():
    return pd.DataFrame({"date": pd.date_range("2024-01-01", periods=21, freq="D"),
                         "close": [100.0] * 21})


# ———————————— OPT-1:护栏数据只对候选采 ————————————
def test_guardrail_only_fetched_for_candidates(monkeypatch, tmp_path):
    """阶段②纪律:便宜门(形态+RS+量能)淘汰的票不采基本面/公告;只对候选采护栏。"""
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(index, "load_index", lambda code: _bench_df())
    monkeypatch.setattr(board, "load_membership", lambda: {})       # 单层
    klines = {"HIT": _breakout_df(), "MISS": _flat_df()}
    monkeypatch.setattr(market, "load_kline", lambda code: klines[code])

    seen: list[str] = []

    def spy(code, fetch):
        seen.append(code)
        return {"pe_percentile": None, "净利增速": 10.0, "ann_titles": [], "有数据": True}

    monkeypatch.setattr(sp, "_guardrail_inputs", spy)

    view = sp.run_pattern_screen(["HIT", "MISS"], as_of="2024-06-01", fetch=False)
    assert seen == ["HIT"]                       # 平盘票(便宜门淘汰)未采护栏
    assert view["候选数"] == 1
    assert view["护栏覆盖"] == "1/1"
    assert [x["code"] for x in view["达标清单"]] == ["HIT"]
    assert view["有效样本"] == 2                  # 两票都判过(有效样本不受两阶段影响)


def test_cheap_gate_only_uses_kline():
    """便宜门只用 K线判 形态+RS+量能:突破+放量+RS 正→候选;RS 负→淘汰。不碰基本面/公告。"""
    from tools.config.strategy import THRESHOLDS
    cfg = {**THRESHOLDS["形态选股"], "RS": {**THRESHOLDS["形态选股"]["RS"], "启用板块层": False}}
    ok, pat = sp._cheap_gate(_breakout_df(), rs_stock_vs_board=0.08,
                             rs_board_vs_hs300=None, cfg=cfg)
    assert ok is True and pat["达标"] is True
    ok2, _ = sp._cheap_gate(_breakout_df(), rs_stock_vs_board=-0.08,
                            rs_board_vs_hs300=None, cfg=cfg)
    assert ok2 is False                          # RS 为负 → 便宜门淘汰


# ———————————— OPT-2:collect_values_missing skip-if-cached ————————————
def test_collect_values_missing_skips_cached(monkeypatch):
    """只补缺失:已缓存的票/源不重采,只对缺的子集调 fetch。"""
    cached_k = {"AAA"}                            # AAA 有 K线缓存,BBB 缺

    def load_k(c):
        if c in cached_k:
            return "df"
        raise FileNotFoundError(c)

    monkeypatch.setattr(run.market, "load_kline", load_k)
    monkeypatch.setattr(run.fd, "load_fundamental", lambda c: {})      # 全已缓存
    monkeypatch.setattr(run.an, "load_announcements", lambda c: [])    # 全已缓存
    monkeypatch.setattr(run.ff, "load_fundflow", lambda c: "df")       # 全已缓存

    fetched: dict[str, list] = {}
    monkeypatch.setattr(run.market, "fetch_kline", lambda codes: fetched.update(k=list(codes)))
    monkeypatch.setattr(run.fd, "fetch_fundamental", lambda codes: fetched.update(f=list(codes)))
    monkeypatch.setattr(run.an, "fetch_announcements", lambda codes: fetched.update(a=list(codes)))
    monkeypatch.setattr(run.ff, "fetch_fundflow", lambda codes: fetched.update(ff=list(codes)))

    run.collect_values_missing(["AAA", "BBB"])
    assert fetched.get("k") == ["BBB"]           # 只补缺 K线的 BBB
    assert "f" not in fetched and "a" not in fetched and "ff" not in fetched  # 已缓存→不采


# ———————————— OPT-2:run_two_stage 顺序 + 双子集(LLM 子集 vs 合议集)————————————
def _stub_pipeline(monkeypatch, calls, near=None):
    """stub 掉阶段②所有重活为记录调用;screen 返回达标 QUAL1 + 可选接近达标。"""
    from tools.pipeline import screen_pattern

    def fake_screen(codes, as_of, fetch=True):
        calls.append(("screen", list(codes)))
        return {"达标清单": [{"code": "QUAL1"}], "有效样本": len(codes),
                "候选数": 1, "达标数": 1, "接近达标": near or {}}

    monkeypatch.setattr(screen_pattern, "run_pattern_screen", fake_screen)
    monkeypatch.setattr(run.stock_pool, "get_codes", lambda: ["WATCH1", "WATCH2"])
    monkeypatch.setattr(run, "_enrich_near_miss", lambda as_of: 0)

    def rec(name):
        def _f(*a, **k):
            calls.append((name, list(a[0]) if a else None))
        return _f

    for fn in ("collect_values_missing", "collect_message", "collect_market_context",
               "run_sentiment", "run_serialize", "run_events", "run_factor",
               "run_council", "run_panel", "run_screen"):
        monkeypatch.setattr(run, fn, rec(fn))


def test_run_two_stage_llm_subset_vs_analysis_set(monkeypatch):
    """便宜筛先于 LLM;新闻/LLM 只对达标∪自选;组装/合议对 +接近达标(接近达标不进 LLM)。"""
    calls: list[tuple] = []
    near = {"电子": [{"code": "NEAR1", "行业": "电子", "合议分": None}]}
    _stub_pipeline(monkeypatch, calls, near=near)

    out = run.run_two_stage(["A", "B", "C"], "2026-08-08")

    names = [c[0] for c in calls]
    assert names.index("screen") < names.index("run_sentiment")       # 便宜筛先于 LLM
    assert names.index("collect_message") < names.index("run_sentiment")
    assert names.index("run_sentiment") < names.index("run_council")
    # 两个子集
    assert out["llm_subset"] == ["QUAL1", "WATCH1", "WATCH2"]           # 达标∪自选
    assert out["analysis_set"] == ["QUAL1", "WATCH1", "WATCH2", "NEAR1"]  # +接近达标
    # 新闻/LLM 只对 LLM 子集(NEAR1 不在)
    assert next(arg for n, arg in calls if n == "run_sentiment") == ["QUAL1", "WATCH1", "WATCH2"]
    assert next(arg for n, arg in calls if n == "collect_message") == ["QUAL1", "WATCH1", "WATCH2"]
    # 组装/合议对合议集(含 NEAR1)
    assert next(arg for n, arg in calls if n == "run_council") == ["QUAL1", "WATCH1", "WATCH2", "NEAR1"]
    assert next(arg for n, arg in calls if n == "run_serialize") == ["QUAL1", "WATCH1", "WATCH2", "NEAR1"]


def test_run_two_stage_dedups_qualified_in_watchlist(monkeypatch):
    """达标票已在自选池时,子集去重不重复。"""
    from tools.pipeline import screen_pattern
    monkeypatch.setattr(screen_pattern, "run_pattern_screen",
                        lambda codes, as_of, fetch=True: {
                            "达标清单": [{"code": "WATCH1"}, {"code": "QUAL2"}],
                            "有效样本": len(codes), "候选数": 2, "达标数": 2, "接近达标": {}})
    monkeypatch.setattr(run.stock_pool, "get_codes", lambda: ["WATCH1", "WATCH2"])
    monkeypatch.setattr(run, "_enrich_near_miss", lambda as_of: 0)
    for fn in ("collect_values_missing", "collect_message", "collect_market_context",
               "run_sentiment", "run_serialize", "run_events", "run_factor",
               "run_council", "run_panel", "run_screen"):
        monkeypatch.setattr(run, fn, lambda *a, **k: None)

    out = run.run_two_stage(["X"], "2026-08-08")
    assert out["llm_subset"] == ["WATCH1", "QUAL2", "WATCH2"]   # WATCH1 只出现一次


# ———————————— Part B:接近达标(达标0降级数据)————————————
def test_pattern_gap_box_awaiting_breakout():
    """箱体窄幅已成但未突破 → 形态接近,给差距说明。"""
    gap = sp._pattern_gap("箱体", {"窄幅": True, "突破": False, "放量": False})
    assert gap is not None and "待向上突破箱顶" in gap[0]
    gap2 = sp._pattern_gap("箱体", {"窄幅": True, "突破": True, "放量": False})
    assert gap2 is not None and "待放量确认" in gap2[0] and gap2[1] > gap[1]  # 越接近突破分越高
    assert sp._pattern_gap("箱体", {"窄幅": False}) is None                    # 结构未成→非接近


def test_near_miss_form_shape_without_kline_gate(monkeypatch):
    """非候选票(便宜门淘汰)但形态结构已成 → 接近达标(不需基本面/公告)。"""
    pat = {"达标": False, "命中形态": [],
           "明细": {"箱体": {"达标": False, "特征": {"窄幅": True, "突破": False}},
                    "楔形": {"达标": False, "特征": {}},
                    "杯柄": {"达标": False, "特征": {}},
                    "旗形": {"达标": False, "特征": {}}}}
    nm = sp._near_miss("X", {"X": "C39计算机、通信和其他电子设备制造业"}, pat, None)
    assert nm is not None
    assert nm["最接近形态"] == ["箱体"] and nm["合议分"] is None
    assert nm["行业"] == "电子" and "待向上突破箱顶" in nm["差距说明"]


def test_near_miss_qualify_gate_positive_confirm(monkeypatch):
    """候选票形态+突破+RS 已成但正向确认未过 → 达标接近(高接近度)。"""
    pat = {"达标": True, "命中形态": ["箱体"], "明细": {}}
    cand = {"达标": False, "各项": {"形态": True, "RS": True, "量能": True,
                                    "护栏": True, "正向确认": False}}
    nm = sp._near_miss("Y", {}, pat, cand)
    assert nm is not None and "待基本面或事件正向确认" in nm["差距说明"]
    assert nm["最接近形态"] == ["箱体"]


def test_group_near_miss_top3_per_sector():
    """接近达标按板块 top3,合议分 None 时按接近度降序。"""
    near = [{"code": f"E{i}", "行业": "电子", "合议分": None, "_接近度": i} for i in range(5)]
    near += [{"code": "B0", "行业": "银行", "合议分": None, "_接近度": 2}]
    g = sp._group_near_miss(near)
    assert len(g["电子"]) == 3                                     # top3
    assert [x["code"] for x in g["电子"]] == ["E4", "E3", "E2"]    # 接近度降序
    assert all("_接近度" not in x for x in g["电子"])              # 内部排序键已剔除
    assert len(g["银行"]) == 1


def test_view_has_near_miss_when_zero_qualified(monkeypatch, tmp_path):
    """达标0 时 view「接近达标」仍有内容(区块②不空页)。"""
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(index, "load_index", lambda code: _bench_df())
    monkeypatch.setattr(board, "load_membership", lambda: {"BOX": "C39计算机、通信和其他电子设备制造业"})
    # 窄幅箱体但末根不放量、不突破 → 形态接近但不达标
    flat_box = _flat_df()
    monkeypatch.setattr(market, "load_kline", lambda code: flat_box)
    view = sp.run_pattern_screen(["BOX"], as_of="2024-06-01", fetch=False)
    assert view["达标数"] == 0
    assert "接近达标" in view and isinstance(view["接近达标"], dict)
    assert "接近达标数" in view


def test_enrich_near_miss_fills_score_and_resorts(monkeypatch):
    """council 后回填合议分并按合议分重排 top3。"""
    view = {"接近达标": {"电子": [
        {"code": "A", "行业": "电子", "合议分": None},
        {"code": "B", "行业": "电子", "合议分": None},
        {"code": "C", "行业": "电子", "合议分": None}]}}
    monkeypatch.setattr(run.store, "get_view", lambda name, date=None: view)
    monkeypatch.setattr(run.store, "put_view", lambda name, v: "mem://view")
    scores = {"A": 0.1, "B": 0.9, "C": 0.5}
    from tools.analysis import serialize
    monkeypatch.setattr(serialize, "load_record",
                        lambda code, date="latest": {"council": {"default": {"综合分": scores[code]}}})

    filled = run._enrich_near_miss("2026-08-08")
    assert filled == 3
    order = [x["code"] for x in view["接近达标"]["电子"]]
    assert order == ["B", "C", "A"]                               # 按合议分降序
    assert view["接近达标"]["电子"][0]["合议分"] == 0.9
