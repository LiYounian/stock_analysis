"""前向收益 / α 引擎(与项目 event_study / shadow / forward_scorecard 口径一致)。

收益口径:r_N = close[进场+N] / close[进场] − 1(同 forward_scorecard 的 r_N 定义)。
进场锚定:execution_lag=1 → 信号日 t 收盘确定性节点已知,**次日 t+1 进场**,杜绝
"用当日收盘信号买当日收盘价"的偷看(比 event_study 默认的当日进场更严)。
α 口径:α_N = 个股前向 r_N − 全A等权前向 r_N(全样本均值 = 等权基准),
单位 pp。与 shadow_score 的 alpha=买入侧 mean − 全样本 mean 同源。
窗口越界(进场+N 超出数据)→ NaN(不结算、不编造),与 event_study 一致。

本模块用 date×code 的收盘价宽面板做向量化前向收益(等价于对每票逐一 event_study,
但快且省内存);test_capitulation_backtest.py 断言其与 event_study.forward_returns 数值一致。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def forward_returns_panel(close_panel: pd.DataFrame, signal_date,
                          horizons=(1, 5), lag: int = 1) -> dict[int, pd.Series]:
    """给定信号日,返回各 horizon 的**逐票**前向收益 Series(index=code,单位 pp)。

    进场 idx = pos(signal_date)+lag;退出 idx = 进场+N。越界或价缺 → NaN。
    """
    idx = close_panel.index
    if signal_date not in idx:
        # 顺延到首个 >= signal_date 的交易日(事件日非交易日容错)
        after = idx[idx >= pd.to_datetime(signal_date)]
        if len(after) == 0:
            return {N: pd.Series(dtype=float) for N in horizons}
        signal_date = after[0]
    i = idx.get_loc(signal_date)
    entry_i = i + lag
    out: dict[int, pd.Series] = {}
    n = len(idx)
    for N in horizons:
        exit_i = entry_i + N
        if entry_i >= n or exit_i >= n:
            out[N] = pd.Series(dtype=float)      # 未结算
            continue
        entry_px = close_panel.iloc[entry_i]
        exit_px = close_panel.iloc[exit_i]
        r = (exit_px / entry_px - 1.0) * 100.0    # pp
        out[N] = r.replace([np.inf, -np.inf], np.nan).dropna()
    return out


class ForwardBook:
    """预算 date×code 前向收益矩阵(t+1 进场),供 H1/H2 做 O(1) 查询。

    fmat[N][信号日, code] = close[进场+N]/close[进场]−1(pp),进场=信号日+lag。
    与 forward_returns_panel 同口径(test 断言二者一致),但向量化一次、复用多次。
    """

    def __init__(self, close_panel: pd.DataFrame, horizons=(1, 5), lag: int = 1):
        self.horizons = tuple(horizons)
        self.lag = lag
        self.fmat = {N: (close_panel.shift(-(lag + N)) / close_panel.shift(-lag) - 1.0) * 100.0
                     for N in self.horizons}
        self._day_cache: dict = {}

    def _day_row(self, date, N):
        key = (date, N)
        if key not in self._day_cache:
            fm = self.fmat[N]
            if date in fm.index:
                row = fm.loc[date].replace([np.inf, -np.inf], np.nan).dropna()
            else:
                row = pd.Series(dtype=float)
            bench = float(row.mean()) if len(row) else None
            self._day_cache[key] = (bench, row)
        return self._day_cache[key]

    def alpha_for_subset(self, signal_date, subset_codes, horizons=None) -> dict:
        horizons = horizons or self.horizons
        subset = set(map(str, subset_codes))
        res = {}
        for N in horizons:
            bench, row = self._day_row(signal_date, N)
            if bench is None:
                res[N] = None
                continue
            sub = row[row.index.isin(subset)].astype(float)
            if len(sub) == 0:
                res[N] = {"benchmark": round(bench, 6), "subset_mean": None, "alpha": None,
                          "n_subset": 0, "n_universe": int(len(row)), "hit": None,
                          "per_stock_r": [], "per_stock_alpha": []}
                continue
            per_alpha = sub - bench
            res[N] = {
                "benchmark": round(bench, 6),
                "subset_mean": round(float(sub.mean()), 6),
                "alpha": round(float(sub.mean()) - bench, 6),
                "n_subset": int(len(sub)), "n_universe": int(len(row)),
                "hit": round(float((per_alpha > 0).mean()), 6),
                "per_stock_r": [round(x, 6) for x in sub.tolist()],
                "per_stock_alpha": [round(x, 6) for x in per_alpha.tolist()],
            }
        return res

    def stock_alpha(self, signal_date, code, N):
        """单票在信号日的 (前向收益 r, 基准 benchmark, α=r−benchmark);缺则 None。"""
        bench, row = self._day_row(signal_date, N)
        if bench is None or code not in row.index:
            return None
        r = float(row[code])
        return {"r": r, "benchmark": bench, "alpha": r - bench}


def alpha_for_subset(close_panel: pd.DataFrame, signal_date, subset_codes,
                     horizons=(1, 5), lag: int = 1) -> dict:
    """一个信号日:子集(超跌票)前向收益、全样本(等权基准)前向收益、α=子集−基准。

    返回 {N: {benchmark, subset_mean, alpha, n_subset, n_universe, hit,
              per_stock_r(list), per_stock_alpha(list)}}。
    per_stock_alpha = 个股 r − benchmark;hit = per_stock_alpha>0 占比。
    """
    fwd = forward_returns_panel(close_panel, signal_date, horizons, lag)
    res = {}
    subset = set(map(str, subset_codes))
    for N in horizons:
        r_all = fwd[N]
        if len(r_all) == 0:
            res[N] = None
            continue
        benchmark = float(r_all.mean())
        sub = r_all[r_all.index.isin(subset)]
        if len(sub) == 0:
            res[N] = {"benchmark": benchmark, "subset_mean": None, "alpha": None,
                      "n_subset": 0, "n_universe": int(len(r_all)),
                      "hit": None, "per_stock_r": [], "per_stock_alpha": []}
            continue
        per_r = sub.astype(float)
        per_alpha = per_r - benchmark
        res[N] = {
            "benchmark": round(benchmark, 6),
            "subset_mean": round(float(per_r.mean()), 6),
            "alpha": round(float(per_r.mean()) - benchmark, 6),
            "n_subset": int(len(per_r)),
            "n_universe": int(len(r_all)),
            "hit": round(float((per_alpha > 0).mean()), 6),
            "per_stock_r": [round(x, 6) for x in per_r.tolist()],
            "per_stock_alpha": [round(x, 6) for x in per_alpha.tolist()],
        }
    return res


def bootstrap_ci(values, n_boot: int = 5000, ci: float = 0.95, seed: int = 42):
    """对一维样本做 bootstrap 均值置信区间。返回 (mean, lo, hi, p_two_sided_vs_0)。"""
    v = np.asarray([x for x in values if x is not None and not np.isnan(x)], dtype=float)
    if len(v) == 0:
        return None, None, None, None
    rng = np.random.default_rng(seed)
    boots = rng.choice(v, size=(n_boot, len(v)), replace=True).mean(axis=1)
    lo = float(np.quantile(boots, (1 - ci) / 2))
    hi = float(np.quantile(boots, 1 - (1 - ci) / 2))
    # 双侧 p:bootstrap 分布跨 0 的比例(近似)
    p = 2 * min((boots <= 0).mean(), (boots >= 0).mean())
    return round(float(v.mean()), 6), round(lo, 6), round(hi, 6), round(float(p), 6)
