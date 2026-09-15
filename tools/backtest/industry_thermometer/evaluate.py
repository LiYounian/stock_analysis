"""逐维度评测:每个数值维对**前瞻行业指数收益(sector β)**的预测力。

区别于旧 IET(只测合成 k 投票):这里对**每一个维度单独**产出:
  - rank-IC(截面「维度连续值 vs 前瞻行业指数收益」,按 h)+ ICIR / t
  - A/B 组前瞻收益差(B过冷 − A过热)+ 日频序列 t + **块自助 CI(块长=h,校正重叠区间自相关)**
  - 功效:n_days / 有效独立块(n_days/h) / 达 t=2 需样本 / MDE
  - 逐维 verdict:有预测力 / 欠功效待复查 / 不可用(真 null)

方向不预设:IC<0 = 贵/热 → 低前瞻收益 = Marks 逆向(过冷跑赢);IC>0 = 动量延续。
功效纪律:短样本不显著只判「欠功效待复查」,**绝不写「证伪」**;只有样本充足且点估≈0 才判「不可用」。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.backtest.iet_probe import run_probe as R
from tools.config.strategy import THRESHOLDS

_IET = THRESHOLDS["行业环境温度计"]
IC_BAR: float = _IET["IET_IC_BAR"]
T_BAR: float = _IET["IET_T_BAR"]

# 判「不可用·真 null」的样本充足门槛(独立块);低于此只能判「欠功效」,不得判不可用
ADEQUATE_BLOCKS = 100


# ————————————————————— 前瞻收益长表(一次性) —————————————————————
def ret_long(ret_by_industry: dict) -> pd.DataFrame:
    """{industry: DataFrame[date,h,r]} → long [date, industry, h, r]。"""
    parts = []
    for ind, df in ret_by_industry.items():
        if df is None or df.empty:
            continue
        g = df.copy()
        g["industry"] = ind
        parts.append(g[["date", "industry", "h", "r"]])
    if not parts:
        return pd.DataFrame(columns=["date", "industry", "h", "r"])
    return pd.concat(parts, ignore_index=True)


# ————————————————————— 块自助(校正重叠) —————————————————————
def _block_bootstrap_mean(x, block: int, n_boot: int = 2000, seed: int = 0) -> dict:
    """移动块自助:块长=block(≈持有期 h)以校正重叠区间自相关。返回均值+95%CI+两侧p。"""
    x = np.asarray([v for v in x if np.isfinite(v)], dtype="float64")
    n = len(x)
    if n == 0:
        return {"mean": None, "ci_lo": None, "ci_hi": None, "p_boot": None, "n": 0}
    block = max(1, min(int(block), n))
    n_blocks = int(np.ceil(n / block))
    starts_max = n - block + 1
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot, dtype="float64")
    for b in range(n_boot):
        starts = rng.integers(0, starts_max, size=n_blocks)
        sample = np.concatenate([x[s:s + block] for s in starts])[:n]
        means[b] = sample.mean()
    lo, hi = np.percentile(means, [2.5, 97.5])
    frac_le0 = float(np.mean(means <= 0.0))
    p = 2.0 * min(frac_le0, 1.0 - frac_le0)
    return {"mean": float(x.mean()), "ci_lo": float(lo), "ci_hi": float(hi),
            "p_boot": float(min(1.0, p)), "n": n, "block": block}


# ————————————————————— A/B 组前瞻收益差(B−A) —————————————————————
def ab_spread(ab_df: pd.DataFrame, ret_lf: pd.DataFrame, h: int) -> dict:
    """维度 A/B 分组的前瞻收益差(B过冷 − A过热),日频截面 → 序列均值+t+块自助。

    ab_df: 单维 [date, industry, ab];ret_lf: long [date, industry, h, r]。
    daily_spread(T) = mean(r|B, 当日) − mean(r|A, 当日),需两组当日各≥1;跨日成序列。
    """
    ret_h = ret_lf[ret_lf["h"] == h][["date", "industry", "r"]]
    m = ab_df.dropna(subset=["ab"]).merge(ret_h, on=["date", "industry"], how="inner")
    if m.empty:
        return {"h": h, "daily_spread_mean_pp": None, "t": None, "n_days": 0, "bootstrap": None}
    daily = []
    for _d, g in m.groupby("date"):
        a = g[g["ab"] == "A"]["r"]
        b = g[g["ab"] == "B"]["r"]
        if len(a) >= 1 and len(b) >= 1:
            daily.append(float(b.mean() - a.mean()))
    if not daily:
        return {"h": h, "daily_spread_mean_pp": None, "t": None, "n_days": 0, "bootstrap": None}
    arr = np.asarray(daily, dtype="float64")
    n = len(arr)
    mean = float(arr.mean())
    sd = float(arr.std(ddof=1)) if n > 1 else float("nan")
    t = float(mean / (sd / np.sqrt(n))) if n > 1 and sd > 0 else None
    boot = _block_bootstrap_mean(arr, block=h)
    return {
        "h": h,
        "daily_spread_mean_pp": round(mean * 100, 4),   # B−A,百分点
        "t": round(t, 3) if t is not None else None,
        "n_days": n,
        "bootstrap_ci_pp": None if boot["mean"] is None else
            [round(boot["ci_lo"] * 100, 4), round(boot["ci_hi"] * 100, 4)],
        "p_boot": boot["p_boot"],
    }


# ————————————————————— 连续 IC 的重叠校正显著性 —————————————————————
def daily_ic_series(ic_frame: pd.DataFrame, h: int) -> np.ndarray:
    """每交易日截面 Spearman IC(维度连续值 vs 前瞻收益)组成的日频序列。"""
    sub = ic_frame[ic_frame["h"] == h].dropna(subset=["k", "r"])
    ics = []
    for _d, g in sub.groupby("date"):
        if len(g) >= 3 and g["k"].nunique() > 1 and g["r"].nunique() > 1:
            c = g[["k", "r"]].corr(method="spearman").iloc[0, 1]
            if np.isfinite(c):
                ics.append(float(c))
    return np.asarray(ics, dtype="float64")


def ic_significance(ic_frame: pd.DataFrame, h: int) -> dict:
    """连续 IC 的 naive t(overlap-inflated,仅报告) + 块自助(块长=h,重叠校正,作判定依据)。"""
    series = daily_ic_series(ic_frame, h)
    n = len(series)
    if n == 0:
        return {"mean_ic": None, "naive_t": None, "boot_ci": None, "p_boot": None, "n_days": 0}
    mean = float(series.mean())
    sd = float(series.std(ddof=1)) if n > 1 else float("nan")
    naive_t = float(mean / (sd / np.sqrt(n))) if n > 1 and sd > 0 else None
    boot = _block_bootstrap_mean(series, block=h)
    return {
        "mean_ic": round(mean, 4),
        "naive_t": round(naive_t, 3) if naive_t is not None else None,
        "boot_ci": None if boot["mean"] is None else [round(boot["ci_lo"], 4), round(boot["ci_hi"], 4)],
        "p_boot": boot["p_boot"],
        "n_days": n,
    }


# ————————————————————— 功效 —————————————————————
def power_note(n_days: int, h: int, observed_t) -> dict:
    eff_blocks = round(n_days / h, 1) if h else float(n_days)
    days_to_t2 = None
    if observed_t is not None and abs(observed_t) > 1e-9:
        days_to_t2 = int(round((T_BAR / abs(observed_t)) ** 2 * n_days))
    return {"n_days": int(n_days), "有效独立块": eff_blocks,
            "观测t": None if observed_t is None else round(float(observed_t), 3),
            "达t=2需样本日": days_to_t2}


# ————————————————————— 单维评测 —————————————————————
def evaluate_dim(dim: str, dim_panel: pd.DataFrame, ret_by_industry: dict,
                 horizons=R.HORIZONS) -> dict:
    """单维:rank-IC(按h) + A/B spread(按h) + 子样本符号 + 功效 + verdict。"""
    sub = dim_panel[dim_panel["dim"] == dim]
    val_df = sub[["date", "industry", "value"]].rename(columns={"value": "k"})
    ic_frame = R.build_eval_frame(val_df, ret_by_industry, horizons)
    # 连续 IC 的重叠校正显著性(块自助,作判定依据;naive t 仅报告)
    ic = {h: ic_significance(ic_frame, h) for h in horizons}
    subs = {h: R.subsample_sign_stability(ic_frame, h, by="year") for h in horizons}

    ab_df = sub[["date", "industry", "ab"]]
    ret_lf = ret_long(ret_by_industry)
    spreads = {h: ab_spread(ab_df, ret_lf, h) for h in horizons}
    powers = {h: power_note(ic[h].get("n_days", 0), h, ic[h].get("naive_t"))
              for h in horizons}

    verdict = _verdict(ic, spreads, subs, horizons)
    return {"dim": dim, "ic": ic, "ab_spread": spreads,
            "subsample": subs, "power": powers, "verdict": verdict}


def _verdict(ic: dict, spreads: dict, subs: dict, horizons) -> dict:
    """预注册判定(**用重叠校正的块自助 p_boot,不用 overlap-inflated 的 naive t**)。
    有预测力:某 h 连续IC块自助 p_boot<0.05 且 |mean_ic|≥IC_BAR 且 子样本符号稳定;
    不可用·真null:样本充足(有效块≥ADEQUATE_BLOCKS) 且各h点估≈0(|IC|<bar 且 IC块自助CI含0);
    否则:欠功效待复查(短样本不显著≠证伪,给达t=2需样本)。方向由 IC 符号给。"""
    passed_h = []
    for h in horizons:
        s = ic[h]
        icv, p = s.get("mean_ic"), s.get("p_boot")
        sign_stable = subs.get(h, {}).get("符号一致") is True
        if (icv is not None and p is not None and abs(icv) >= IC_BAR
                and p < 0.05 and sign_stable):
            passed_h.append(h)

    # 方向(取 |IC| 最大的 h 的符号)
    best_h = max(horizons, key=lambda h: abs(ic[h].get("mean_ic") or 0.0))
    best_ic = ic[best_h].get("mean_ic")
    direction = None
    if best_ic is not None and abs(best_ic) > 1e-6:
        direction = ("动量延续(热续强/冷续弱)" if best_ic > 0
                     else "逆向反转(过冷跑赢/过热跑输·Marks一致)")

    if passed_h:
        return {"判定": "有预测力", "达标h": passed_h, "方向": direction,
                "best_ic": None if best_ic is None else round(best_ic, 4)}

    # 是否样本充足到可判"真 null"(用重叠校正的 IC 块自助 CI 是否含 0)
    n_days_max = max((ic[h].get("n_days", 0) for h in horizons), default=0)
    adequate = any((ic[h].get("n_days", 0) / h) >= ADEQUATE_BLOCKS for h in horizons)
    near_zero = all((ic[h].get("mean_ic") is None or abs(ic[h]["mean_ic"]) < IC_BAR)
                    for h in horizons)
    ic_ci_contains_zero = all(
        (ic[h].get("boot_ci") is None) or
        (ic[h]["boot_ci"][0] <= 0 <= ic[h]["boot_ci"][1])
        for h in horizons)
    if adequate and near_zero and ic_ci_contains_zero:
        return {"判定": "不可用·真null", "方向": direction,
                "说明": f"样本充足(≥{ADEQUATE_BLOCKS}独立块)且各h点估≈0、IC块自助CI含0"}

    # 否则:欠功效
    days_needed = {h: power_note(ic[h].get("n_days", 0), h, ic[h].get("naive_t")).get("达t=2需样本日")
                   for h in horizons}
    return {"判定": "欠功效待复查", "方向": direction,
            "n_days_max": n_days_max, "达t=2需样本日": days_needed,
            "说明": "短样本未达显著≠证伪;补长历史/加样本后复查"}


def evaluate_all(dim_panel: pd.DataFrame, ret_by_industry: dict,
                 horizons=R.HORIZONS) -> dict:
    """所有维度逐一评测 + 多重检验提示。"""
    from tools.backtest.industry_thermometer.dims import DIMS
    results = {dim: evaluate_dim(dim, dim_panel, ret_by_industry, horizons)
               for dim in DIMS if (dim_panel["dim"] == dim).any()}
    n_tests = len(results) * len(horizons)
    return {
        "results": results,
        "multiple_testing": {
            "族大小": n_tests,
            "Bonferroni_alpha": round(0.05 / n_tests, 5) if n_tests else None,
            "提示": "维度×h 多重检验;单格显著须过族校正,勿挑单格",
        },
    }
