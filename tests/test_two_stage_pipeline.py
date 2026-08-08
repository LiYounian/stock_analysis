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


# ———————————— OPT-2:run_two_stage 顺序 + 候选子集 ————————————
def test_run_two_stage_expensive_only_on_candidates(monkeypatch):
    """便宜筛先于 LLM;新闻/LLM 只对(达标∪自选)候选,而非全A。"""
    calls: list[tuple] = []

    from tools.pipeline import screen_pattern

    def fake_screen(codes, as_of, fetch=True):
        calls.append(("screen", list(codes)))
        return {"达标清单": [{"code": "QUAL1"}], "有效样本": len(codes),
                "候选数": 1, "达标数": 1}

    monkeypatch.setattr(screen_pattern, "run_pattern_screen", fake_screen)
    monkeypatch.setattr(run.stock_pool, "get_codes", lambda: ["WATCH1", "WATCH2"])

    def rec(name):
        def _f(*a, **k):
            calls.append((name, list(a[0]) if a else None))
        return _f

    for fn in ("collect_values_missing", "collect_message", "collect_market_context",
               "run_sentiment", "run_serialize", "run_events", "run_factor",
               "run_council", "run_panel", "run_screen"):
        monkeypatch.setattr(run, fn, rec(fn))

    out = run.run_two_stage(["A", "B", "C"], "2026-08-08")

    names = [c[0] for c in calls]
    assert names.index("screen") < names.index("run_sentiment")       # 便宜筛先于 LLM
    assert names.index("collect_message") < names.index("run_sentiment")
    assert names.index("run_sentiment") < names.index("run_council")
    # 候选 = 达标 ∪ 自选,去重保序
    assert out["candidates"] == ["QUAL1", "WATCH1", "WATCH2"]
    # 最贵两步(新闻/LLM)只对候选,不是全A 的 A/B/C
    sent = next(arg for n, arg in calls if n == "run_sentiment")
    msg = next(arg for n, arg in calls if n == "collect_message")
    assert sent == ["QUAL1", "WATCH1", "WATCH2"]
    assert msg == ["QUAL1", "WATCH1", "WATCH2"]


def test_run_two_stage_dedups_qualified_in_watchlist(monkeypatch):
    """达标票已在自选池时,候选去重不重复。"""
    from tools.pipeline import screen_pattern
    monkeypatch.setattr(screen_pattern, "run_pattern_screen",
                        lambda codes, as_of, fetch=True: {
                            "达标清单": [{"code": "WATCH1"}, {"code": "QUAL2"}],
                            "有效样本": len(codes), "候选数": 2, "达标数": 2})
    monkeypatch.setattr(run.stock_pool, "get_codes", lambda: ["WATCH1", "WATCH2"])
    for fn in ("collect_values_missing", "collect_message", "collect_market_context",
               "run_sentiment", "run_serialize", "run_events", "run_factor",
               "run_council", "run_panel", "run_screen"):
        monkeypatch.setattr(run, fn, lambda *a, **k: None)

    out = run.run_two_stage(["X"], "2026-08-08")
    assert out["candidates"] == ["WATCH1", "QUAL2", "WATCH2"]   # WATCH1 只出现一次
