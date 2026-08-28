"""策略3 箱体形态 根因诊断脚本(临时诊断产物,不改生产 signal)。

目的:坐实"箱体放量突破为何显著负",并用变体实验判"救/改/删"。
方法:复用 eval_v3 打分/聚合(T+1 入场、命中锚 close[T]、按日聚类 bootstrap),
     只替换 SELECT 逻辑为若干**纯 as-of(只用 ≤t)**变体,同宇宙同日历横比。

变体(全部防未来函数,只用 kdf.iloc[:t+1]):
  V0_base      : 现网箱体(detect_box 默认参数)——复现基线。
  V1_confirm1d : 突破次日确认(t-1 达标 且 close[t] 仍站上箱顶)→ 晚 1 日入场。
  V2_pullback  : 突破后 L 日内回踩箱顶再入(low[t]≤箱顶*1.01 且 close[t]>箱顶)。
  V3_vol25     : 加强放量(突破放量倍数 1.5→2.5),其余同 V0。
  V4_tight8    : 更窄箱体(高度上限 12%→8%),其余同 V0。
  V5_vol25tight: V3+V4 叠加(强量能 + 更窄箱)。

对每个变体在 horizons (1,5,10,20) 打分聚合,打印 5 日为主口径的
超额%/聚类p/命中率/盈亏比/样本,兼看 1/10/20 日。
"""
from __future__ import annotations

import sys
import time

import numpy as np
import pandas as pd

from tools.analysis.pattern_screener.pattern import detect_box
from tools.backtest.eval_v3 import aggregate, prices, replay_source, scoring, schema
from tools.config.strategy import THRESHOLDS
from tools.collectors import market

BOX = THRESHOLDS["形态选股"]["箱体"]
WIN = int(BOX["窗口"])
HORIZONS = (1, 5, 10, 20)
PULLBACK_L = 5

# cfg 变体(detect_box 读 cfg["箱体"])
CFG_BASE = {"箱体": dict(BOX)}
CFG_VOL25 = {"箱体": {**BOX, "突破放量倍数": 2.5}}
CFG_TIGHT8 = {"箱体": {**BOX, "高度上限%": 8.0}}
CFG_VOL25TIGHT = {"箱体": {**BOX, "突破放量倍数": 2.5, "高度上限%": 8.0}}


VARIANTS = ("V0_base", "V1_confirm1d", "V2_pullback",
            "V3_vol25", "V4_tight8", "V5_vol25tight")
_HGT = float(BOX["高度上限%"])        # 12.0
_HGT8 = 8.0
_BRK = float(BOX["突破幅度%"]) / 100.0  # 0.03
_V15 = float(BOX["突破放量倍数"])       # 1.5
_V25 = 2.5


def _rolling(arr, W, fn):
    """长度 n 数组 → 对每个 t 的窗 arr[t-W:t](t-1 结尾)聚合值,前 W 个记 nan。与 detect_box 同窗。"""
    from numpy.lib.stride_tricks import sliding_window_view
    n = len(arr)
    out = np.full(n, np.nan)
    if n >= W:
        sw = sliding_window_view(arr, W)          # (n-W+1, W):sw[i]=arr[i:i+W]
        agg = fn(sw, axis=1)                       # 窗 arr[i:i+W]
        out[W:] = agg[0:n - W]                     # t=W..n-1 用 i=t-W=0..n-1-W
    return out


def scan_ticker(kdf, dates_idx):
    """向量化(numpy 滑窗)复现 detect_box 四参数版 + 派生 confirm/pullback。

    detect_box 对第 t 根:base_hi=max(high[t-W:t]),base_lo=min(low[t-W:t]),
    vol_mean=mean(vol[t-W:t]),突破=close[t]>base_hi*(1+突破幅度%),放量=vol[t]>vol_mean*倍数,
    窄幅=(base_hi-base_lo)/base_lo*100≤高度上限%。四个 cfg 只在阈值/倍数不同 → 共用滑窗,一次算完。
    返回 dict[variant]->list[date]。语义与 tools.pipeline.screen_box.signal_at 完全等价(已用小样本校验)。
    """
    n = len(kdf)
    high = kdf["high"].to_numpy(float)
    low = kdf["low"].to_numpy(float)
    close = kdf["close"].to_numpy(float)
    vol = kdf["volume"].to_numpy(float)
    bh = _rolling(high, WIN, np.max)
    bl = _rolling(low, WIN, np.min)
    vm = _rolling(vol, WIN, np.mean)
    with np.errstate(invalid="ignore", divide="ignore"):
        height = (bh - bl) / bl * 100.0
        broke = close > bh * (1 + _BRK)
        vok15 = (vm > 0) & (vol > vm * _V15)
        vok25 = (vm > 0) & (vol > vm * _V25)
    tight12 = height <= _HGT
    tight8 = height <= _HGT8
    base_ok = tight12 & broke & vok15 & np.isfinite(bh)      # V0
    v3 = tight12 & broke & vok25 & np.isfinite(bh)
    v4 = tight8 & broke & vok15 & np.isfinite(bh)
    v5 = tight8 & broke & vok25 & np.isfinite(bh)
    box_top = np.where(base_ok, bh, np.nan)

    out = {k: [] for k in VARIANTS}
    for d, t in dates_idx:
        if t < WIN:               # 与 signal_at/find_signals_box 起点一致(t≥WIN)
            continue
        if base_ok[t]:
            out["V0_base"].append(d)
        if v3[t]:
            out["V3_vol25"].append(d)
        if v4[t]:
            out["V4_tight8"].append(d)
        if v5[t]:
            out["V5_vol25tight"].append(d)
        # V1 confirm:突破在 t-1,且 close[t] 仍站上该箱顶(晚 1 日入场)
        if t - 1 >= 0 and base_ok[t - 1] and close[t] > box_top[t - 1]:
            out["V1_confirm1d"].append(d)
        # V2 pullback:[t-L,t-1] 最近一次突破的箱顶,今日回踩(low≤箱顶*1.01)且收在其上
        for j in range(t - 1, max(WIN, t - PULLBACK_L) - 1, -1):
            if base_ok[j]:
                bt = box_top[j]
                if low[t] <= bt * 1.01 and close[t] > bt:
                    out["V2_pullback"].append(d)
                break
    return out


