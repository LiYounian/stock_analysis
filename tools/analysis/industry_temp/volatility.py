"""行业波动维(Marks 表15-1「风险」行)——IET 设计过、但从没建的第4数值维。

⚠️ 语义谨慎:Marks「风险高」出现在顶部(过热),但**已实现波动**往往在**底部恐慌/
capitulation** 也飙升。故本维**不预设** high-vol=过热;A/B 仅按"波动分位≥cut → A"编码为
约定,真实方向(A 究竟对应过热还是过冷)由回测的 IC/前瞻收益**符号**揭示,报告如实标注。

防未来:已实现波动 = trailing 对数收益标准差(rolling,只用≤T);分位 = causal_rolling_pctile。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from tools.analysis.industry_temp.temperature import (
    PCTILE_MIN,
    PCTILE_WIN,
    VAL_CUT,
    causal_rolling_pctile,
)

TRADING_DAYS = 244.0
VOL_WIN = 20  # 已实现波动窗口(约1个月交易日)


def realized_vol_series(close: pd.Series, win: int = VOL_WIN) -> pd.Series:
    """年化已实现波动:trailing ``win`` 日对数收益标准差 × sqrt(244),严格因果。

    close: index=date(升序), 值=收盘价。返回同 index 的年化波动率序列(前 win 段为 NaN)。
    """
    close = pd.Series(close, dtype="float64")
    logret = np.log(close / close.shift(1))
    vol = logret.rolling(win, min_periods=win).std() * np.sqrt(TRADING_DAYS)
    return vol


def vol_pctile_series(
    close: pd.Series,
    *,
    vol_win: int = VOL_WIN,
    pctile_win: int = PCTILE_WIN,
    pctile_min: int = PCTILE_MIN,
) -> pd.Series:
    """收盘序列 → 已实现波动 → 因果历史分位 [0,1](只用≤T 的 trailing 窗口)。"""
    vol = realized_vol_series(close, vol_win)
    return causal_rolling_pctile(vol, win=pctile_win, min_periods=pctile_min)


def ab_volatility(vol_pctile: Optional[float], cut: float = VAL_CUT) -> Optional[str]:
    """波动分位 ≥cut → 'A'(约定:高波动侧);否则 'B';None/NaN → None(弃权)。

    注意:'A' 是否等于「过热」不在此断言——由回测前瞻收益符号决定,防止把语义写死。
    """
    if vol_pctile is None or pd.isna(vol_pctile):
        return None
    return "A" if float(vol_pctile) >= cut else "B"
