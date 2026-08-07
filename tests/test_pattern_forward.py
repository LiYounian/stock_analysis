"""形态选股前瞻胜率回测(批次C)单测。

锁语义:达标池 view→事件、前瞻收益+胜率+Alpha、**t+1 进场防未来函数**(达标日价不做基)、
regime 择时开关过滤事件、无快照时诚实报告样本0。
"""
import pandas as pd
import pytest

from tools.backtest import pattern_forward as pf
from tools.collectors import index, market
from tools.store import repo as store


def _kline(start, closes):
    days = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"date": days, "close": [float(c) for c in closes]})


def _bench(start, n):
    return _kline(start, [1000.0] * n)          # 平盘基准 → Alpha = 个股前瞻


def _seed_views(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path)
    # 达标日 01-05:AAA(regime 分化=可交易);01-12:BBB(regime 冰点=不可交易)
    store.put_view("形态选股", {"达标清单": [{"code": "AAA"}], "达标占比": 0.03}, date="2024-01-05")
    store.put_view("市场状态", {"标签": "分化", "情绪分": 72}, date="2024-01-05")
    store.put_view("形态选股", {"达标清单": [{"code": "BBB"}], "达标占比": 0.02}, date="2024-01-12")
    store.put_view("市场状态", {"标签": "冰点", "情绪分": 12}, date="2024-01-12")


def _seed_klines(monkeypatch):
    # AAA:达标日(01-05)收盘设成天价陷阱 9999;t+1(01-08)起 101,102,...(递增→前瞻正)
    aaa = _kline("2024-01-05", [9999] + [101 + i for i in range(29)])
    # BBB:达标日 01-12,其后递减 → 前瞻负
    bbb = _kline("2024-01-12", [50] + [100 - i for i in range(29)])
    kl = {"AAA": aaa, "BBB": bbb}
    monkeypatch.setattr(market, "load_kline", lambda c: kl[c])
    monkeypatch.setattr(index, "load_index", lambda c: _bench("2024-01-05", 40))
    return kl


def test_collect_events_from_pool_views(monkeypatch, tmp_path):
    _seed_views(tmp_path, monkeypatch)
    evs = pf.collect_events()
    assert {(e["code"], e["date"]) for e in evs} == {("AAA", "2024-01-05"), ("BBB", "2024-01-12")}
    assert pf.pool_dates() == ["2024-01-05", "2024-01-12"]


def test_t_plus_1_entry_no_lookahead(monkeypatch, tmp_path):
    """进场锚 = 达标日次交易日(t+1);达标日天价 9999 绝不作基(防未来函数)。"""
    _seed_views(tmp_path, monkeypatch)
    kl = _seed_klines(monkeypatch)
    rows = pf.event_study.forward_returns([pf._entry_offset_date("2024-01-05")], kl["AAA"],
                                          windows=(5,))
    assert rows[0]["进场日"] == "2024-01-08"          # t+1,非达标日 01-05
    assert rows[0]["进场价"] == 101.0                 # 用 t+1 收盘,不是 9999


def test_backtest_winrate_and_alpha(monkeypatch, tmp_path):
    _seed_views(tmp_path, monkeypatch)
    _seed_klines(monkeypatch)
    r = pf.run_backtest(windows=(5,), gate=False)
    assert r["样本天数"] == 2 and r["事件数"] == 2
    s5 = r["汇总"][5]
    assert s5["样本数"] == 2 and s5["胜率"] == 0.5      # AAA 正 / BBB 负
    assert s5["平均Alpha"] is not None                 # 平盘基准→Alpha 有值


def test_regime_gate_filters_events(monkeypatch, tmp_path):
    _seed_views(tmp_path, monkeypatch)
    _seed_klines(monkeypatch)
    on = pf.run_backtest(windows=(5,), gate=True)
    assert on["事件数"] == 1                            # 只留 01-05(分化);01-12(冰点)被滤
    assert on["汇总"][5]["胜率"] == 1.0                 # 仅 AAA(正)


def test_regime_gain_and_empty(monkeypatch, tmp_path):
    _seed_views(tmp_path, monkeypatch)
    _seed_klines(monkeypatch)
    g = pf.regime_gain(windows=(5,))
    assert g["gate_off"]["事件数"] == 2 and g["gate_on"]["事件数"] == 1
    assert 5 in g["regime增益"] and "结论" in g

    # 无任何快照 → 样本0天,诚实结论
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "empty")
    g2 = pf.regime_gain(windows=(5,))
    assert g2["gate_off"]["样本天数"] == 0 and "样本0天" in g2["结论"]
