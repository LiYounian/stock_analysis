"""六维聚合 + 按策略类型分流:广筛型(命中率+收益质量+超额+显著性)/ 可排序型(广筛全量+Top-N精度+rank-IC)/
参考型(伪排序,仅广筛口径)。

维度对照(任务①–⑥):
  ③ 收益质量:每票期望收益(均值/中位数)+ 盈亏比 + 分布(P10/P90)+ 胜率
  ④ 超额收益(幅度):策略均收益 − 基准均收益(等权全市场 / 随机同数量 bootstrap;指数基准见 report)
  ⑤ 显著性 + 按日聚类:命中率/超额的按交易日聚类 bootstrap CI + p 值(Wilson 仅作 naive 对照)
  ⑥ 按策略类型匹配:广筛型 → 命中/收益/超额;可排序型 → 追加 Top-N(5/10/20)精度 + rank-IC/ICIR;
     参考型(策略11伪排序)→ 只走广筛口径,不跑 rank-IC/Top-N(避免"看着有排序其实是噪声"的误导)
  ② 全部基于 T+1 入场重算;隔夜跳空单列。
滚动窗:近一周=5/近一月=20/近一季=60/近一年=250 交易日 + 全史(所有预测日)。
"""
from __future__ import annotations

import bisect

import numpy as np
import pandas as pd

from . import stats as _st
from .schema import DIRECTIONAL, RANKABLE, REFERENCE

WINDOWS: dict[str, int] = {"近一周": 5, "近一月": 20, "近一季": 60, "近一年": 250}
_THIN_N = 30
_BOOT_B = 2000
TOPN_LEVELS = (5, 10, 20)   # 可排序型 Top-N 精度分档


# ────────────────────── 收益质量 ──────────────────────
def return_quality(r: np.ndarray) -> dict:
    """③ 收益质量:均值/中位数/盈亏比/胜率/P10/P90。r=逐票 T+1 基准实现收益%。"""
    r = np.asarray([x for x in r if x is not None and np.isfinite(x)], float)
    n = len(r)
    if n == 0:
        return {"n": 0, "均值%": None, "中位数%": None, "胜率%": None,
                "盈亏比": None, "P10%": None, "P90%": None}
    wins, losses = r[r > 0], r[r < 0]
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    pf = round(avg_win / abs(avg_loss), 2) if avg_loss < 0 else None   # 无输家→无穷,记 None
    return {"n": n, "均值%": round(float(r.mean()), 3),
            "中位数%": round(float(np.median(r)), 3),
            "胜率%": round(float((r > 0).mean()) * 100, 1),
            "盈亏比": pf, "P10%": round(float(np.percentile(r, 10)), 3),
            "P90%": round(float(np.percentile(r, 90)), 3)}


def _rate(series) -> float | None:
    s = pd.Series(series).dropna()
    return round(float(s.mean()) * 100, 1) if len(s) else None


