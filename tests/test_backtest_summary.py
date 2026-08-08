"""前瞻回测闭环单测:防未来函数前瞻收益 / 胜率 / Alpha / regime 分层 / 待观察兜底。

全 data-independent(monkeypatch 合成达标池 view + 市场状态 view + K线),
锁住"为什么这么改"的语义:t+1 锚不回看、窗口未到期记待观察、按市场状态分层胜率。
"""
import pandas as pd
import pytest

from tools.backtest import backtest_summary as bs
from tools.backtest import pattern_forward
from tools.collectors import market
from tools.store import repo as store


def _kline(anchor_start: str, n: int, p0: float, step: float) -> pd.DataFrame:
    """连续 n 个交易日的 K线,close 从 p0 每日 +step(step<0 即下跌)。"""
    dates = pd.bdate_range(start=anchor_start, periods=n)
    return pd.DataFrame({"date": dates, "close": [p0 + step * i for i in range(n)]})


def _install(monkeypatch, pool: dict, regime: dict, klines: dict, bench=None):
    """装好合成环境:达标池 view / 市场状态 view / 单票 K线 / 基准。

    pool:   {date: [code,...]}         → 形态选股 达标清单
    regime: {date: label}             → 市场状态 标签(缺 date → None)
    klines: {code: DataFrame}         → market.load_kline
    bench:  DataFrame|None            → 沪深300(pattern_forward._bench_df)
    """
    dates = sorted(pool)

    def fake_get_view(name, date="latest"):
        if name == pattern_forward._POOL_VIEW:
            if date not in pool:
                raise FileNotFoundError(date)
            return {"达标清单": [{"code": c} for c in pool[date]],
                    "扫描数": 500, "达标数": len(pool[date])}
        if name == pattern_forward._REGIME_VIEW:
            if date not in regime:
                raise FileNotFoundError(date)
            return {"标签": regime[date]}
        raise FileNotFoundError(name)

    def fake_load_kline(code):
        if code in klines:
            return klines[code]
        raise FileNotFoundError(code)

    monkeypatch.setattr(store, "list_dates", lambda root="analysis": dates)
    monkeypatch.setattr(store, "get_view", fake_get_view)
    monkeypatch.setattr(market, "load_kline", fake_load_kline)
    monkeypatch.setattr(pattern_forward, "_bench_df", lambda fetch=False: bench)


# ———————————————————— 前瞻收益 / 胜率 / Alpha ————————————————————
def test_forward_returns_and_winrate(monkeypatch):
    """两达标日、涨/跌两票 → 各持有期胜率与均值符合防未来函数口径。"""
    up = _kline("2026-08-03", 30, 100.0, 1.0)      # 持续上涨 → 每个前瞻窗都赢
    dn = _kline("2026-08-03", 30, 100.0, -1.0)     # 持续下跌 → 输
    _install(monkeypatch,
             pool={"2026-08-06": ["AAA", "BBB"], "2026-08-07": ["AAA"]},
             regime={"2026-08-06": "震荡", "2026-08-07": "牛市共振"},
             klines={"AAA": up, "BBB": dn})
    r = bs.summarize(windows=(5, 10, 20))
    assert r["样本数"] == 3 and r["达标日数"] == 2
    w5 = r["各持有期"][5]
    assert w5["样本数"] == 3                        # AAA@06, BBB@06, AAA@07 都到期
    assert w5["胜率"] == pytest.approx(2 / 3, abs=1e-6)   # AAA 两胜、BBB 一负
    assert w5["平均收益"] > 0                       # 两涨一跌净正
    assert "可用" in w5["状态"] or "样本少" in w5["状态"]


def test_alpha_uses_benchmark(monkeypatch):
    """给基准 → Alpha = 个股前瞻 − 基准前瞻;基准走平时 Alpha≈个股收益。"""
    up = _kline("2026-08-03", 30, 100.0, 1.0)
    flat = _kline("2026-08-03", 30, 3000.0, 0.0)   # 沪深300 走平
    _install(monkeypatch, pool={"2026-08-06": ["AAA"]},
             regime={"2026-08-06": "震荡"}, klines={"AAA": up}, bench=flat)
    r = bs.summarize(windows=(5,))
    assert r["有基准"] is True
    w5 = r["各持有期"][5]
    assert w5["平均Alpha"] is not None and w5["平均Alpha"] > 0


def test_no_benchmark_alpha_none(monkeypatch):
    """无基准 → Alpha 未计算,带说明,不报错。"""
    up = _kline("2026-08-03", 30, 100.0, 1.0)
    _install(monkeypatch, pool={"2026-08-06": ["AAA"]},
             regime={"2026-08-06": "震荡"}, klines={"AAA": up}, bench=None)
    r = bs.summarize(windows=(5,))
    assert r["有基准"] is False and r["各持有期"][5]["平均Alpha"] is None
    assert "Alpha说明" in r


# ———————————————————— regime 分层 ————————————————————
def test_stratify_by_regime(monkeypatch):
    """按市场状态分层:各 regime 桶独立算胜率。"""
    up = _kline("2026-08-03", 30, 100.0, 1.0)
    dn = _kline("2026-08-03", 30, 100.0, -1.0)
    _install(monkeypatch,
             pool={"2026-08-06": ["AAA", "BBB"], "2026-08-07": ["AAA"]},
             regime={"2026-08-06": "震荡", "2026-08-07": "牛市共振"},
             klines={"AAA": up, "BBB": dn})
    strat = bs.summarize(windows=(5,))["按市场状态分层"]
    assert set(strat) == {"震荡", "牛市共振"}
    assert strat["震荡"]["事件数"] == 2
    assert strat["震荡"]["各持有期"][5]["胜率"] == pytest.approx(0.5, abs=1e-6)  # AAA胜/BBB负
    assert strat["牛市共振"]["各持有期"][5]["胜率"] == pytest.approx(1.0, abs=1e-6)


