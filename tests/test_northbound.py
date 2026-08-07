"""北向采集单测:个股明细本机不可用时 I4 降级(返 None/空,不抛)。"""
from tools.collectors import northbound as nb


def test_trend_degrades_to_none():
    assert nb.trend("000001") is None          # 接口未实现/不可用→None,不抛


def test_trend_map_empty_when_unavailable():
    assert nb.trend_map(["000001", "600519"]) == {}   # 全不可得→空,资金流维度整体缺失


def test_trend_uses_fetch(monkeypatch):
    """接口一旦可用,trend 透传其值(证明只差数据源实现)。"""
    monkeypatch.setattr(nb, "_fetch_individual", lambda code, win: 1.23)
    assert nb.trend("000001") == 1.23
    assert nb.trend_map(["000001"]) == {"000001": 1.23}