# ────────────────────── 方向型单元格 ──────────────────────
def directional_cell(sub: pd.DataFrame, uni_ret: dict, h: int, seed: int) -> dict:
    """一个 (策略, 窗, horizon) 的方向型全维度。sub=该切片已到期行(matured)。"""
    scored = sub.dropna(subset=["hit_end"])           # 方向非中性
    r = scored["r"].to_numpy(float)
    n = len(scored)
    pred_days = sorted(scored["pred_date"].unique().tolist())
    cell = {"已到期样本": n, "预测日数": len(pred_days)}

    # ③ 收益质量 + 方向命中(期末/期内)
    cell["收益质量"] = return_quality(r)
    cell["命中率%_期末"] = _rate(scored["hit_end"])
    if h != 1:
        cell["命中率%_期内触及"] = _rate(scored["hit_intra"])
    cell["隔夜跳空均值%"] = (round(float(scored["gap"].dropna().mean()), 3)
                            if scored["gap"].notna().any() else None)
    fb = scored["entry_fallback"]
    cell["用close入场占比%"] = (round(float(fb.fillna(False).mean()) * 100, 1)
                                if len(fb) else None)
    if n == 0:
        return cell

    # ⑤ 命中率按日聚类 bootstrap CI + naive Wilson
    day_hit = [g["hit_end"].to_numpy(float) for _, g in scored.groupby("pred_date")]
    hb = _st.cluster_bootstrap_ci(day_hit, "mean", B=_BOOT_B, seed=seed)
    cell["命中率_聚类CI%"] = ([round(hb["lo"] * 100, 1), round(hb["hi"] * 100, 1)]
                              if hb["lo"] is not None else None)
    k = int(scored["hit_end"].sum())
    cell["命中率_naiveWilson%"] = list(_st.wilson_ci(k, n))
    cell["聚类交易日数"] = hb["n_days"]

    # ④ 超额(幅度)+ ⑤ 超额按日聚类检验
    strat_day, mkt_day, day_uni, sizes = [], [], [], []
    for d in pred_days:
        gd = scored[scored["pred_date"] == d]["r"].to_numpy(float)
        u = uni_ret.get((d, h), np.array([]))
        strat_day.append(gd)
        mkt_day.append(float(u.mean()) if len(u) else None)
        day_uni.append(u)
        sizes.append(len(gd))
    # 组合口径(与聚类显著性一致):每日等权组合收益,再跨日等权。策略/基准/超额三者同口径。
    strat_day_means = [float(a.mean()) if len(a) else None for a in strat_day]
    paired = [(s, m) for s, m in zip(strat_day_means, mkt_day)
              if s is not None and m is not None]
    strat_pf = round(float(np.mean([s for s, _ in paired])), 3) if paired else None
    mkt_pf = round(float(np.mean([m for _, m in paired])), 3) if paired else None
    cell["策略均收益%_组合日均"] = strat_pf
    cell["基准_全市场均收益%"] = mkt_pf
    cell["超额收益%_vs全市场"] = (round(strat_pf - mkt_pf, 3)
                                 if (strat_pf is not None and mkt_pf is not None) else None)
    ex = _st.cluster_bootstrap_excess(strat_day, mkt_day, B=_BOOT_B, seed=seed)
    cell["超额_聚类CI%"] = ([ex["lo"], ex["hi"]] if ex.get("lo") is not None else None)
    cell["超额_聚类p值"] = ex.get("p_value")
    rp = _st.bootstrap_random_pick(strat_day_means, day_uni, sizes, B=_BOOT_B, seed=seed)
    cell["随机基准均收益%"] = rp.get("rand_mean")
    cell["优于随机p值"] = rp.get("p_value")
    cell["随机分布[P10,P90]%"] = ([rp["rand_p10"], rp["rand_p90"]]
                                  if rp.get("rand_p10") is not None else None)

    if 0 < n < _THIN_N:
        cell["薄样本"] = f"仅{n}"
    return cell


# ────────────────────── 排序型单元格 ──────────────────────
def ranking_cell(sub: pd.DataFrame, h: int) -> dict:
    """⑥ 排序型:截面 rank-IC/ICIR。sub=该切片已到期且有 rank_score 的行。"""
    valid = sub.dropna(subset=["rank_score", "r"])
    pairs = []
    for _d, g in valid.groupby("pred_date"):
        pairs.append((g["rank_score"].to_numpy(float), g["r"].to_numpy(float)))
    ic = _st.rank_ic(pairs)
    ic["已到期样本"] = int(len(valid))
    return ic


# ────────────────────── Top-N 精度(可排序型专用) ──────────────────────
def topn_precision(sub: pd.DataFrame, uni_ret: dict, h: int,
                   levels=TOPN_LEVELS, seed: int = 20260828) -> dict:
    """可排序型 Top-N 分档精度:每预测日按 rank_score **降序**取前 N 只 → 命中率/期望收益/超额。

    检验"分数越高是否越准/越赚"(全部票等权评法会浪费此排序信息)。
    sub=该 (策略,窗,horizon) 已到期且有 rank_score 的行。某日 picks 不足 N → 取该日全部(min(N,当日数))。
    跨日聚合:命中/期望收益按选中的 Top-N 行**池化**;超额按"每日 Top-N 组合收益 − 当日全市场均收益"
    **跨日等权**(与全量单元『组合日均』同口径,可与全量指标直接对比看选择性)。

    返回 {N: {档位N, 命中率%_期末, 命中率%_期内触及, 期望收益%_池化, 期望收益%_组合日均,
              超额%_vs全市场, 超额_聚类p值, 选中样本, 预测日数, 每日实际档位<N占比%}}。
    无有效 rank_score(广筛/参考型或该策略未透出打分)→ {}(上层据此标注"无连续打分,Top-N不适用")。
    """
    valid = sub.dropna(subset=["rank_score", "r"])
    if valid.empty:
        return {}
    day_groups = list(valid.groupby("pred_date"))
    out: dict = {}
    for N in levels:
        hit_end, hit_intra, r_pool = [], [], []
        strat_day, mkt_day, undersized = [], [], 0
        for d, g in day_groups:
            gd = g.sort_values("rank_score", ascending=False).head(N)
            k = len(gd)
            if k < N:
                undersized += 1
            he = gd["hit_end"].dropna().to_numpy(float)
            hi = gd["hit_intra"].dropna().to_numpy(float)
            rv = gd["r"].dropna().to_numpy(float)
            hit_end.extend(he.tolist())
            hit_intra.extend(hi.tolist())
            r_pool.extend(rv.tolist())
            strat_day.append(rv)
            u = uni_ret.get((d, h), np.array([]))
            mkt_day.append(float(u.mean()) if len(u) else None)
        n_sel = len(r_pool)
        if n_sel == 0:
            continue
        strat_means = [float(a.mean()) if len(a) else None for a in strat_day]
        paired = [(s, m) for s, m in zip(strat_means, mkt_day)
                  if s is not None and m is not None]
        strat_pf = float(np.mean([s for s, _ in paired])) if paired else None
        mkt_pf = float(np.mean([m for _, m in paired])) if paired else None
        ex = _st.cluster_bootstrap_excess(strat_day, mkt_day, B=_BOOT_B, seed=seed)
        cell = {
            "档位N": N,
            "命中率%_期末": _rate(pd.Series(hit_end)) if hit_end else None,
            "期望收益%_池化": round(float(np.mean(r_pool)), 3),
            "期望收益%_组合日均": round(strat_pf, 3) if strat_pf is not None else None,
            "超额%_vs全市场": (round(strat_pf - mkt_pf, 3)
                              if (strat_pf is not None and mkt_pf is not None) else None),
            "超额_聚类p值": ex.get("p_value"),
            "选中样本": n_sel,
            "预测日数": len(day_groups),
            "每日不足N占比%": round(undersized / len(day_groups) * 100, 1) if day_groups else None,
        }
        if h != 1 and hit_intra:
            cell["命中率%_期内触及"] = _rate(pd.Series(hit_intra))
        out[N] = cell
    return out


