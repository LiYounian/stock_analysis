"""聚合与推断。

关键:按**执行日聚类**推断(同日多票横截面相关会虚增 t)。对每 (维度) 先算日内均值,
再在日间做 t 检验;naive 逐事件统计仅作参考。OOS 按日期切早/晚段。

⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _day_clustered(sub: pd.DataFrame, col: str) -> dict:
    """按 exec_date 聚类:日内均值 → 日间 mean/t/正比例。"""
    g = sub.groupby("exec_date")[col].mean().dropna()
    n_days = len(g)
    if n_days == 0:
        return dict(n_days=0, mean=np.nan, t=np.nan, days_pos=np.nan)
    mean = float(g.mean())
    sd = float(g.std(ddof=1)) if n_days > 1 else np.nan
    t = mean / (sd / np.sqrt(n_days)) if (sd and sd > 0) else np.nan
    return dict(n_days=n_days, mean=mean, t=float(t) if np.isfinite(t) else np.nan,
                days_pos=float((g > 0).mean()))


def summarize(events: pd.DataFrame, rule: str, buyable_only: bool = True) -> pd.DataFrame:
    """每 strategy 一行:触发/成交/胜率/均值收益/均值α + 日聚类 α。"""
    df = events[events["rule"] == rule].copy()
    rows = []
    for strat, sub_all in list(df.groupby("strategy")) + [("__ALL_CORE__", df)]:
        n_events = len(sub_all)
        sub = sub_all[sub_all["filled"]]
        if buyable_only:
            sub = sub[~sub["unbuyable"]]
        n_filled = len(sub)
        if n_filled == 0:
            rows.append(dict(strategy=strat, n_events=n_events, n_filled=0))
            continue
        dc_a = _day_clustered(sub, "alpha_net")
        rows.append(dict(
            strategy=strat, n_events=n_events, n_filled=n_filled,
            trigger_rate=round(n_filled / n_events, 4) if n_events else np.nan,
            win_net=round(float((sub["net"] > 0).mean()), 4),
            mean_ret=round(float(sub["ret"].mean()) * 100, 4),         # %
            mean_net=round(float(sub["net"].mean()) * 100, 4),         # %
            mean_alpha_net=round(float(sub["alpha_net"].mean()) * 100, 4),
            alpha_dayclust=round(dc_a["mean"] * 100, 4) if np.isfinite(dc_a["mean"]) else np.nan,
            alpha_t=round(dc_a["t"], 2) if np.isfinite(dc_a["t"]) else np.nan,
            n_days=dc_a["n_days"], days_alpha_pos=round(dc_a["days_pos"], 3),
            unbuyable_rate=round(float(sub_all["unbuyable"].mean()), 4),
        ))
    return pd.DataFrame(rows)


def oos_split(events: pd.DataFrame, rule: str, split_date: str,
              buyable_only: bool = True) -> pd.DataFrame:
    """__ALL_CORE__ 早/晚段 α 日聚类对比。"""
    df = events[(events["rule"] == rule) & events["filled"]].copy()
    if buyable_only:
        df = df[~df["unbuyable"]]
    rows = []
    for label, seg in (("早段", df[df["exec_date"] < split_date]),
                       ("晚段", df[df["exec_date"] >= split_date])):
        dc = _day_clustered(seg, "alpha_net")
        rows.append(dict(seg=label, split=split_date, n=len(seg),
                         alpha_dayclust=round(dc["mean"] * 100, 4) if np.isfinite(dc["mean"]) else np.nan,
                         t=round(dc["t"], 2) if np.isfinite(dc["t"]) else np.nan,
                         n_days=dc["n_days"],
                         days_pos=round(dc["days_pos"], 3) if np.isfinite(dc["days_pos"]) else np.nan))
    return pd.DataFrame(rows)


def paired_vs_baseline(our_events: pd.DataFrame, base_events: pd.DataFrame,
                       rule: str, buyable_only: bool = True) -> dict:
    """按 exec_date 配对:我们日均 α − baseline 日均 α → 日间 mean/t/正比例。"""
    def daily(ev):
        d = ev[(ev["rule"] == rule) & ev["filled"]].copy()
        if buyable_only:
            d = d[~d["unbuyable"]]
        return d.groupby("exec_date")["alpha_net"].mean()
    a = daily(our_events); b = daily(base_events)
    common = a.index.intersection(b.index)
    if len(common) == 0:
        return dict(n_days=0)
    diff = (a.loc[common] - b.loc[common]).dropna()
    n = len(diff)
    mean = float(diff.mean())
    sd = float(diff.std(ddof=1)) if n > 1 else np.nan
    t = mean / (sd / np.sqrt(n)) if (sd and sd > 0) else np.nan
    return dict(n_days=n, our_alpha=round(float(a.loc[common].mean()) * 100, 4),
                base_alpha=round(float(b.loc[common].mean()) * 100, 4),
                excess=round(mean * 100, 4),
                t=round(float(t), 2) if np.isfinite(t) else np.nan,
                days_excess_pos=round(float((diff > 0).mean()), 3))
