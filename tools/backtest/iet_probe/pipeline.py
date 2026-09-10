"""IET 探针端到端编排:向量化构面板 → 温度序列 → 行业层回测。

性能:快照成分是**静态**的(code_industry 当前快照,与date无关) → 用 merge_asof 一次性把
每只票的 PE/PB/换手 as-of 对齐到目标交易日,再按行业 groupby 逐日中位/均值,避免逐日逐票 O(N·D)。
防未来:merge_asof direction=backward(只取≤date)、rolling分位因果、动量因果、T+1入场。

⚠️ 成分源=当前快照(用户拍板A),含**弱前视**;缓释见报告。PIT申万子样本另路交叉验证(build_pit_subsample_panel)。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

import numpy as np
import pandas as pd

from tools.analysis import industry_map
from tools.analysis.industry_temp import temperature as TEMP
from tools.backtest.iet_probe import data as D
from tools.backtest.iet_probe import run_probe as R
from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("iet_probe.pipeline")
_IET = THRESHOLDS["行业环境温度计"]
MIN_MEMBERS: int = _IET["IET_MIN_MEMBERS"]

WARMUP_START = "2024-07-01"     # 分位预热起点(给250日因果分位攒足样本,早于动量可用的2025-07)


# ————————————————————— 交易日历 —————————————————————
def trading_calendar(ref_code: str = "000001", start: str = WARMUP_START,
                     end: Optional[str] = None) -> list[str]:
    """用一只长历史个股的 K 线日期作交易日历(全市场共享交易日)。"""
    k = D._kline(ref_code)
    if k is None:
        raise RuntimeError(f"无法用 {ref_code} 建交易日历")
    ds = k["date"].tolist()
    ds = [d for d in ds if d >= start and (end is None or d <= end)]
    return sorted(ds)


# ————————————————————— 向量化 as-of 矩阵 —————————————————————
def _asof_matrix(codes: list[str], target_dates: list[str],
                 series_getter: Callable[[str], Optional[pd.DataFrame]],
                 value_col: str) -> pd.DataFrame:
    """[date × code] as-of 值矩阵(direction=backward,只取≤date,防未来)。"""
    td = pd.DataFrame({"date": pd.to_datetime(target_dates)})
    cols = {}
    for c in codes:
        s = series_getter(c)
        if s is None or value_col not in s.columns:
            continue
        ss = s[["date", value_col]].copy()
        ss["date"] = pd.to_datetime(ss["date"])
        ss = ss.dropna(subset=["date"]).sort_values("date")
        if ss.empty:
            continue
        merged = pd.merge_asof(td, ss, on="date", direction="backward")
        cols[c] = merged[value_col].to_numpy(dtype="float64")
    df = pd.DataFrame(cols, index=target_dates)
    return df


def build_panel_snapshot(membership: dict, target_dates: list[str],
                         min_members: int = MIN_MEMBERS) -> pd.DataFrame:
    """静态快照成分 → 向量化行业面板(long: date,industry,n_members,pe_median,pb_median,turnover_mean)。

    membership: {code: 申万一级名}(静态)。
    """
    # 按行业分组成分
    by_ind: dict[str, list[str]] = {}
    for code, ind in membership.items():
        if ind:
            by_ind.setdefault(ind, []).append(code)
    all_codes = [c for cs in by_ind.values() for c in cs]

    PE = _asof_matrix(all_codes, target_dates, D._valuation_series, "PE_TTM")
    PB = _asof_matrix(all_codes, target_dates, D._valuation_series, "PB")
    TURN = _asof_matrix(all_codes, target_dates, D._kline, "turnover")
    # 估值剔 ≤0
    PE = PE.where(PE > 0)
    PB = PB.where(PB > 0)

    rows = []
    for ind, codes in by_ind.items():
        pe_c = [c for c in codes if c in PE.columns]
        pb_c = [c for c in codes if c in PB.columns]
        tn_c = [c for c in codes if c in TURN.columns]
        pe_med = PE[pe_c].median(axis=1) if pe_c else pd.Series(np.nan, index=target_dates)
        pb_med = PB[pb_c].median(axis=1) if pb_c else pd.Series(np.nan, index=target_dates)
        tn_mean = TURN[tn_c].mean(axis=1) if tn_c else pd.Series(np.nan, index=target_dates)
        n_mem = (TURN[tn_c].notna().sum(axis=1) if tn_c
                 else pd.Series(0, index=target_dates))
        for d in target_dates:
            n = int(n_mem.get(d, 0))
            if n < min_members:
                continue
            rows.append({
                "date": d, "industry": ind, "n_members": n,
                "pe_median": _f(pe_med.get(d)), "pb_median": _f(pb_med.get(d)),
                "turnover_mean": _f(tn_mean.get(d)),
            })
    return pd.DataFrame(rows, columns=["date", "industry", "n_members",
                                       "pe_median", "pb_median", "turnover_mean"])


def _f(v):
    return None if v is None or pd.isna(v) else float(v)


# ————————————————————— PIT 申万子样本(交叉验证,防前视) —————————————————————
def build_pit_subsample_panel(universe: list[str], target_dates: list[str],
                              min_members: int = MIN_MEMBERS,
                              std: str = "申银万国行业分类标准") -> pd.DataFrame:
    """PIT 申万成分(industry_at 按申万std,历史时点归属,防前视)构面板。

    只覆盖有 history 文件的~25%票池,作稳健性交叉验证。成分随date变(SW重分类罕见)。
    为效率:先算每票的 PIT 归属变更点,再对每个 target_date 取 as-of 归属。
    """
    from tools.collectors import industry_history as IH

    # 每票的 (变更日, 申万一级) 升序列表(只保留能 to_sw 的)
    hist: dict[str, list[tuple[str, str]]] = {}
    for c in universe:
        try:
            items = IH.load_industry_history(c)
        except Exception:
            continue
        seq = []
        for it in items:
            if it.get("std") == std:
                sw = industry_map.to_sw(it.get("industry"))
                if sw:
                    seq.append((str(it["date"])[:10], sw))
        if seq:
            seq.sort()
            hist[c] = seq
    if not hist:
        return pd.DataFrame(columns=["date", "industry", "n_members",
                                     "pe_median", "pb_median", "turnover_mean"])

    codes = list(hist.keys())
    PE = _asof_matrix(codes, target_dates, D._valuation_series, "PE_TTM").where(lambda x: x > 0)
    PB = _asof_matrix(codes, target_dates, D._valuation_series, "PB").where(lambda x: x > 0)
    TURN = _asof_matrix(codes, target_dates, D._kline, "turnover")

    def _ind_at(seq, d):
        cur = None
        for chg, sw in seq:
            if chg <= d:
                cur = sw
            else:
                break
        return cur

    rows = []
    for d in target_dates:
        # 当日各票 PIT 归属
        buckets: dict[str, list[str]] = {}
        for c in codes:
            ind = _ind_at(hist[c], d)
            if ind:
                buckets.setdefault(ind, []).append(c)
        for ind, cs in buckets.items():
            tn_c = [c for c in cs if c in TURN.columns and pd.notna(TURN.at[d, c])]
            if len(tn_c) < min_members:
                continue
            pe_c = [c for c in cs if c in PE.columns and pd.notna(PE.at[d, c])]
            pb_c = [c for c in cs if c in PB.columns and pd.notna(PB.at[d, c])]
            rows.append({
                "date": d, "industry": ind, "n_members": len(tn_c),
                "pe_median": _f(PE.loc[d, pe_c].median()) if pe_c else None,
                "pb_median": _f(PB.loc[d, pb_c].median()) if pb_c else None,
                "turnover_mean": _f(TURN.loc[d, tn_c].mean()) if tn_c else None,
            })
    return pd.DataFrame(rows, columns=["date", "industry", "n_members",
                                       "pe_median", "pb_median", "turnover_mean"])


# ————————————————————— 行业指数远期收益 —————————————————————
def industry_forward_returns(industries: list[str], horizons=R.HORIZONS) -> dict:
    """各行业 board_kline → forward_returns。返回 {industry: DataFrame[date,h,r]}。"""
    from tools.collectors import board
    out = {}
    for ind in industries:
        sw = industry_map.to_sw(ind) or ind
        try:
            bk = board.load_board_kline(sw)
        except Exception:
            continue
        if bk is None or bk.empty:
            continue
        s = bk[["date", "close"]].copy()
        s["date"] = pd.to_datetime(s["date"]).dt.strftime("%Y-%m-%d")
        s = s.dropna().sort_values("date").set_index("date")["close"]
        out[ind] = R.forward_returns(s, horizons)
    return out


# ————————————————————— 端到端 —————————————————————
def run(panel_df: pd.DataFrame, *, win: int = None, val_cut: float = None,
        turn_cut: float = None, horizons=R.HORIZONS, label: str = "") -> dict:
    """面板 → 温度序列 → 行业层 IC/分层/子样本/闸门。参数可覆盖(敏感性)。"""
    kw = {}
    if win is not None:
        kw["win"] = win
    if val_cut is not None:
        kw["val_cut"] = val_cut
    if turn_cut is not None:
        kw["turn_cut"] = turn_cut
    temp = TEMP.build_temperature_series(panel_df, D.rs_momentum_of, **kw)
    industries = sorted(temp["industry"].unique())
    ret = industry_forward_returns(industries, horizons)
    frame = R.build_eval_frame(temp[["date", "industry", "k"]], ret, horizons)
    ic = R.rank_ic_table(frame, horizons)
    layers = {h: R.layer_stats(frame, h) for h in horizons}
    subs = {h: R.subsample_sign_stability(frame, h, by="year") for h in horizons}
    verdict = R.gate_verdict(ic, subs.get(max(horizons), {}))
    return {
        "label": label, "n_temp_rows": int(temp["k"].notna().sum()),
        "n_frame_rows": len(frame),
        "date_range": (frame["date"].min() if len(frame) else None,
                       frame["date"].max() if len(frame) else None),
        "ic": ic, "layers": layers, "subsample": subs, "verdict": verdict,
        "_temp": temp, "_frame": frame,
    }
