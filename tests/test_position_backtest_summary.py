"""策略 S01 持仓回测**汇总报告接线**(模块③)单测。

锁语义:跨票扫描 → 逐票 backtest_one → summarize_trades 汇总 → 落 view「趋势深跌反包回测」;
历史不足的票诚实跳过;缺基准时 Alpha 不计并标注;无票/无信号优雅(不报错)。
"""
import pandas as pd
import pytest

from tools.backtest import position_backtest as pb
from tools.collectors import index, market
from tools.store import repo as store

E = 240                                                 # 进场索引(K线足够长过 251 根门槛)


def _flat(n=260, price=100.0, vol=1000.0):
    return pd.DataFrame({
        "date": pd.bdate_range("2019-01-01", periods=n),
        "open": [price] * n, "close": [price] * n,
        "high": [price + 0.2] * n, "low": [price - 0.2] * n,
        "volume": [vol] * n,
    })


def _with_rule3(df):
    """在 E+3 造 +30% 加速止盈离场(收益 +0.30)。"""
    df = df.copy()
    for col, val in (("open", 125), ("high", 131), ("low", 124), ("close", 130)):
        df.loc[E + 3, col] = val
    return df


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    monkeypatch.setattr(index, "load_index",
                        lambda c: pd.DataFrame({"date": pd.bdate_range("2019-01-01", periods=260),
                                                "close": [1000.0] * 260}))
    monkeypatch.setattr(pb, "find_signals", lambda k, cfg=None: [E])   # 定点信号(绕历史门槛)


def test_run_and_store_aggregates_and_writes_view(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    kl = {"AAA": _with_rule3(_flat()), "BBB": _flat()}   # AAA +30%(赢);BBB 平盘→时间成本 0%(不赢)
    monkeypatch.setattr(market, "load_kline", lambda c: kl[c])
    r = pb.run_and_store(codes=["AAA", "BBB"], fetch=False, min_sample=2)

    assert r["扫描票数"] == 2 and r["有效样本票"] == 2 and r["出信号票数"] == 2
    s = r["汇总"]
    assert s["交易数"] == 2 and s["已离场数"] == 2
    assert s["胜率"] == pytest.approx(0.5)               # AAA 赢 / BBB 不赢
    assert r["有基准"] is True
    # 落库可回读
    got = store.get_view("趋势深跌反包回测")
    assert got["汇总"]["交易数"] == 2


def test_skips_codes_with_insufficient_history(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    kl = {"AAA": _with_rule3(_flat()), "SHORT": _flat(n=100)}  # SHORT<251 → 跳过
    monkeypatch.setattr(market, "load_kline", lambda c: kl[c])
    r = pb.summarize(codes=["AAA", "SHORT"], fetch=False)
    assert r["有效样本票"] == 1 and r["跳过票数(历史不足/无K线)"] == 1


def test_missing_benchmark_notes_no_alpha(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)

    def _raise(_c):
        raise FileNotFoundError("no bench")

    monkeypatch.setattr(index, "load_index", _raise)
    monkeypatch.setattr(market, "load_kline", lambda c: _with_rule3(_flat()))
    r = pb.summarize(codes=["AAA"], fetch=False)
    assert r["有基准"] is False and "Alpha" in r["Alpha说明"]


def test_empty_universe_is_graceful(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    r = pb.summarize(codes=[], fetch=False)
    assert r["扫描票数"] == 0 and r["汇总"]["已离场数"] == 0
    assert "待积累" in r["汇总"]["状态"]
