"""RPS250 横截面百分位(A8)。

需求 §4.3:池内对 `Return250 = Close(T)/Close(T-250)-1` 从高到低排名 → 0~100 百分位。
与 `feat/s02-trend-filter` 的 `build_rs_panel` 同机制(`rank(pct=True)*100`,含并列均秩),
但**改用原始 Return250**(不减沪深300 基准)、窗口 250(s02 是超额收益、窗口 126)。
可重复:同池同数据 → 同结果(pandas rank 确定性)。

注:本函数只做「给定 {code:Return250} → {code:RPS}」的横截面排名;Return250 的计算由
indicators.return_n 提供,取数/组池在编排层。⚠️ 非投资建议。
"""
from __future__ import annotations

import math

import pandas as pd


def rps_from_returns(returns: dict[str, float]) -> dict[str, float]:
    """池内 Return250 → RPS250 百分位(0~100)。

    returns: {code: Return250};值为 None/NaN 的票**不参与排名、不出现在结果里**
    (需求 §10:RPS 无法计算的票在完整/增强模式跳过)。
    返回 {code: RPS250},保留 2 位小数,含并列均秩。
    """
    clean = {c: float(v) for c, v in (returns or {}).items()
             if v is not None and isinstance(v, (int, float)) and math.isfinite(v)}
    if not clean:
        return {}
    s = pd.Series(clean)
    ranked = s.rank(pct=True) * 100.0          # 高收益 → 高分位;并列取均秩
    return {c: round(float(v), 2) for c, v in ranked.items()}
