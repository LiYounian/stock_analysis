"""形态选股编排单测(mock 采集层,不触网)。

锁语义:基准沪深300 → 逐票 RS(单层)→ 硬规则 AND → 达标占比 → 落 view「形态选股」;
达标池只含真达标票;降级声明写进 view。
"""
import pandas as pd
import pytest

from tools.collectors import index, market
from tools.pipeline import screen_pattern as sp
from tools.store import repo as store


def _breakout_df():
    base = [100 + (2 if i % 2 else -2) for i in range(20)]
    closes = base + [108]                         # 末根放量突破箱体
    vols = [1000.0] * 20 + [2500.0]
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=21, freq="D"),
        "open": closes, "high": [c * 1.005 for c in closes],
        "low": [c * 0.995 for c in closes], "close": closes, "volume": vols,
    })


def _flat_df():
    flat = [100 + (0.5 if i % 2 else -0.5) for i in range(21)]
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=21, freq="D"),
        "open": flat, "high": flat, "low": flat, "close": flat,
        "volume": [1000.0] * 21})


def _bench_df():
    """沪深300 基准:平盘(20日收益≈0),让突破票 RS 为正→达标。"""
    return pd.DataFrame({"date": pd.date_range("2024-01-01", periods=21, freq="D"),
                         "close": [100.0] * 21})


def test_run_pattern_screen_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path)
    monkeypatch.setattr(index, "load_index", lambda code: _bench_df())
    klines = {"AAA": _breakout_df(), "BBB": _flat_df()}
    monkeypatch.setattr(market, "load_kline", lambda code: klines[code])

    view = sp.run_pattern_screen(["AAA", "BBB"], as_of="2024-06-01", fetch=False)

    assert view["扫描数"] == 2 and view["有效样本"] == 2
    assert view["达标数"] == 1
    assert [x["code"] for x in view["达标清单"]] == ["AAA"]        # 只有突破票达标
    assert "箱体" in view["达标清单"][0]["命中形态"]
    assert "单层" in view["降级"]["RS"]                            # 降级声明落库
    # 落 view 可读回
    got = store.get_view("形态选股", date="2024-06-01")
    assert got["达标占比"] == 0.5


def test_skips_insufficient_kline(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path)
    monkeypatch.setattr(index, "load_index", lambda code: _bench_df())
    monkeypatch.setattr(market, "load_kline",
                        lambda code: _breakout_df().head(5))       # 不足 win+1
    view = sp.run_pattern_screen(["AAA"], as_of="2024-06-01", fetch=False)
    assert view["跳过数"] == 1 and view["有效样本"] == 0 and view["达标占比"] == 0.0
