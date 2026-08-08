"""L3 止盈止损阈值 · 历史数据验证(数据说话,非投资建议)。

目的:用自选池历史 K线,验证 L3 设计里几个占位阈值是否合理,给出**数据支持的推荐值**。
验证三件(对应用户口径:支撑压力 + 放量):
  A. 放量阈值区分度:突破日按量比分组,看未来 N 日收益/胜率——放量突破是否显著优于缩量。
  B. 突破容差 τ 的假突破率:不同 τ 下"突破后 N 日内跌回被突破位"的比例。
  C. 支撑/压力有效性:贴近支撑/压力时,未来 N 日是否倾向反弹/受阻(vs 无条件基准)。

防未来函数:
  - 突破用「前 20 日最高价」(rolling(20).max().shift(1),不含当日);
  - 量比用「前 5 日均量」(shift(1),不含当日);
  - pivot 支撑/压力只用「已确认」的(pivot 索引 + 窗口 ≤ 当前 t);
  - 前瞻收益 close[t+N]/close[t]-1 是被预测的标签,不作输入。

用法:python -m tools.backtest.validate_l3_thresholds [--horizon 5,10] [--near 2.0]
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from tools.collectors import market
from tools.config import stock_pool

_W = 5          # swing pivot 左右窗口(与 predict.support_resistance 一致)
_BREAK_LOOKBACK = 20   # 突破:超过前 N 日最高
_WARMUP = 25    # 起步预热(需前 20 日高 + 若干 pivot)


def _pivots(high: np.ndarray, low: np.ndarray, w: int = _W):
    """返回 (pivot_highs, pivot_lows):每项 (确认索引=i+w, 价格)。i 是局部极值位。"""
    ph, pl = [], []
    n = len(high)
    for i in range(w, n - w):
        seg_h = high[i - w:i + w + 1]
        seg_l = low[i - w:i + w + 1]
        if high[i] == seg_h.max():
            ph.append((i + w, float(high[i])))     # i+w 才"确认"(需右侧 w 根)
        if low[i] == seg_l.min():
            pl.append((i + w, float(low[i])))
    return ph, pl


def _nearest_levels(ph, pl, t: int, price: float):
    """t 时刻:已确认 pivot 中,现价下方最近支撑 S1、上方最近压力 R1(无则 None)。"""
    sups = [p for (ci, p) in pl if ci <= t and p < price]
    ress = [p for (ci, p) in ph if ci <= t and p > price]
    s1 = max(sups) if sups else None
    r1 = min(ress) if ress else None
    return s1, r1


def validate_stock(df: pd.DataFrame, horizons):
    """单只:产出突破样本 + 贴近支撑/压力样本(带未来收益)。"""
    df = df.reset_index(drop=True)
    close = df["close"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    vol = df["volume"].to_numpy(float)
    n = len(df)
    prior_high = pd.Series(high).rolling(_BREAK_LOOKBACK).max().shift(1).to_numpy()
    vma5_prev = pd.Series(vol).rolling(5).mean().shift(1).to_numpy()
    ph, pl = _pivots(high, low)

    breaks, near_sup, near_res = [], [], []
    maxN = max(horizons)
    for t in range(_WARMUP, n - maxN):
        if np.isnan(prior_high[t]) or np.isnan(vma5_prev[t]) or vma5_prev[t] <= 0:
            continue
        vr = vol[t] / vma5_prev[t]
        fwd = {N: close[t + N] / close[t] - 1.0 for N in horizons}
        # A/B 突破样本:当日收盘创前 20 日新高
        if close[t] > prior_high[t]:
            # 被突破位 = 前 20 日高(近似阻力);突破后是否 N 日内跌回该位
            level = prior_high[t]
            back = {N: bool((close[t + 1:t + 1 + N] < level).any()) for N in horizons}
            breaks.append({"vr": vr, "over": close[t] / level - 1.0, "fwd": fwd, "back": back})
        # C 支撑/压力有效性
        s1, r1 = _nearest_levels(ph, pl, t, close[t])
        if s1 is not None and 0 <= (close[t] - s1) / s1 <= 0.02:      # 贴近支撑(上方2%内)
            near_sup.append(fwd)
        if r1 is not None and 0 <= (r1 - close[t]) / close[t] <= 0.02:  # 贴近压力(下方2%内)
            near_res.append(fwd)
    return breaks, near_sup, near_res


def _winrate(vals):
    return (float(np.mean([v > 0 for v in vals])) * 100) if vals else float("nan")


def _mean_pct(vals):
    return (float(np.mean(vals)) * 100) if vals else float("nan")


def run(horizons=(5, 10)):
    codes = stock_pool.get_codes()
    allbreaks, allsup, allres, allfwd = [], [], [], {N: [] for N in horizons}
    used = 0
    for code in codes:
        try:
            df = market.load_kline(code)
        except Exception:
            continue
        if df is None or len(df) < _WARMUP + max(horizons) + 10:
            continue
        used += 1
        b, ns, nr = validate_stock(df, horizons)
        allbreaks += b; allsup += ns; allres += nr
        # 无条件基准:所有可用 t 的前瞻收益
        c = df["close"].to_numpy(float)
        for N in horizons:
            allfwd[N] += list(c[_WARMUP + N:len(c)] / c[_WARMUP:len(c) - N] - 1.0)

    print(f"\n===== L3 阈值历史验证 · 样本 {used} 只 · horizon={horizons} =====")
    print("(非投资建议;防未来函数)\n")

    print("【基准】无条件前瞻收益(所有交易日)")
    for N in horizons:
        print(f"  {N}日: 上涨概率 {_winrate(allfwd[N]):.1f}% · 均值 {_mean_pct(allfwd[N]):+.2f}% · n={len(allfwd[N])}")

    print("\n【A. 放量阈值区分度】突破日(收盘创前20日新高)按量比分组的未来收益")
    for thr in (1.2, 1.5, 1.8, 2.0):
        hi = [b for b in allbreaks if b["vr"] >= thr]
        lo = [b for b in allbreaks if b["vr"] < thr]
        print(f"  量比阈值 {thr}:")
        for N in horizons:
            hw, hm = _winrate([b["fwd"][N] for b in hi]), _mean_pct([b["fwd"][N] for b in hi])
            lw, lm = _winrate([b["fwd"][N] for b in lo]), _mean_pct([b["fwd"][N] for b in lo])
            print(f"    {N}日: 放量(n={len(hi)}) 胜率{hw:.1f}%/均值{hm:+.2f}%  vs  "
                  f"缩量(n={len(lo)}) 胜率{lw:.1f}%/均值{lm:+.2f}%  → 差 {hm - lm:+.2f}%")

    print("\n【B. 突破容差 τ 的假突破率】突破后 N 日内跌回被突破位的比例(越低越实)")
    for N in horizons:
        row = []
        for tau in (0.0, 0.005, 0.0075, 0.01, 0.015):
            sub = [b for b in allbreaks if b["over"] >= tau]
            fr = (np.mean([b["back"][N] for b in sub]) * 100) if sub else float("nan")
            row.append(f"τ={tau*100:.2f}%: {fr:.0f}%(n={len(sub)})")
        print(f"  {N}日  " + " | ".join(row))
    print("  (对比放量确认:)")
    for N in horizons:
        vv = [b for b in allbreaks if b["vr"] >= 1.5]
        nn = [b for b in allbreaks if b["vr"] < 1.5]
        fv = (np.mean([b["back"][N] for b in vv]) * 100) if vv else float("nan")
        fn = (np.mean([b["back"][N] for b in nn]) * 100) if nn else float("nan")
        print(f"  {N}日  放量突破假突破率 {fv:.0f}%  vs  缩量突破 {fn:.0f}%")

    print("\n【C. 支撑/压力有效性】贴近(2%内)时未来收益 vs 基准")
    for N in horizons:
        sw, sm = _winrate([f[N] for f in allsup]), _mean_pct([f[N] for f in allsup])
        rw, rm = _winrate([f[N] for f in allres]), _mean_pct([f[N] for f in allres])
        bw = _winrate(allfwd[N])
        print(f"  {N}日: 贴支撑 上涨{sw:.1f}%/均值{sm:+.2f}%(n={len(allsup)})  "
              f"贴压力 上涨{rw:.1f}%/均值{rm:+.2f}%(n={len(allres)})  基准上涨{bw:.1f}%")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", default="5,10")
    args = ap.parse_args()
    run(tuple(int(x) for x in args.horizon.split(",")))
