"""口径B(deep, 2018+):个股聚合重建行业等权指数,突破 native 申万指数 ~1.5yr 的功效天花板。

⚠️ 前视偏差(如实标注):成分=当前快照(code_industry),倒填历史 → **弱前视**(退市票缺失、
今日归属安到过去)。故口径B样本长(有功效)但不干净;与口径A(native 短而干净)**并报,以一致为准**。

重建:行业等权日收益 = 成分股当日 pct_chg 的等权均值(≥MIN_MEMBERS 只) → cumprod 成等权指数。
四维:估值/拥挤复用 build_panel_snapshot(deep 深史);动量=等权指数 vs 沪深300 的因果 RS-Mom;
波动=等权指数已实现波动因果分位。前瞻收益=等权指数 close→close(T+1入场,防未来)。
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from tools.analysis import industry_map, rrg
from tools.analysis.industry_temp import temperature as TEMP
from tools.analysis.industry_temp import volatility as VOL
from tools.backtest.iet_probe import data as D
from tools.backtest.iet_probe import pipeline as PIPE
from tools.backtest.iet_probe import run_probe as R

logger = logging.getLogger("industry_thermometer.deep")
MIN_MEMBERS = PIPE.MIN_MEMBERS


# ————————————————————— 等权行业指数重建 —————————————————————
def build_ew_returns(membership: dict, start: str, end: Optional[str] = None) -> dict:
    """{industry: pd.Series(date→等权日收益%)}。当日成分<MIN_MEMBERS 的日剔除。"""
    by_ind: dict[str, list[str]] = {}
    for code, ind in membership.items():
        if ind:
            by_ind.setdefault(ind, []).append(code)
    out = {}
    for ind, codes in by_ind.items():
        frames = []
        for c in codes:
            k = D._kline(c)
            if k is None or "pct_chg" not in k.columns:
                continue
            s = k[["date", "pct_chg"]]
            s = s[s["date"] >= start]
            if end:
                s = s[s["date"] <= end]
            if not s.empty:
                frames.append(s)
        if not frames:
            continue
        allf = pd.concat(frames, ignore_index=True)
        grp = allf.groupby("date")["pct_chg"]
        ew = grp.mean()
        cnt = grp.count()
        ew = ew[cnt >= MIN_MEMBERS].sort_index()
        if not ew.empty:
            out[ind] = ew.astype(float)
    return out


def ew_index(ew_ret: pd.Series) -> pd.Series:
    """等权日收益% → 等权指数 level(cumprod,起点100)。"""
    return 100.0 * (1.0 + ew_ret / 100.0).cumprod()


# ————————————————————— 深史动量(等权指数 vs 沪深300) —————————————————————
def _rs_momentum_map(level: pd.Series, bench: pd.Series) -> dict:
    """等权指数 level + 沪深300 close → 每日因果 RS-Momentum {date: mom}。"""
    b = pd.DataFrame({"date": level.index, "close_b": level.values})
    k = pd.DataFrame({"date": bench.index, "close_k": bench.values})
    m = b.merge(k, on="date").sort_values("date")
    if len(m) < rrg._min_bars() + 5:
        return {}
    dates = m["date"].tolist()
    try:
        rs = rrg.rs_line(m["close_b"].tolist(), m["close_k"].tolist())
        ratio = rrg.rs_ratio_series(rs)
        mom = rrg.rs_momentum_series(ratio)
    except Exception:
        return {}
    if not mom:
        return {}
    offset = len(dates) - len(mom)
    if offset < 0:
        return {}
    return {dates[offset + j]: float(mom[j]) for j in range(len(mom))}


def build_market_ew(membership: dict, start: str, end: Optional[str] = None) -> pd.Series:
    """全A等权市场基准(2018+):所有票 pct_chg 等权均值 → cumprod level。

    ⚠️ 用重建的 EW 全A 而非 沪深300 指数——因本仓 index_kline(沪深300)同为 ~1.5yr 滚动窗,
    深史 RS 相对强度需一条深史基准;EW 全A 与各行业 EW 指数口径一致(相对强度=行业 vs 市场)。
    """
    frames = []
    for c in membership:
        k = D._kline(c)
        if k is None or "pct_chg" not in k.columns:
            continue
        s = k[["date", "pct_chg"]]
        s = s[s["date"] >= start]
        if end:
            s = s[s["date"] <= end]
        if not s.empty:
            frames.append(s)
    if not frames:
        return pd.Series(dtype="float64")
    allf = pd.concat(frames, ignore_index=True)
    ew = allf.groupby("date")["pct_chg"].mean().sort_index().astype(float)
    return ew_index(ew)


# ————————————————————— 深史 4 维面板 —————————————————————
def build_dim_panel_deep(membership: dict, dates: list[str], *,
                         horizons=R.HORIZONS,
                         vol_win: int = VOL.VOL_WIN,
                         pctile_win: int = TEMP.PCTILE_WIN,
                         pctile_min: int = TEMP.PCTILE_MIN) -> tuple[pd.DataFrame, dict]:
    """返回 (dim_panel[date,industry,dim,value,ab], ret_by_industry)。

    估值/拥挤:build_panel_snapshot(deep);动量/波动:等权指数。前瞻收益:等权指数。
    """
    start, end = dates[0], dates[-1]
    ew_ret = build_ew_returns(membership, start, end)
    ew_idx = {ind: ew_index(r) for ind, r in ew_ret.items()}
    bench = build_market_ew(membership, start, end)   # 深史 EW 全A 基准(非 ~1.5yr 的沪深300指数)

    # 动量映射(每行业一次)
    mom_maps = {ind: _rs_momentum_map(lvl, bench) for ind, lvl in ew_idx.items()}

    def rs_mom_of(industry: str, date: str) -> Optional[float]:
        mp = mom_maps.get(industry)
        if not mp:
            return None
        keys = [d for d in mp if d <= date]
        return mp[max(keys)] if keys else None

    # 估值/换手面板(deep) → 温度序列(估值/换手因果分位 + 深史动量)
    panel = PIPE.build_panel_snapshot(membership, dates)
    if panel.empty:
        return pd.DataFrame(columns=["date", "industry", "dim", "value", "ab"]), {}
    temp = TEMP.build_temperature_series(panel, rs_mom_of, win=pctile_win,
                                         min_periods=pctile_min).copy()
    temp["mom_raw"] = [rs_mom_of(i, d) for i, d in zip(temp["industry"], temp["date"])]

    # 波动分位(等权指数已实现波动)
    vol_lookup = {}
    for ind, lvl in ew_idx.items():
        vp = VOL.vol_pctile_series(lvl, vol_win=vol_win, pctile_win=pctile_win,
                                   pctile_min=pctile_min)
        vol_lookup[ind] = vp.dropna()

    def _vol_at(ind, date):
        vp = vol_lookup.get(ind)
        if vp is None or vp.empty:
            return None
        sub = vp[vp.index <= date]
        return float(sub.iloc[-1]) if not sub.empty else None

    rows = []
    for _i, r in temp.iterrows():
        d, ind = r["date"], r["industry"]
        vp = _vol_at(ind, d)
        rows.append({"date": d, "industry": ind, "dim": "估值",
                     "value": _f(r.get("val_pctile")), "ab": r.get("val_ab")})
        rows.append({"date": d, "industry": ind, "dim": "动量",
                     "value": _f(r.get("mom_raw")), "ab": r.get("mom_ab")})
        rows.append({"date": d, "industry": ind, "dim": "拥挤",
                     "value": _f(r.get("turn_pctile")), "ab": r.get("turn_ab")})
        rows.append({"date": d, "industry": ind, "dim": "波动",
                     "value": _f(vp), "ab": VOL.ab_volatility(vp)})
    dim_panel = pd.DataFrame(rows, columns=["date", "industry", "dim", "value", "ab"])

    # 前瞻收益(等权指数)
    ret = {ind: R.forward_returns(lvl.copy(), horizons) for ind, lvl in ew_idx.items()}
    return dim_panel, ret


def _f(v):
    return None if v is None or pd.isna(v) else float(v)
