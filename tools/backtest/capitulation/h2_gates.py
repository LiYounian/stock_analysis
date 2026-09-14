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


GATES = ["G_MA5", "G_MA3", "G_prevhigh", "G_volup_close"]


def build_gate_panels(panels) -> dict[str, pd.DataFrame]:
    """向量化 4 档闸门的逐日触发布尔面板(date×code)。NaN → False。

    因果:每格仅用当日面板值(面板本身由 ≤当日 K线算得),无未来泄露。
    """
    close, openp = panels["close"], panels["open"]
    ma3, ma5 = panels["ma3"], panels["ma5"]
    vr, ph = panels["vol_ratio"], panels["prev_high"]
    volup = vr > VOL_RATIO_GATE
    return {
        "G_MA5": (volup & (close >= ma5)).fillna(False),
        "G_MA3": (volup & (close >= ma3)).fillna(False),
        "G_prevhigh": (close >= ph).fillna(False),
        "G_volup_close": (volup & (close > openp)).fillna(False),
    }


def run_h2(panels, oversold_panel, cap_dates, horizons=(1, 5),
           window=FWD_WINDOW, oos_start=None, book=None):
    """遍历 capitulation 底部超跌票,对每档闸门找首触发 + 前向 α + 首段捕获。"""
    close_panel = panels["close"]
    if book is None:
        book = ForwardBook(close_panel, horizons, lag=1)
    gate_panels = build_gate_panels(panels)
    idx = close_panel.index
    n = len(idx)
    pos = {d: i for i, d in enumerate(idx)}
    # 每档:触发次数、前向 α/收益样本(stock 级)、首段捕获、以及按 capitulation 日分组的 α(day 级)
    rec = {g: {"fires": 0, "alpha": {N: [] for N in horizons},
               "ret": {N: [] for N in horizons}, "seg_capture": [],
               "day_alpha": {N: {} for N in horizons}} for g in GATES}
    n_samples = 0
    for d in cap_dates:
        d = pd.Timestamp(d)
        if oos_start and d < pd.Timestamp(oos_start):
            continue
        if d not in pos:
            continue
        di = pos[d]
        entry_bottom_i = di + 1              # 底部 t+1 进场价(首段捕获基准)
        if entry_bottom_i >= n:
            continue
        subset = list(oversold_panel.columns[oversold_panel.loc[d].values]) \
            if d in oversold_panel.index else []
        base_row = close_panel.iloc[entry_bottom_i]
        # 窗口的行区间(含边界),各闸门在该区间内取该码列
        w_hi = min(di + window, n - 1)
        close_col_all = close_panel  # 复用
        for c in subset:
            base_px = base_row.get(c, np.nan)
            if pd.isna(base_px) or not base_px:
                continue
            n_samples += 1
            for g in GATES:
                col = gate_panels[g][c].iloc[di + 1:w_hi + 1]
                hit = col[col]
                if len(hit) == 0:
                    continue
                trig_date = hit.index[0]
                fi = pos[trig_date]
                rec[g]["fires"] += 1
                trig_px = close_panel[c].iloc[fi]     # 单列标量,避免整行materialize
                if not pd.isna(trig_px):
                    rec[g]["seg_capture"].append((trig_px / base_px - 1.0) * 100.0)
                for N in horizons:
                    sa = book.stock_alpha(trig_date, c, N)
                    if sa is None:
                        continue
                    rec[g]["ret"][N].append(sa["r"])
                    rec[g]["alpha"][N].append(sa["alpha"])
                    rec[g]["day_alpha"][N].setdefault(d, []).append(sa["alpha"])
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
            row[f"alpha_{N}_mean"] = round(float(np.mean(al)), 4) if al else None
            row[f"ret_{N}_mean"] = round(float(np.mean(rt)), 4) if rt else None
            row[f"precision_{N}"] = round(float(np.mean([a > 0 for a in al])), 4) if al else None
            row[f"n_{N}"] = len(al)
            # day 级(诚实显著性:先按 capitulation 日取均值,再 bootstrap 跨日 → 修正同日相关)
            day_means = [float(np.mean(v)) for v in gd["day_alpha"][N].values() if v]
            dmean, lo, hi, p = bootstrap_ci(day_means) if day_means else (None, None, None, None)
            row[f"alpha_{N}_day_mean"] = round(dmean, 4) if dmean is not None else None
            row[f"alpha_{N}_day_ci95"] = (lo, hi)
            row[f"alpha_{N}_day_p"] = p
            row[f"n_{N}_days"] = len(day_means)
        out["gates"][g] = row
    return out
