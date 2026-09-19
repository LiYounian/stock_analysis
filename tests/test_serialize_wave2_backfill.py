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


# ────────────────── 两融：采了没进record → 挂顶层 margin 块 + consensus 同款口径戳 ──────────────────
def _stub_margin(monkeypatch, summary):
    """把 collectors.margin 的 load/summarize 替换为受控返回（build_record 内 import 该模块）。"""
    from tools.collectors import margin as mg
    monkeypatch.setattr(mg, "load_margin", lambda c: ([summary] if summary else []))
    monkeypatch.setattr(mg, "summarize_asof", lambda recs, asof: (recs[0] if recs else None))


def test_margin_落盘_顶层块含口径戳(stub, monkeypatch):
    monkeypatch.setattr(sz.market, "load_kline_recent", lambda c: None)
    monkeypatch.setattr(sz.fd, "load_fundamental", lambda c: {})
    _stub_margin(monkeypatch, {
        "code": CODE, "date": "2026-09-15", "融资买入额": 1.0e7,
        "融资余额": 5.0e8, "融券余量": 1000.0, "market": "SZSE",
        "visible_after_close": True,
    })
    rec = sz.build_record(CODE, AS_OF)
    mb = rec.get("margin")
    assert mb is not None, "collectors.margin 有数据 → 必进顶层 margin 块"
    # 采集器归一键透传（工具读取处期望）
    assert mb["融资余额"] == 5.0e8 and mb["融资买入额"] == 1.0e7 and mb["融券余量"] == 1000.0
    # consensus 同款口径戳：口径日期 = 记录自带 date，非 as_of；as_of 更晚 → 陈旧
    assert mb[sz.VINTAGE_DATE] == "2026-09-15" and mb[sz.FRESHNESS] == sz.STALE
    # provenance 两条轴：布尔有数据 + 口径挂新鲜度
    assert rec["provenance"]["margin"] is True
    assert rec["provenance"]["口径"]["margin"]["口径日期"] == "2026-09-15"


def test_margin_源缺则为None_provenance假(stub, monkeypatch):
    monkeypatch.setattr(sz.market, "load_kline_recent", lambda c: None)
    monkeypatch.setattr(sz.fd, "load_fundamental", lambda c: {})
    _stub_margin(monkeypatch, None)
    rec = sz.build_record(CODE, AS_OF)
    assert rec.get("margin") is None, "无两融记录 → None，不编"
    assert rec["provenance"]["margin"] is False


def test_margin_契约合规(stub, monkeypatch):
    """带 margin 块的 record 过契约校验（margin 已登记 VINTAGE_BLOCKS/OPTIONAL_TOP）。"""
    from tools.contracts import record as rc
    monkeypatch.setattr(sz.market, "load_kline_recent", lambda c: None)
    monkeypatch.setattr(sz.fd, "load_fundamental", lambda c: {})
    _stub_margin(monkeypatch, {
        "code": CODE, "date": "2026-09-15", "融资买入额": 1.0e7,
        "融资余额": 5.0e8, "融券余量": 1000.0, "market": "SZSE",
        "visible_after_close": True,
    })
    rec = sz.build_record(CODE, AS_OF)
    assert rc.validate_record(rec) == []
