"""因子横截面标准化公共工具(去极值 + z-score)。

定位:分析/策略层内的小工具,被多因子选股策略复用(反转低换手组合等)。
口径与 jqfactor.winsorize_med / standardlize 等价——与 `semi_factor.py` 内的同名
私有实现**语义一致**(此处为抽提出的公共单一真源;semi_factor 暂保留其内部副本,
后续可收敛统一,属独立清理项,不在本策略范围内)。

- winsorize_med:中位数 ± scale×MAD 截断,压制离群值主导排名。
- zscore:(x-mean)/std,把不同量纲因子拉到同一把尺子后才能加权相加。

两者对**空 / 全同值**输入都安全(不抛错、不除零),守诚实性降级。
"""
from __future__ import annotations

from statistics import median

WINSOR_SCALE_DEFAULT = 3.0


def winsorize_med(values: list[float], scale: float = WINSOR_SCALE_DEFAULT) -> list[float]:
    """中位数 ± scale × MAD 截断。空 / 全同值(MAD=0)原样返回。"""
    if not values:
        return values
    med = median(values)
    devs = [abs(v - med) for v in values]
    mad = median(devs)
    if mad == 0:
        return list(values)
    lo, hi = med - scale * mad, med + scale * mad
    return [min(max(v, lo), hi) for v in values]


def zscore(values: list[float]) -> list[float]:
    """z-score 标准化:(x-mean)/std。空原样返回;全同值(std=0)→ 全 0.0(避免除零)。"""
    if not values:
        return values
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    std = var ** 0.5
    if std == 0:
        return [0.0] * len(values)
    return [(v - mean) / std for v in values]
