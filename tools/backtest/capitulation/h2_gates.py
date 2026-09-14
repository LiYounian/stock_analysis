"""H2 · 更早触发闸门:超跌反抽样本上,对比不同触发口径捕捉反抽首段的收益与精确率。

问题:现闸门「放量站上 MA5」对超跌反抽太滞后(反抽首波常在 MA5 下方走完)。检验 4 档
(可从 kline 算的)口径,谁在可接受精确率下多捕捉反抽首段:
  G_MA5(基线): 量比>1.5 且 close≥MA5   G_MA3: 量比>1.5 且 close≥MA3
  G_prevhigh: close≥前日高               G_volup_close: 量比>1.5 且 close>open(放量收阳)
⚠️ 主买翻正(G_mainbuy)需 fundflow/tick 逐笔主买,项目无该目录 → 本轮**不可测**,不臆造。

样本:capitulation 底部日 d 的超跌票 c。向后扫 window(默认 10 交易日)找每档**首次触发日** f,
从 f 的 t+1 进场算前向 r_N/α_N;精确率=触发中前向 α_N>0 占比;首段捕获=从 d+1 到 f 的区间收益
(量化 MA5 相对更早口径系统性放弃的首段)。防偷看:面板指标皆 ≤当日,触发只用当日及历史。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.backtest.capitulation.forward import ForwardBook, bootstrap_ci

VOL_RATIO_GATE = 1.5      # 放量阈值(镜像 technical 的 vol_state / _reversal 放量反包)
FWD_WINDOW = 10           # 底部后扫触发的最大交易日窗口


def _gate_fired(panels, code, f_idx):
    """返回该码在面板第 f_idx 行(某交易日)各闸门是否触发的 dict(NaN→False)。"""
    def g(field):
        v = panels[field].iloc[f_idx].get(code, np.nan)
        return v
    close = g("close"); openp = g("open"); ma3 = g("ma3"); ma5 = g("ma5")
    vr = g("vol_ratio"); ph = g("prev_high")
    if pd.isna(close):
        return None
    volup = (not pd.isna(vr)) and vr > VOL_RATIO_GATE
    return {
        "G_MA5": bool(volup and not pd.isna(ma5) and close >= ma5),
        "G_MA3": bool(volup and not pd.isna(ma3) and close >= ma3),
        "G_prevhigh": bool(not pd.isna(ph) and close >= ph),
        "G_volup_close": bool(volup and not pd.isna(openp) and close > openp),
    }


GATES = ["G_MA5", "G_MA3", "G_prevhigh", "G_volup_close"]


def run_h2(panels, oversold_panel, cap_dates, horizons=(1, 5),
           window=FWD_WINDOW, oos_start=None, book=None):
    """遍历 capitulation 底部超跌票,对每档闸门找首触发 + 前向 α + 首段捕获。"""
    close_panel = panels["close"]
    if book is None:
        book = ForwardBook(close_panel, horizons, lag=1)
    idx = close_panel.index
    n = len(idx)
    # 每档:触发次数、前向 α/收益样本、首段捕获样本
    rec = {g: {"fires": 0, "alpha": {N: [] for N in horizons},
               "ret": {N: [] for N in horizons}, "seg_capture": []} for g in GATES}
    n_samples = 0
    for d in cap_dates:
        d = pd.Timestamp(d)
        if oos_start and d < pd.Timestamp(oos_start):
            continue
        if d not in idx:
            continue
        di = idx.get_loc(d)
        if di + 1 >= n:
            continue
        subset = list(oversold_panel.columns[oversold_panel.loc[d].values]) \
            if d in oversold_panel.index else []
        entry_bottom_i = di + 1              # 底部 t+1 进场价(首段捕获的基准)
        if entry_bottom_i >= n:
            continue
        for c in subset:
            base_px = close_panel.iloc[entry_bottom_i].get(c, np.nan)
            if pd.isna(base_px):
                continue
            n_samples += 1
            # 扫窗口找每档首触发
            fired_at = {g: None for g in GATES}
            for step in range(1, window + 1):
                f_idx = di + step
                if f_idx >= n:
                    break
                gf = _gate_fired(panels, c, f_idx)
                if gf is None:
                    continue
                for g in GATES:
                    if fired_at[g] is None and gf[g]:
                        fired_at[g] = f_idx
                if all(v is not None for v in fired_at.values()):
                    break
            for g in GATES:
                fi = fired_at[g]
                if fi is None:
                    continue
                rec[g]["fires"] += 1
                trig_date = idx[fi]
                # 首段捕获:从底部 t+1 到触发日收盘的区间收益(pp)
                trig_px = close_panel.iloc[fi].get(c, np.nan)
                if not pd.isna(trig_px) and base_px:
                    rec[g]["seg_capture"].append((trig_px / base_px - 1.0) * 100.0)
                # 触发日 t+1 进场的前向 α / 收益(ForwardBook O(1) 查询)
                for N in horizons:
                    sa = book.stock_alpha(trig_date, c, N)
                    if sa is None:
                        continue
                    rec[g]["ret"][N].append(sa["r"])
                    rec[g]["alpha"][N].append(sa["alpha"])
    return _summarize_h2(rec, horizons, n_samples)


def _summarize_h2(rec, horizons, n_samples):
    out = {"n_samples": n_samples, "gates": {}}
    for g in GATES:
        gd = rec[g]
        fires = gd["fires"]
        row = {"fires": fires,
               "fire_rate": round(fires / n_samples, 4) if n_samples else None,
               "seg_capture_mean": round(float(np.mean(gd["seg_capture"])), 4) if gd["seg_capture"] else None}
        for N in horizons:
            al = gd["alpha"][N]
            rt = gd["ret"][N]
            mean_a, lo, hi, p = bootstrap_ci(al) if al else (None, None, None, None)
            row[f"alpha_{N}_mean"] = round(float(np.mean(al)), 4) if al else None
            row[f"alpha_{N}_ci95"] = (lo, hi)
            row[f"alpha_{N}_p"] = p
            row[f"ret_{N}_mean"] = round(float(np.mean(rt)), 4) if rt else None
            row[f"precision_{N}"] = round(float(np.mean([a > 0 for a in al])), 4) if al else None
            row[f"n_{N}"] = len(al)
        out["gates"][g] = row
    return out
