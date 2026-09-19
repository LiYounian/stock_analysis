"""Wave2 体检卡三项补落盘·序列化器锁测试。

锁的语义(为什么改):体检卡工具读 per-stock json，但三项数据"算了/采了却没进 json"，
工具恒标"待补落盘"。本轮把它们补进序列化器。这些断言锁死"源数据存在则必进对应 json 段、
键名与工具读取处一致"——防未来重写序列化器时又把字段漏掉、或改了键名让工具读不到。

  · PE历史分位:fundamental 已算 fund["PE分位"] → 必进 valuation.pe_percentile
  · BOLL:technical.compute 已算 tech["boll"](含"位置") → 必进 snapshot.boll，
    且加"状态"别名=位置值(工具读"状态"，源产"位置"，别名不动 technical.py)
  · 两融:collectors.margin 已按 code 落库 → 见 test_两融_*（另加）
"""
from __future__ import annotations

import pandas as pd
import pytest

from tools.analysis import serialize as sz

AS_OF = "2026-09-18"
CODE = "000001"


def _kdf(last_day: str = AS_OF, n: int = 3) -> pd.DataFrame:
    dates = pd.bdate_range(end=pd.Timestamp(last_day), periods=n)
    return pd.DataFrame({"date": dates, "close": [10.0] * n})


def _fake_tech(boll: dict | None) -> dict:
    """最小 tech dict，覆盖 _build_snapshot 读取的全部键；boll=None 时不含 boll。"""
    t = {
        "last": {"close": 10.0, "pct_chg": 1.2},
        "ma": {"ma5": 9.8, "ma10": 9.5, "ma20": 9.0, "ma60": 8.5, "排列": "多头"},
        "macd": {"dif": 0.1, "dea": 0.05, "macd": 0.05, "状态": "金叉"},
        "kdj": {"k": 60, "d": 55, "j": 70, "状态": "-"},
        "rsi": {"rsi6": 55, "rsi12": 52, "rsi24": 50},
        "bias": {"bias20": 3.0},
        "vol": {"量比": 1.1, "状态": "温和"},
        "signal": {"评级": "中性", "得分": 0, "依据": []},
        "reversal": {}, "ob_os": {},
    }
    if boll is not None:
        t["boll"] = boll
    return t


@pytest.fixture
def stub(monkeypatch):
    """把非目标链路统一降级：无公告、无财报副作用；stock_pool 返回 None。"""
    monkeypatch.setattr(sz.an, "load_announcements", lambda c: [])
    monkeypatch.setattr(sz.stock_pool, "get", lambda c: None)


# ────────────────── BOLL：算了没拷 → 必进 snapshot.boll + 状态别名 ──────────────────
def test_boll_落盘_含状态别名等于位置(stub, monkeypatch):
    boll = {"上轨": 12.0, "中轨": 11.0, "下轨": 10.0, "带宽": 0.18,
            "percent_b": 0.97, "位置": "触上轨", "挤压": True}
    monkeypatch.setattr(sz.market, "load_kline_recent", lambda c: _kdf())
    monkeypatch.setattr(sz.ta, "compute", lambda k: _fake_tech(boll))
    monkeypatch.setattr(sz.fd, "load_fundamental", lambda c: {})

    rec = sz.build_record(CODE, AS_OF)
    snap_boll = (rec.get("snapshot") or {}).get("boll")
    assert snap_boll is not None, "technical 已算 boll，序列化器必须拷进 snapshot"
    # 工具读的三轨键名对齐
    assert snap_boll["上轨"] == 12.0 and snap_boll["中轨"] == 11.0 and snap_boll["下轨"] == 10.0
    # 关键别名:工具读 boll['状态']，源只产 '位置' → 序列化侧加同值别名
    assert snap_boll["状态"] == "触上轨" == snap_boll["位置"]
    # 原字段不丢(只加不改)
    assert snap_boll["挤压"] is True and snap_boll["percent_b"] == 0.97


def test_boll_源缺则snapshot不含boll(stub, monkeypatch):
    monkeypatch.setattr(sz.market, "load_kline_recent", lambda c: _kdf())
    monkeypatch.setattr(sz.ta, "compute", lambda k: _fake_tech(None))
    monkeypatch.setattr(sz.fd, "load_fundamental", lambda c: {})

    rec = sz.build_record(CODE, AS_OF)
    snap = rec.get("snapshot") or {}
    assert snap, "有 signal → snapshot 应存在"
    assert "boll" not in snap, "源无 boll 时不得凭空造字段(不编)"


# ────────────────── PE历史分位：算了没拷 → 必进 valuation.pe_percentile ──────────────────
def test_pe_percentile_落盘_透传fund(stub, monkeypatch):
    monkeypatch.setattr(sz.market, "load_kline_recent", lambda c: None)  # 无技术
    monkeypatch.setattr(sz.fd, "load_fundamental", lambda c: {
        "PE_TTM": 30.0, "PB": 2.0, "总市值": 100.0, "报告期": "20260630",
        "PE分位": 0.42, "PE分位窗口": None,
    })
    monkeypatch.setattr(sz.valuation, "pe_switch",
                        lambda f: {"pe_valid": True, "mode": "PE适用", "basis": "b"})

    rec = sz.build_record(CODE, AS_OF)
    val = rec.get("valuation") or {}
    assert val.get("pe_percentile") == 0.42, "fund['PE分位'] 必须进 valuation.pe_percentile"
    assert "pe_percentile_window" in val and val["pe_percentile_window"] is None


def test_pe_percentile_源缺则为None(stub, monkeypatch):
    monkeypatch.setattr(sz.market, "load_kline_recent", lambda c: None)
    monkeypatch.setattr(sz.fd, "load_fundamental", lambda c: {
        "PE_TTM": 30.0, "PB": 2.0, "总市值": 100.0, "报告期": "20260630",
    })
    monkeypatch.setattr(sz.valuation, "pe_switch",
                        lambda f: {"pe_valid": True, "mode": "PE适用", "basis": "b"})

    rec = sz.build_record(CODE, AS_OF)
    val = rec.get("valuation") or {}
    assert val.get("pe_percentile") is None, "源无分位 → None，不编"
