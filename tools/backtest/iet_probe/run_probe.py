"""IET 探针·行业层回测核心。

流程(端到端串联在 build_and_run,成分源确定后接):
  温度序列 temp_df[date,industry,k,(分维信号)]  +  行业指数远期收益
    → rank-IC(均值/ICIR/t, 按 h)  + 分层单调(k=0..3 四档) + 多空net + 子样本符号稳定
    → 对闸门 IET_IC_BAR / IET_T_BAR 判定。

收益口径(规格 §4.1):信号日 T(只用≤T)→ T+1 入场 → 持有 h 日 → forward return,
行业指数 close→close;只算已到期(未到期不纳入)。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from tools.backtest.eval_v3 import stats as ev_stats
from tools.config.strategy import THRESHOLDS

_IET = THRESHOLDS["行业环境温度计"]
IC_BAR: float = _IET["IET_IC_BAR"]
T_BAR: float = _IET["IET_T_BAR"]
HORIZONS = (1, 5, 20)
N_LAYERS = 4                       # k∈{0,1,2,3}
IET_COST_ROUNDTRIP = 0.2           # 多空组合往返成本%(沿用 eval_v3 exit_sim COST_GRID 上档,行业指数层)
_TRADING_DAYS = 244.0              # 年化


# ————————————————————— 远期收益(因果, T+1 入场) —————————————————————
def forward_returns(close: pd.Series, horizons=HORIZONS) -> pd.DataFrame:
    """行业指数收盘序列 → 各信号日 T 的远期收益。

    close: index=date(升序), 值=行业指数收盘。
    fwd_r(T,h) = close[T+1+h]/close[T+1] - 1  (T+1 入场收盘价,持有 h 日,close→close)。
    只输出已到期(T+1+h 存在)的行;防未来。
    返回 long DataFrame[date, h, r](date=信号日 T)。
    """
    close = close.dropna()
    dates = list(close.index)
    vals = close.to_numpy(dtype="float64")
    n = len(vals)
    rows = []
    for i in range(n):
        entry_i = i + 1                        # T+1 入场
        if entry_i >= n:
            continue
        entry = vals[entry_i]
        if not np.isfinite(entry) or entry <= 0:
            continue
        for h in horizons:
            exit_i = entry_i + h
            if exit_i >= n:                     # 未到期,防未来剔除
                continue
            r = vals[exit_i] / entry - 1.0
            rows.append({"date": dates[i], "h": h, "r": float(r)})
    return pd.DataFrame(rows, columns=["date", "h", "r"])


# ————————————————————— rank-IC —————————————————————
def rank_ic_table(frame: pd.DataFrame, horizons=HORIZONS) -> dict:
    """截面 rank-IC(温度 k vs 行业远期收益),按 h。

    frame: long DataFrame,列须含 [date, industry, k, h, r]。
    每个 (h, date) 截面对 (k 数组, r 数组) 求 Spearman = 当日 IC → 复用 ev_stats.rank_ic。
    返回 {h: {mean_ic, icir, t_stat, p_value, n_days, pos_ratio}}。
    """
    out = {}
    for h in horizons:
        sub = frame[frame["h"] == h].dropna(subset=["k", "r"])
        pairs = []
        for _date, g in sub.groupby("date"):
            pairs.append((g["k"].to_numpy(float), g["r"].to_numpy(float)))
        out[h] = ev_stats.rank_ic(pairs, method="spearman")
    return out


# ————————————————————— 分层单调 + 多空 —————————————————————
def layer_stats(frame: pd.DataFrame, h: int, cost_roundtrip: float = IET_COST_ROUNDTRIP) -> dict:
    """按 k=0..3 四档分层,各档远期收益均值 + 单调性 + 多空 net 年化。

    不预设方向(探针探"有无预测力");多空按数据显示的 IC 方向构造:
      IC>0(k大收益高,动量延续)→ 多 k=3、空 k=0;IC<0(k大收益低,过热反转)→ 多 k=0、空 k=3。
    多空 spread 扣往返成本;按 h 日持有折年化。
    返回 {layer_mean:{k:收益均值%}, layer_n:{k:样本}, 单调递增, 单调递减, 多空net年化%, 方向}。
    """
    sub = frame[frame["h"] == h].dropna(subset=["k", "r"])
    layer_mean, layer_n = {}, {}
    for k in range(N_LAYERS):
        g = sub[sub["k"] == k]["r"]
        layer_mean[k] = float(g.mean()) * 100 if len(g) else None
        layer_n[k] = int(len(g))
    means = [layer_mean[k] for k in range(N_LAYERS)]
    valid = [m for m in means if m is not None]
    inc = len(valid) >= 2 and all(x <= y + 1e-9 for x, y in zip(valid, valid[1:]))
    dec = len(valid) >= 2 and all(x >= y - 1e-9 for x, y in zip(valid, valid[1:]))
    # 多空:用整体 IC 符号决定方向
    top, bot = layer_mean.get(N_LAYERS - 1), layer_mean.get(0)
    ls_direction, ls_net = None, None
    if top is not None and bot is not None:
        # 先按 k 与 r 的整体相关判方向
        corr = sub[["k", "r"]].corr(method="spearman").iloc[0, 1] if len(sub) >= 3 else np.nan
        if np.isfinite(corr):
            if corr >= 0:      # 动量延续:多高k空低k
                gross = (top - bot)
                ls_direction = "多k3空k0(动量延续)"
            else:              # 过热反转:多低k空高k
                gross = (bot - top)
                ls_direction = "多k0空k3(过热反转)"
            net_per_hold = gross - cost_roundtrip           # 单次持有 net%(已×100)
            ls_net = net_per_hold * (_TRADING_DAYS / h)     # 折年化(粗略,非复利)
    return {
        "layer_mean": layer_mean, "layer_n": layer_n,
        "单调递增": inc, "单调递减": dec,
        "多空net年化%": round(ls_net, 2) if ls_net is not None else None,
        "方向": ls_direction,
    }


# ————————————————————— 子样本符号稳定 —————————————————————
def subsample_sign_stability(frame: pd.DataFrame, h: int, by: str = "year") -> dict:
    """分子样本(默认按年份)各自算 IC 均值符号,检验跨样本符号是否一致(防单窗过拟合)。

    返回 {子样本: mean_ic, ..., 符号一致: bool}。
    """
    sub = frame[frame["h"] == h].dropna(subset=["k", "r"]).copy()
    if sub.empty:
        return {"符号一致": None}
    if by == "year":
        sub["_grp"] = sub["date"].astype(str).str.slice(0, 4)
    else:
        sub["_grp"] = by
    res = {}
    signs = []
    for grp, g in sub.groupby("_grp"):
        pairs = [(gg["k"].to_numpy(float), gg["r"].to_numpy(float))
                 for _d, gg in g.groupby("date")]
        ic = ev_stats.rank_ic(pairs, method="spearman").get("mean_ic")
        res[str(grp)] = None if ic is None else round(ic, 4)
        if ic is not None and abs(ic) > 1e-6:
            signs.append(np.sign(ic))
    res["符号一致"] = bool(len(set(signs)) == 1) if signs else None
    return res


# ————————————————————— 闸门判定 —————————————————————
def gate_verdict(ic_table: dict, subsample: dict) -> dict:
    """对预注册闸门判定:某 horizon 上 |IC|≥IC_BAR 且 |t|≥T_BAR 且跨子样本符号稳定 → 有信号。"""
    passed = {}
    for h, s in ic_table.items():
        ic, t = s.get("mean_ic"), s.get("t_stat")
        ok = (ic is not None and t is not None
              and abs(ic) >= IC_BAR and abs(t) >= T_BAR)
        passed[h] = ok
    sign_stable = subsample.get("符号一致") is True
    any_pass = any(passed.values()) and sign_stable
    return {
        "各h达标": passed, "符号稳定": sign_stable,
        "判定": "有信号·建议投全量一期" if any_pass else "未达标·停在探针(温度降级纯报告)",
        "达标": any_pass,
    }


def build_eval_frame(temp_df: pd.DataFrame, ret_by_industry: dict,
                     horizons=HORIZONS) -> pd.DataFrame:
    """把温度序列与各行业指数远期收益对齐成回测长表。

    temp_df: [date, industry, k](k 可含弃权=NaN,会在 dropna 剔除)。
    ret_by_industry: {industry: forward_returns 输出 DataFrame[date,h,r]}。
    返回 long [date, industry, k, h, r]。
    """
    parts = []
    for industry, g in temp_df.dropna(subset=["k"]).groupby("industry"):
        ret = ret_by_industry.get(industry)
        if ret is None or ret.empty:
            continue
        merged = g[["date", "industry", "k"]].merge(ret, on="date", how="inner")
        parts.append(merged)
    if not parts:
        return pd.DataFrame(columns=["date", "industry", "k", "h", "r"])
    return pd.concat(parts, ignore_index=True)