def test_regime_missing_defaults_uncategorized(monkeypatch):
    """无市场状态 view → 分层桶落「未分类」,不报错。"""
    up = _kline("2026-08-03", 30, 100.0, 1.0)
    _install(monkeypatch, pool={"2026-08-06": ["AAA"]},
             regime={}, klines={"AAA": up})
    strat = bs.summarize(windows=(5,))["按市场状态分层"]
    assert list(strat) == [bs._UNTRADED]


# ———————————————————— 待观察 / 兜底 ————————————————————
def test_await_observation_when_window_not_matured(monkeypatch):
    """K线不够长(前瞻窗未到期)→ 样本0、标注待观察、不崩不编造。"""
    short = _kline("2026-08-03", 7, 100.0, 1.0)    # 进场后不足 5 根
    _install(monkeypatch, pool={"2026-08-06": ["AAA"]},
             regime={"2026-08-06": "震荡"}, klines={"AAA": short})
    r = bs.summarize(windows=(5, 10, 20))
    assert r["各持有期"][5]["样本数"] == 0
    assert "待观察" in r["各持有期"][5]["状态"]
    assert "待观察" in r["状态"]                    # 整体也待观察


def test_zero_qualified_days_graceful(monkeypatch):
    """无任何「形态选股」view → 0 达标日,优雅标注,不报错。"""
    _install(monkeypatch, pool={}, regime={}, klines={})
    # pool 空 → list_dates 空 → pool_dates 返回 []
    r = bs.summarize(windows=(5, 10, 20))
    assert r["样本数"] == 0 and r["达标日数"] == 0
    assert r["按市场状态分层"] == {} and r["逐日"] == []
    assert "无达标池快照" in r["状态"]


def test_missing_kline_skipped_not_crash(monkeypatch):
    """达标票无 K线缓存 → 跳过计数,不报错。"""
    up = _kline("2026-08-03", 30, 100.0, 1.0)
    _install(monkeypatch, pool={"2026-08-06": ["AAA", "NOCACHE"]},
             regime={"2026-08-06": "震荡"}, klines={"AAA": up})
    r = bs.summarize(windows=(5,))
    assert r["K线缺失跳过"] == 1 and r["各持有期"][5]["样本数"] == 1


# ———————————————————— 逐日 / 落库 ————————————————————
def test_per_date_breakdown(monkeypatch):
    """逐日快照:每达标日一段,带其市场状态与事件数,按日期升序。"""
    up = _kline("2026-08-03", 30, 100.0, 1.0)
    _install(monkeypatch,
             pool={"2026-08-06": ["AAA"], "2026-08-07": ["AAA"]},
             regime={"2026-08-06": "震荡", "2026-08-07": "牛市共振"},
             klines={"AAA": up})
    per = bs.summarize(windows=(5,))["逐日"]
    assert [d["日期"] for d in per] == ["2026-08-06", "2026-08-07"]
    assert per[0]["市场状态"] == "震荡" and per[1]["市场状态"] == "牛市共振"
    assert per[0]["事件数"] == 1


def test_run_and_store_writes_backtest_json(monkeypatch):
    """run_and_store 落 backtest.json 到最新达标日(不真写盘,捕获 put_view 调用)。"""
    up = _kline("2026-08-03", 30, 100.0, 1.0)
    _install(monkeypatch,
             pool={"2026-08-06": ["AAA"], "2026-08-07": ["AAA"]},
             regime={"2026-08-06": "震荡", "2026-08-07": "牛市共振"},
             klines={"AAA": up})
    calls = []
    monkeypatch.setattr(store, "put_view",
                        lambda name, obj, date=None: calls.append((name, date)) or "ok")
    bs.run_and_store(windows=(5,))
    names = {c[0] for c in calls}
    assert bs._BACKTEST_VIEW in names                         # 落了 backtest.json
    assert all(c[1] == "2026-08-07" for c in calls)           # 写到最新达标日


def test_run_and_store_per_date_writes_each_day(monkeypatch):
    """--per-date 时逐日各写一份 backtest.json。"""
    up = _kline("2026-08-03", 30, 100.0, 1.0)
    _install(monkeypatch,
             pool={"2026-08-06": ["AAA"], "2026-08-07": ["AAA"]},
             regime={"2026-08-06": "震荡", "2026-08-07": "牛市共振"},
             klines={"AAA": up})
    dates_written = []
    monkeypatch.setattr(store, "put_view",
                        lambda name, obj, date=None: dates_written.append((name, date)) or "ok")
    bs.run_and_store(windows=(5,), per_date=True)
    bt_dates = {d for n, d in dates_written if n == bs._BACKTEST_VIEW}
    assert {"2026-08-06", "2026-08-07"} <= bt_dates            # 每达标日都写了


def test_zero_days_still_records_await(monkeypatch):
    """0 达标日仍落一份待观察到最新分析日期(机器已挂上可见)。"""
    _install(monkeypatch, pool={}, regime={}, klines={})
    monkeypatch.setattr(store, "list_dates", lambda root="analysis": ["2026-08-07"])
    calls = []
    monkeypatch.setattr(store, "put_view",
                        lambda name, obj, date=None: calls.append((name, date)) or "ok")
    bs.run_and_store(windows=(5,))
    assert (bs._BACKTEST_VIEW, "2026-08-07") in calls
