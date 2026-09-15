"""板块环境层(生产 native)纯逻辑锁。重 IO 的端到端已手工冒烟;此处锁不依赖数据的语义。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.analysis.industry_temp import thermometer as TH


def test_band_thresholds():
    assert TH._band(0.1) == "冷"
    assert TH._band(0.5) == "温"
    assert TH._band(0.9) == "热"
    assert TH._band(1 / 3 - 1e-9) == "冷"
    assert TH._band(2 / 3 + 1e-9) == "热"
    assert TH._band(None) is None
    assert TH._band(float("nan")) is None


def test_mom_pctile_causal():
    """20日动量因果分位:改未来点不影响过去分位值。"""
    idx = pd.bdate_range("2024-01-01", periods=400).strftime("%Y-%m-%d")
    close = pd.Series(100 * np.cumprod(1 + 0.005 * np.sin(np.arange(400) / 7)), index=idx)
    p = TH._mom_pctile(close, mom_win=20, pctile_win=250, pctile_min=60)
    close2 = close.copy()
    close2.iloc[-1] *= 1.3
    p2 = TH._mom_pctile(close2, mom_win=20, pctile_win=250, pctile_min=60)
    # 倒数第5点(过去)不受最后一点(未来)影响
    assert np.isclose(p.iloc[-5], p2.iloc[-5], equal_nan=True)
    # 分位落在 [0,1]
    valid = p.dropna()
    assert ((valid >= 0) & (valid <= 1)).all()


def test_panel_rejects_deep_kou():
    """生产层只暴露 native;deep 研究口径不得从此层出。"""
    import pytest
    with pytest.raises(ValueError):
        TH.build_thermometer_panel(["2026-09-11"], {}, 口径="deep")
