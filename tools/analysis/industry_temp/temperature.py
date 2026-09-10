"""IET 温度打分:3 维 A/B 二分类 → 等权多数票 k∈{0..3}。

纯逻辑层,不触 IO(面板/RRG 由调用方喂),便于单测锁语义(守则6)。
维度(探针规格 §3):
  动量维  RS-Momentum ≥ 中枢 → A面(过热/动量向上);< 中枢 → B面
  估值维  行业 pe_median 的自身历史因果分位 ≥ 切点 → A面(贵/过热);否则 B面
  换手维  行业 turnover_mean 的自身历史因果分位 ≥ 切点 → A面(拥挤);否则 B面
聚合:k = 3 维中 A 面个数;任一维缺失(None)→ 该行业当日温度弃权(不硬填)。

A/B 编码:字符串 "A"/"B";弃权用 None。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from tools.config.strategy import THRESHOLDS

_IET = THRESHOLDS["行业环境温度计"]
VAL_CUT: float = _IET["IET_VAL_CUT"]
TURN_CUT: float = _IET["IET_TURN_CUT"]
MOM_CENTER: float = _IET["IET_MOM_CENTER"]
PCTILE_WIN: int = _IET["IET_PCTILE_WIN"]
PCTILE_MIN: int = _IET["IET_PCTILE_MIN"]

# 温度档(探针 4 档,k 越大越"热")
K_LABEL = {3: "强A", 2: "偏A", 1: "偏B", 0: "强B"}


# ————————————————————— 因果 rolling 历史分位 —————————————————————
def causal_rolling_pctile(
    series: pd.Series,
    win: int = PCTILE_WIN,
    min_periods: int = PCTILE_MIN,
) -> pd.Series:
    """逐点计算「当前值在其≤当前时点的滚动窗内的分位」,严格因果(不含未来)。

    分位定义 = 窗口内 ≤ 当前值 的比例 ∈ [0,1](含当前值自身)。
    - series 必须按时间升序(index=date 或位置序)。
    - 每点用「截至该点、长度≤win」的窗口;有效样本 < min_periods → 该点 NaN(弃权)。
    - 右移不变性:序列末尾追加新点不改变任何历史点的分位值(防未来泄漏,test_rolling_pctile_causal 锁)。
    """
    vals = series.to_numpy(dtype="float64")
    n = len(vals)
    out = np.full(n, np.nan)
    for i in range(n):
        lo = max(0, i - win + 1)
        window = vals[lo : i + 1]
        window = window[~np.isnan(window)]
        cur = vals[i]
        if np.isnan(cur) or len(window) < min_periods:
            continue
        out[i] = float(np.mean(window <= cur))
    return pd.Series(out, index=series.index)


# ————————————————————— 单维 A/B —————————————————————
def ab_momentum(rs_momentum: Optional[float], center: float = MOM_CENTER) -> Optional[str]:
    """RS-Momentum ≥ center → A面(过热);< center → B面;缺失 → None(弃权)。"""
    if rs_momentum is None or (isinstance(rs_momentum, float) and np.isnan(rs_momentum)):
        return None
    return "A" if rs_momentum >= center else "B"


def ab_valuation(val_pctile: Optional[float], cut: float = VAL_CUT) -> Optional[str]:
    """估值历史分位 ≥ cut → A面(贵/过热);否则 B面;缺失 → None(弃权)。"""
    if val_pctile is None or (isinstance(val_pctile, float) and np.isnan(val_pctile)):
        return None
    return "A" if val_pctile >= cut else "B"


def ab_turnover(turn_pctile: Optional[float], cut: float = TURN_CUT) -> Optional[str]:
    """换手历史分位 ≥ cut → A面(拥挤/过热);否则 B面;缺失 → None(弃权)。"""
    if turn_pctile is None or (isinstance(turn_pctile, float) and np.isnan(turn_pctile)):
        return None
    return "A" if turn_pctile >= cut else "B"


# ————————————————————— 聚合 k —————————————————————
def temperature_k(
    mom_ab: Optional[str],
    val_ab: Optional[str],
    turn_ab: Optional[str],
) -> dict:
    """3 维 A/B → 温度。任一维缺失 → 弃权(k=None,不进回测样本)。

    返回 {k, 档, 弃权, 维度:{动量,估值,换手}}。
    """
    dims = {"动量": mom_ab, "估值": val_ab, "换手": turn_ab}
    if any(v is None for v in dims.values()):
        return {"k": None, "档": None, "弃权": True, "维度": dims}
    k = sum(1 for v in dims.values() if v == "A")
    return {"k": k, "档": K_LABEL[k], "弃权": False, "维度": dims}


def score_row(
    rs_momentum: Optional[float],
    val_pctile: Optional[float],
    turn_pctile: Optional[float],
    *,
    val_cut: float = VAL_CUT,
    turn_cut: float = TURN_CUT,
    mom_center: float = MOM_CENTER,
) -> dict:
    """便捷入口:原始三量(RS-Momentum + 估值分位 + 换手分位)→ 温度 dict。

    切点/中枢可覆盖(供敏感性网格)。分位须由调用方用 causal_rolling_pctile 因果算好。
    """
    return temperature_k(
        ab_momentum(rs_momentum, mom_center),
        ab_valuation(val_pctile, val_cut),
        ab_turnover(turn_pctile, turn_cut),
    )