# ────────────────────── 窗口 + 顶层聚合 ──────────────────────
def _window_dates(calendar, N):
    return set(calendar[-N:]) if calendar else set()


def aggregate(scored: pd.DataFrame, uni_ret: dict, calendar: list[str],
              horizons=(1, 5), windows=None, seed: int = 20260828,
              track: str = "live") -> dict:
    """顶层:每策略 × 每窗 × 每 horizon 按 stype 选指标。含"全史"窗(所有预测日)。"""
    windows = dict(windows or WINDOWS)
    out = {"轨道": track, "口径": "T+1入场;广筛型=命中/收益质量/超额/显著性(按日聚类,全部票等权vs市场);"
                            "可排序型=广筛全量+Top-N(5/10/20)精度+rank-IC/ICIR;"
                            "参考型(策略11伪排序)=仅广筛口径、不跑rank-IC/Top-N;非投资建议", "窗口": {}}
    if scored.empty or not calendar:
        return out

    first_pred = str(scored["pred_date"].min())
    span = len(calendar) - bisect.bisect_left(calendar, first_pred)
    # 全史窗:覆盖所有预测日(不受 N 限)。
    win_specs = list(windows.items()) + [("全史", None)]

    for wname, N in win_specs:
        if N is None:
            wdates = set(scored["pred_date"].unique().tolist())
            sufficient, actual, note = True, span, "全部预测日(不设窗)"
        else:
            wdates = _window_dates(calendar, N)
            sufficient = N <= span
            actual = min(N, span)
            note = (f"数据充足(窗内交易日≥{N})" if sufficient
                    else f"数据不足 {N} 交易日,实为全部 {actual} 日(观测约 {span} 交易日)")
        wentry = {"窗口交易日数N": N, "数据充足": bool(sufficient),
                  "实际覆盖交易日": int(actual), "说明": note, "策略": {}}
        rw = scored[scored["pred_date"].isin(wdates)]
        for sid, g in rw.groupby("strategy_id"):
            stype = g["stype"].iloc[0]
            sentry = {"策略名": g["strategy"].iloc[0], "类型": stype,
                      "预测日数": int(g["pred_date"].nunique())}
            for h in horizons:
                gh = g[(g["h"] == h) & (g["matured"])]
                if stype == RANKABLE:
                    # 可排序型:广筛全量指标 + Top-N 分档精度 + rank-IC(三者并存于同一单元)。
                    cell = directional_cell(gh, uni_ret, h, seed)
                    cell["Top-N精度"] = topn_precision(gh, uni_ret, h, seed=seed)
                    cell["rank_ic"] = ranking_cell(gh, h)
                    sentry[f"{h}日"] = cell
                else:
                    # 广筛型 / 参考型:均只走广筛全量指标(参考型不跑 rank-IC/Top-N)。
                    sentry[f"{h}日"] = directional_cell(gh, uni_ret, h, seed)
            wentry["策略"][sid] = sentry
        out["窗口"][wname] = wentry
    return out
