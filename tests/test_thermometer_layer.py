"""板块环境层(生产 native)纯逻辑锁。重 IO 的端到端已手工冒烟;此处锁不依赖数据的语义。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.analysis.industry_temp import thermometer as TH


def test_band_thresholds():
    # 时序档 1/3,2/3
    assert TH._band(0.1, TH.TS_COLD, TH.TS_HOT) == "冷"
    assert TH._band(0.5, TH.TS_COLD, TH.TS_HOT) == "温"
    assert TH._band(0.9, TH.TS_COLD, TH.TS_HOT) == "热"
    # 截面档 0.2,0.8(与 pattern 对齐)
    assert TH._band(0.15, TH.CS_COLD, TH.CS_HOT) == "冷"
    assert TH._band(0.5, TH.CS_COLD, TH.CS_HOT) == "温"
    assert TH._band(0.85, TH.CS_COLD, TH.CS_HOT) == "热"
    assert TH._band(None, TH.CS_COLD, TH.CS_HOT) is None
    assert TH._band(float("nan"), TH.TS_COLD, TH.TS_HOT) is None


def test_mom20_causal():
    """20日动量因果:改未来点不影响过去的动量值。"""
    idx = pd.bdate_range("2024-01-01", periods=200).strftime("%Y-%m-%d")
    close = pd.Series(100 * np.cumprod(1 + 0.005 * np.sin(np.arange(200) / 7)), index=idx)
    m = TH._mom20(close, 20)
    close2 = close.copy()
    close2.iloc[-1] *= 1.3
    m2 = TH._mom20(close2, 20)
    assert np.isclose(m.iloc[-5], m2.iloc[-5])       # 过去点不受未来影响
    assert m.iloc[:20].isna().all()                  # 前 win 段无值


def test_panel_rejects_deep_kou():
    """生产层只暴露 native;deep 研究口径不得从此层出。"""
    import pytest
    with pytest.raises(ValueError):
        TH.build_thermometer_panel(["2026-09-11"], {}, 口径="deep")


def test_schema_has_both_momentum_kou():
    """契约:动量冷热必须同时给时序与截面两种口径(语义不同,防下游误用)。"""
    for c in ("动量_时序分位", "动量_时序档", "动量_截面分位", "动量_截面档"):
        assert c in TH.PANEL_COLS
