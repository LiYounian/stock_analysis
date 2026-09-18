"""gate 阈值语义锁 + 分层判定（合成 K 线锁死 T1/踢出）。守则6：锁"为什么改"。"""
import os
import pandas as pd
import pytest

from tools.pyramid.registry import get
import tools.pyramid.tools  # noqa: F401  触发注册
from tools.pyramid.tools import gate_tool
from tools.pyramid.tools.gate_tool import _TH, _is_20cm

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AS_OF = "2026-09-17"


# ── 阈值语义锁（写死值不许被无意改动）──
def test_阈值锁():
    assert _TH["T1量比"] == 1.5
    assert _TH["T2量比"] == 1.2
    assert _TH["T3量比"] == 1.0
    assert _TH["极高位pos60"] == 0.95
    assert _TH["涨停_主板"] == 9.7
    assert _TH["涨停_20cm"] == 19.5


def test_二十厘米板判定():
    assert _is_20cm("300308") and _is_20cm("688981") and _is_20cm("301001")
    assert not _is_20cm("600995") and not _is_20cm("000001")


def _mk_df(last_open, last_close, last_high, last_vol, base_close=10.0, base_vol=100.0):
    """24 根平盘 + 1 根测试日；prev_close/prev_high 由第 24 根给。"""
    rows = []
    dates = pd.date_range("2026-08-01", periods=25, freq="D")
    for i in range(24):
        rows.append({"date": dates[i], "open": base_close, "high": base_close + 0.1,
                     "low": base_close - 0.1, "close": base_close, "volume": base_vol,
                     "amount": base_close * base_vol})
    rows.append({"date": dates[24], "open": last_open, "high": last_high,
                 "low": min(last_open, last_close) - 0.05, "close": last_close,
                 "volume": last_vol, "amount": last_close * last_vol})
    return pd.DataFrame(rows)


def test_T1_合成锁(monkeypatch):
    # 收阳 +5%(未近涨停) ∧ 站上MA5 ∧ 破前高(10.1) ∧ 量比2.0≥1.5 → T1
    df = _mk_df(last_open=10.0, last_close=10.5, last_high=10.6, last_vol=200.0)
    monkeypatch.setattr(gate_tool, "load_kline", lambda *a, **k: df)
    res = get("gate").run(AS_OF, "600995", root=ROOT)
    assert res.fields["层级"] == "T1"
    assert res.fields["收阳"] and res.fields["站上MA5"] and res.fields["站上前日高"]


def test_涨停踢出_合成锁(monkeypatch):
    # +10% ≥ 涨停线9.7(主板) → 踢出（先于分层）
    df = _mk_df(last_open=10.0, last_close=11.0, last_high=11.0, last_vol=300.0)
    monkeypatch.setattr(gate_tool, "load_kline", lambda *a, **k: df)
    res = get("gate").run(AS_OF, "600995", root=ROOT)
    assert res.fields["层级"] == "踢出"
    assert res.fields["涨停"] is True


def test_极高位踢出_合成锁(monkeypatch):
    # 温和收阳(+2%)但 close 处近60日区间顶部 pos60≥0.95 → 踢出
    dates = pd.date_range("2026-08-01", periods=25, freq="D")
    rows = [{"date": dates[i], "open": 10.0, "high": 10.18, "low": 10.12,
             "close": 10.0, "volume": 100.0, "amount": 1000.0} for i in range(24)]
    rows.append({"date": dates[24], "open": 10.15, "high": 10.20, "low": 10.10,
                 "close": 10.20, "volume": 150.0, "amount": 1530.0})
    df = pd.DataFrame(rows)
    monkeypatch.setattr(gate_tool, "load_kline", lambda *a, **k: df)
    res = get("gate").run(AS_OF, "600995", root=ROOT)
    assert res.fields["pos60"] >= 0.95
    assert res.fields["极高位"] is True
    assert res.fields["层级"] == "踢出"


def test_gate_数据不足不编造():
    res = get("gate").run(AS_OF, "000000", root=ROOT)
    assert res.freshness == "missing"
    assert res.fields.get("数据不足") is True


def test_gate_真数据防未来():
    res = get("gate").run(AS_OF, "300308", root=ROOT)
    if res.fields.get("数据不足"):
        pytest.skip("无数据")
    assert res.防未来 is True
    assert res.fields["层级"] in ("T1", "T2", "T3", "未入层", "踢出")
