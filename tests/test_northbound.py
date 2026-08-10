"""北向采集单测:真查接口 + 新鲜度护栏;停更/被墙时 I4 降级(返 None/空,不抛)。"""
import types

import pandas as pd

from tools.collectors import northbound as nb


def test_trend_degrades_to_none():
    # 接口未 mock,本机可能可通但数据已停更 → 经新鲜度护栏仍降级 None(绝不抛)
    assert nb.trend("000001") is None


def test_trend_map_empty_when_unavailable():
    assert nb.trend_map(["000001", "600519"]) == {}   # 全不可得→空,资金流维度整体缺失


def test_trend_uses_fetch(monkeypatch):
    """接口一旦可用,trend 透传其值(证明只差数据源新鲜)。"""
    monkeypatch.setattr(nb, "_fetch_individual", lambda code, win, as_of=None: 1.23)
    assert nb.trend("000001") == 1.23
    assert nb.trend_map(["000001"]) == {"000001": 1.23}


def _fake_ak(last_date: str):
    """构造 stock_hsgt_individual_em 假返回:近几日北向增持资金,最新 bar = last_date。"""
    dates = pd.date_range(end=last_date, periods=12, freq="D").strftime("%Y-%m-%d").tolist()
    df = pd.DataFrame({"持股日期": dates, "今日增持资金": [1e6] * 12})
    return types.SimpleNamespace(stock_hsgt_individual_em=lambda symbol: df)


def test_fetch_individual_fresh_data_used(monkeypatch):
    """最新 bar 在新鲜窗口内 → 采用(近 win 日增持资金求和)。"""
    monkeypatch.setitem(__import__("sys").modules, "akshare", _fake_ak("2026-08-10"))
    v = nb._fetch_individual("000938", win=5, as_of="2026-08-10")
    assert v == 5e6                                   # 5 日 × 1e6


def test_fetch_individual_stale_data_abstains(monkeypatch):
    """最新 bar 距 as_of 超新鲜阈值(源停更)→ 抛让上层降级;trend 吞掉返 None。"""
    monkeypatch.setitem(__import__("sys").modules, "akshare", _fake_ak("2024-08-16"))
    assert nb.trend("000938", win=5, as_of="2026-08-10") is None
    assert nb.trend_map(["000938"], as_of="2026-08-10") == {}
