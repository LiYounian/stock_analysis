"""capitulation 极端底部标记(一般性、因果、trailing-percentile 自标定)。

输入:compute_breadth() 产出的逐日宽度序列(date 索引,含 mean_pct/below_ma20_ratio/net_adv)。
定义(先验、绝不对例日/例票调参):一天 t 为 capitulation ⟺ 相对其 trailing 窗口
(截至 t、只含过去与当日 → 因果)联合极端:
  ① 超卖广度极端:below_ma20_ratio[t] ≥ trailing 分位 q_os(高分位)
  ② 近端崩跌:2 日累计等权跌幅 cum2[t]=mean_pct[t]+mean_pct[t-1] ≤ trailing 分位 q_crash(低分位)
trailing 窗口 500 交易日(min_periods 保证前 ~2 年 warmup 不出事件)。
先验网格 q_os∈{.85,.90,.95} × q_crash∈{.05,.10} 全报,不挑讨好例日的点。
普通普跌(ordinary down,H1 对照):mean_pct ≤ down_thresh 但非 capitulation。

⚠️ 无任何硬编码例日/例票分支(见 test 的 grep 断言)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_QOS_QC = [(0.85, 0.10), (0.90, 0.10), (0.95, 0.10),
           (0.85, 0.05), (0.90, 0.05), (0.95, 0.05)]
CUM_WINDOWS = [1, 2, 3, 5]     # 累计跌幅 lookback 窗(统筹防偷看细化①:窗口进网格,'2日'不享特权)
# 24 组合全交叉:(q_os, q_crash, w_cum)
GRID = [(qos, qc, w) for w in CUM_WINDOWS for (qos, qc) in _QOS_QC]
TRAILING = 500
DOWN_THRESH = -1.0     # 普通普跌阈值(全A等权 mean_pct ≤ -1%)


def _grid_key(q_os: float, q_crash: float, w_cum: int = 2) -> str:
    return f"cap_os{int(q_os*100)}_cr{int(q_crash*100)}_w{int(w_cum)}"


def build_capitulation_flags(breadth: pd.DataFrame, grid=GRID,
                             trailing: int = TRAILING,
                             down_thresh: float = DOWN_THRESH) -> pd.DataFrame:
    """返回逐日标记表:每个网格点(q_os×q_crash×w_cum)的阈值列与 cap 布尔列 + ordinary_down + warmup。

    cum_w[t]=最近 w_cum 日等权跌幅之和(rolling sum, 含当日, 只回看); crash 分位在其上取。
    """
    b = breadth.sort_index().copy()
    osr = b["below_ma20_ratio"].astype(float)
    mp = b["mean_pct"].astype(float)

    out = pd.DataFrame(index=b.index)
    out["mean_pct"] = mp
    out["below_ma20_ratio"] = osr
    out["net_adv"] = b["net_adv"].astype(float)
    out["warmup"] = osr.rolling(trailing, min_periods=trailing).count().isna()

    os_thr_cache = {}
    cum_cache = {}
    for q_os, q_crash, w in grid:
        if q_os not in os_thr_cache:
            os_thr_cache[q_os] = osr.rolling(trailing, min_periods=trailing).quantile(q_os)
        if w not in cum_cache:
            cum_cache[w] = mp.rolling(w, min_periods=w).sum()
        os_thr = os_thr_cache[q_os]
        cum_w = cum_cache[w]
        crash_thr = cum_w.rolling(trailing, min_periods=trailing).quantile(q_crash)
        cap = (osr >= os_thr) & (cum_w <= crash_thr) & (~out["warmup"])
        key = _grid_key(q_os, q_crash, w)
        out[f"{key}_cum"] = cum_w
        out[f"{key}_os_thr"] = os_thr
        out[f"{key}_crash_thr"] = crash_thr
        out[key] = cap.fillna(False)

    out["down_day"] = (mp <= down_thresh) & (~out["warmup"])
    for q_os, q_crash, w in grid:
        key = _grid_key(q_os, q_crash, w)
        out[f"ord_{key}"] = out["down_day"] & (~out[key])
    return out


def event_dates(flags: pd.DataFrame, key: str) -> list[pd.Timestamp]:
    """某网格点的 capitulation 事件日列表。"""
    return list(flags.index[flags[key]])


def ordinary_dates(flags: pd.DataFrame, key: str) -> list[pd.Timestamp]:
    return list(flags.index[flags[f"ord_{key}"]])
