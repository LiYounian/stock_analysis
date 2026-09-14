"""H1 · 底部参与:capitulation 日超跌票的前向收益/α vs 普通普跌日。

问题:现规则 #10「普跌日整体降级」没区分普通普跌与 capitulation 极端底部;老经验
#5「全市场恐慌=真反弹」暗示极端底部该参与。检验:capitulation 触发后对超跌票参与的
前向 α(与绝对收益)是否显著为正、且优于普通普跌日——#5 是否该在极端底部压过 #10?

每个事件日:超跌票集合 = oversold_panel.loc[日];前向 = t+1 进场;
α = 超跌票均值前向 − 全样本(等权)前向。跨事件日聚合日均 α±SE + bootstrap + 命中率。
诚实:事件稀有,若 n 太小 / CI 跨 0 → 判"证不了",不硬判通过。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.backtest.capitulation.forward import ForwardBook, bootstrap_ci


def _aggregate(events, horizons):
    """events: list[ (date, alpha_for_subset结果) ]。聚合成 per-horizon 指标。"""
    agg = {}
    for N in horizons:
        day_alpha, day_abs, day_bench = [], [], []
        pooled_alpha, pooled_r = [], []
        for _, res in events:
            r = res.get(N)
            if not r or r.get("alpha") is None:
                continue
            day_alpha.append(r["alpha"])
            day_abs.append(r["subset_mean"])
            day_bench.append(r["benchmark"])
            pooled_alpha += r["per_stock_alpha"]
            pooled_r += r["per_stock_r"]
        n_days = len(day_alpha)
        mean_a, lo, hi, p = bootstrap_ci(day_alpha) if n_days else (None, None, None, None)
        agg[N] = {
            "n_events": n_days,
            "n_stocks": len(pooled_alpha),
            "day_alpha_mean": round(float(np.mean(day_alpha)), 4) if n_days else None,
            "day_alpha_se": round(float(np.std(day_alpha, ddof=1) / np.sqrt(n_days)), 4) if n_days > 1 else None,
            "alpha_ci95": (lo, hi),
            "alpha_boot_p": p,
            "abs_ret_mean": round(float(np.mean(day_abs)), 4) if n_days else None,
            "bench_mean": round(float(np.mean(day_bench)), 4) if n_days else None,
            "stock_alpha_hit": round(float(np.mean([a > 0 for a in pooled_alpha])), 4) if pooled_alpha else None,
            "stock_abs_hit": round(float(np.mean([r > 0 for r in pooled_r])), 4) if pooled_r else None,
        }
    return agg


def run_h1(book, oversold_panel, dates, horizons=(1, 5)):
    """给定一组事件日,算超跌票前向 α 聚合(book=ForwardBook)。"""
    events = []
    for d in dates:
        d = pd.Timestamp(d)
        if d not in oversold_panel.index:
            continue
        subset = list(oversold_panel.columns[oversold_panel.loc[d].values])
        if not subset:
            continue
        res = book.alpha_for_subset(d, subset, horizons)
        events.append((d, res))
    return _aggregate(events, horizons), events


def compare_groups(book, oversold_panel, cap_dates, ord_dates,
                   horizons=(1, 5), oos_start=None):
    """capitulation vs 普通普跌:两组各跑 run_h1,返回对比 dict。"""
    def _flt(ds):
        ds = [pd.Timestamp(d) for d in ds]
        if oos_start:
            ds = [d for d in ds if d >= pd.Timestamp(oos_start)]
        return ds
    cap_agg, _ = run_h1(book, oversold_panel, _flt(cap_dates), horizons)
    ord_agg, _ = run_h1(book, oversold_panel, _flt(ord_dates), horizons)
    return {"capitulation": cap_agg, "ordinary_down": ord_agg}