def build_records(universe_n=800, lookback_days=None, seed=20260828):
    universe = replay_source.sample_universe(universe_n, seed)
    calendar = replay_source.build_calendar(universe)
    replay_dates = calendar[-lookback_days:] if lookback_days else calendar
    date_set = set(replay_dates)

    per_variant = {k: [] for k in VARIANTS}
    scanned = 0
    for code in universe:
        try:
            kdf = market.load_kline(code).reset_index(drop=True)
        except Exception:
            continue
        if "date" not in kdf.columns or len(kdf) < WIN + 2:
            continue
        dmap = {str(x)[:10]: i for i, x in enumerate(kdf["date"].tolist())}
        dates_idx = [(d, dmap[d]) for d in replay_dates if d in dmap]
        if not dates_idx:
            continue
        scanned += 1
        res = scan_ticker(kdf, dates_idx)
        for v, dlist in res.items():
            for d in dlist:
                per_variant[v].append({
                    "strategy_id": v, "strategy": v, "pred_date": d,
                    "code": code, "direction": 1, "rank_score": np.nan,
                    "source": "replay", "stype": schema.DIRECTIONAL,
                    "replayable": True})
    meta = {"宇宙抽样票数": len(universe), "有效扫描票数": scanned,
            "回放日范围": [replay_dates[0], replay_dates[-1]] if replay_dates else None}
    return per_variant, universe, calendar, meta


def _lean_metrics(scored_h, ur, h):
    """一个 (变体,horizon) 全史直算:n/命中%/盈亏比/均值%/中位%/即死占比/超额%/聚类p。

    仅一次 cluster_bootstrap_excess(B=2000),避免 5 窗全维聚合的重复 bootstrap。
    即死占比 = 单笔 h 日收益 ≤ -9%(近跌停,追高见光死代理)的比例。
    """
    from tools.backtest.eval_v3 import stats as _st
    sub = scored_h.dropna(subset=["hit_end"])
    r = sub["r"].to_numpy(float)
    r = r[np.isfinite(r)]
    n = len(r)
    if n == 0:
        return None
    wins, losses = r[r > 0], r[r < 0]
    pf = round(float(wins.mean()) / abs(float(losses.mean())), 2) if len(losses) else None
    pred_days = sorted(sub["pred_date"].unique().tolist())
    strat_day, mkt_day = [], []
    for d in pred_days:
        gd = sub[sub["pred_date"] == d]["r"].to_numpy(float)
        gd = gd[np.isfinite(gd)]
        u = ur.get((d, h), np.array([]))
        strat_day.append(gd)
        mkt_day.append(float(u.mean()) if len(u) else None)
    ex = _st.cluster_bootstrap_excess(strat_day, mkt_day, B=2000)
    return {
        "n": n, "命中%": round(float((sub["hit_end"] > 0).mean()) * 100, 1),
        "盈亏比": pf, "均值%": round(float(r.mean()), 3),
        "中位%": round(float(np.median(r)), 3),
        "即死%<-9": round(float((r <= -9.0).mean()) * 100, 1),
        "P90%": round(float(np.percentile(r, 90)), 2),
        "超额%": ex.get("excess"), "聚类p": ex.get("p_value"), "聚类日数": ex.get("n_days"),
    }


def main():
    t0 = time.time()
    universe_n = int(sys.argv[1]) if len(sys.argv) > 1 else 800
    lookback = None
    per_variant, universe, calendar, meta = build_records(universe_n, lookback)
    print("META:", meta, "elapsed", round(time.time() - t0, 1), flush=True)
    book = prices.PriceBook()

    # 预取全宇宙各 horizon 的市场基准(用所有出现过的预测日)
    all_dates = sorted({r["pred_date"] for recs in per_variant.values() for r in recs})
    ur = scoring.universe_returns(all_dates, HORIZONS, universe, book)
    print("universe_returns done", round(time.time() - t0, 1), flush=True)

    print("\n变体          h   n     超额%    聚类p   命中%  盈亏比  均值%   中位%  即死%<-9  P90%   信号", flush=True)
    rows = []
    for v, recs in per_variant.items():
        n_sig = len(recs)
        if n_sig == 0:
            print(f"{v}: 0 信号", flush=True)
            continue
        preds = schema.make_frame(recs)
        scored = scoring.score_predictions(preds, book, HORIZONS)
        for h in HORIZONS:
            gh = scored[(scored["h"] == h) & (scored["matured"])]
            m = _lean_metrics(gh, ur, h)
            if m is None:
                continue
            m.update({"变体": v, "h": h, "出信号": n_sig})
            rows.append(m)
            print(f"{v:14s}{h:2d} {m['n']:5d} {str(m['超额%']):>7s} {str(m['聚类p']):>6s} "
                  f"{m['命中%']:5.1f} {str(m['盈亏比']):>5s} {m['均值%']:7.3f} {m['中位%']:6.3f} "
                  f"{m['即死%<-9']:6.1f}   {m['P90%']:6.2f} {n_sig:6d}", flush=True)
    pd.DataFrame(rows).to_csv("/tmp/box3_diag.csv", index=False)
    print("\n总耗时", round(time.time() - t0, 1), "s → /tmp/box3_diag.csv", flush=True)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    main()
