"""形态选股编排单测(mock 采集层,不触网)。

锁语义:
  · 基准沪深300 → 逐票 RS → 硬规则 AND → 达标占比 → 落 view「形态选股」。
  · 双层:板块基准=同业成分等权均值;个股收益 > 同业均值 且 同业均值 > 沪深300 才 RS 达标。
  · 成分缺失 → 全体降级单层;某行业样本 < 板块最小样本 → 该行业逐票降级单层。
"""
import pandas as pd
import pytest

from tools.collectors import board, index, market
from tools.pipeline import screen_pattern as sp
from tools.store import repo as store


def _breakout_df(last=108.0):
    """箱体放量突破;末根收盘 last 决定 20 日收益(越高收益越大)。"""
    base = [100 + (2 if i % 2 else -2) for i in range(20)]
    closes = base + [last]
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
    """沪深300 基准:平盘(20 日收益≈0)。"""
    return pd.DataFrame({"date": pd.date_range("2024-01-01", periods=21, freq="D"),
                         "close": [100.0] * 21})


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(index, "load_index", lambda code: _bench_df())


def test_single_layer_fallback_when_no_membership(monkeypatch, tmp_path):
    """成分映射缺失(RAW 隔离到空 tmp)→ 全体降级单层;突破票达标。"""
    _isolate(monkeypatch, tmp_path)
    klines = {"AAA": _breakout_df(), "BBB": _flat_df()}
    monkeypatch.setattr(market, "load_kline", lambda code: klines[code])
    view = sp.run_pattern_screen(["AAA", "BBB"], as_of="2024-06-01", fetch=False)
    assert view["RS模式"].startswith("单层")
    assert view["达标数"] == 1 and [x["code"] for x in view["达标清单"]] == ["AAA"]
    assert store.get_view("形态选股", date="2024-06-01")["达标占比"] == 0.5


def test_two_layer_uses_board_mean(monkeypatch, tmp_path):
    """双层:同业(3 只达最小样本)等权均值当板块基准;跑输同业均值的票 RS 不达标。"""
    _isolate(monkeypatch, tmp_path)
    # 同一行业 3 只,均为突破形态,20 日收益不同 → 均值≈10.2%
    klines = {"HI": _breakout_df(112.0), "MID": _breakout_df(108.0), "LO": _breakout_df(104.0)}
    monkeypatch.setattr(market, "load_kline", lambda code: klines[code])
    monkeypatch.setattr(board, "load_membership",
                        lambda: {"HI": "计算机", "MID": "计算机", "LO": "计算机"})
    view = sp.run_pattern_screen(["HI", "MID", "LO"], as_of="2024-06-01", fetch=False)
    assert view["RS模式"].startswith("双层") and view["板块数"] == 1
    hit = {x["code"] for x in view["达标清单"]}
    assert "LO" not in hit                      # 跑输同业均值 → 个股vs板块 RS<0 → 出局
    assert "HI" in hit                          # 跑赢同业均值且板块跑赢沪深300 → 达标
    assert view["单层降级票数"] == 0


def test_thin_board_degrades_to_single_layer(monkeypatch, tmp_path):
    """行业成分数 < 板块最小样本(默认 3)→ 该行业逐票降级单层。"""
    _isolate(monkeypatch, tmp_path)
    klines = {"HI": _breakout_df(112.0), "LO": _breakout_df(104.0)}   # 同行业仅 2 只 < 3
    monkeypatch.setattr(market, "load_kline", lambda code: klines[code])
    monkeypatch.setattr(board, "load_membership", lambda: {"HI": "计算机", "LO": "计算机"})
    view = sp.run_pattern_screen(["HI", "LO"], as_of="2024-06-01", fetch=False)
    assert view["板块数"] == 0 and view["单层降级票数"] == 2
    # 降级单层后两只(个股 vs 沪深300 平盘)收益均为正 → 都达标
    assert view["达标数"] == 2


def test_skips_insufficient_kline(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(market, "load_kline", lambda code: _breakout_df().head(5))
    view = sp.run_pattern_screen(["AAA"], as_of="2024-06-01", fetch=False)
    assert view["跳过数"] == 1 and view["有效样本"] == 0 and view["达标占比"] == 0.0
