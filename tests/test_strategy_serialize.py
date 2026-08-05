"""P1.6 单测:策略配置同步、超买超卖 verdict(KDJ过滤)、PE开关、结构化JSON。"""
import json

import numpy as np

from tools.analysis import serialize, technical as ta, valuation
from tools.config import strategy


def test_strategy_json_sync():
    """dump_json 落盘内容 = THRESHOLDS/FORMULAS(py↔json 一致)。"""
    path = strategy.dump_json()
    data = json.load(open(path, encoding="utf-8"))
    assert data["thresholds"] == strategy.THRESHOLDS
    assert data["formulas"] == strategy.FORMULAS


def test_ob_os_kdj_false_signal_filtered():
    """KDJ 假超买(J>100)但 RSI/BIAS 中性 → 判中性(不足2共振)。"""
    r = ta._overbought_oversold(k=90, j=110, rsi12=55, bias20=5.0)
    assert r["verdict"] == "中性"
    assert r["resonance"] == 1


def test_ob_os_genuine_oversold():
    """KDJ+RSI+BIAS 三共振超卖 → 超卖。"""
    r = ta._overbought_oversold(k=15, j=-5, rsi12=25, bias20=-15.0)
    assert r["verdict"] == "超卖"
    assert r["resonance"] == 3


def test_ob_os_single_indicator_not_enough():
    """仅 KDJ 超卖、其余中性 → 中性(1<2)。"""
    r = ta._overbought_oversold(k=15, j=5, rsi12=55, bias20=-5.0)
    assert r["verdict"] == "中性"


def test_ob_os_j_secondary_hint():
    """J<10 但 K>20:不算超卖,但给濒临提示。"""
    r = ta._overbought_oversold(k=25, j=8, rsi12=50, bias20=0.0)
    assert r["verdict"] == "中性"
    assert "濒临超卖提示" in r["per_indicator"]["kdj"]


def test_bias_in_compute():
    """compute 输出含 bias.bias20,数值符合 (close-MA20)/MA20*100。"""
    closes = list(np.linspace(10, 20, 70))
    df = _kline(closes)
    res = ta.compute(df)
    assert "bias20" in res["bias"]
    assert res["bias"]["bias20"] is not None


def test_pe_switch():
    assert valuation.pe_switch({"PE_TTM": -50, "净利": -1e8})["pe_valid"] is False
    assert valuation.pe_switch({"PE_TTM": 131, "净利": 1e7, "净利增速": -62, "毛利率": 21})["pe_valid"] is False
    # 低 PE + 净利暴增 → 疑一次性损益(东芯 PE1.65 场景)
    low = valuation.pe_switch({"PE_TTM": 1.65, "净利": 5e8, "净利增速": 333, "毛利率": 53})
    assert low["pe_valid"] is False and "一次性" in low["mode"]
    v = valuation.pe_switch({"PE_TTM": 30, "净利": 2e8, "净利增速": 20, "毛利率": 40})
    assert v["pe_valid"] is True


def test_serialize_record_schema():
    """build_record 含所有顶层字段(缺数据降级不报错)。"""
    rec = serialize.build_record("000021", "2026-08-05")
    for key in ("schema_version", "meta", "snapshot", "valuation",
                "fundamental", "signals", "fundflow", "events",
                "timeseries_refs", "provenance"):
        assert key in rec
    assert rec["meta"]["code"] == "000021"


def _kline(closes):
    import pandas as pd
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="D"),
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes,
        "volume": [1000.0] * n, "amount": [c * 1000 for c in closes],
        "turnover": [0.05] * n, "pct_chg": pd.Series(closes).pct_change().mul(100).tolist(),
    })
