"""行业财报信号回测分析:分离评估两类价值,按披露月聚类做显著性。

价值A · 避暴雷(风险侧):高危红旗组 vs 无红旗组,后向收益/超额/最大回撤/暴跌率之差。
价值B · 选alpha:quality_score 分层单调性 + 按截面 rank-IC。

显著性:观测在报告季高度时间聚类 → 一律**按披露月做块自助(block bootstrap)**,
对"月"重采样再算统计量,取双侧 p。避免把同一披露季的重叠票当独立样本高估显著性。

成本:A股单边约 手续费0.025% + 卖出印花税0.05% + 冲击,来回按 ~0.35% 估;
超额/多空口径的净值扣一次来回成本。非投资建议。
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
PANEL = os.path.join(ROOT, "data/analysis/backtest/finval/panel.parquet")
COST_RT = 0.0035  # 来回成本估计

rng = np.random.default_rng(20260830)


def block_bootstrap_diff(df, group_col, metric, n_boot=3000):
    """按 disc_month 块自助:统计量=组间均值差(group True − False)。返回 (点估计, p双侧, ci)。

    向量化:每月预算 A/B 组的 (sum, count),自助时对月重采样后聚合 sum/count 再算均值差,
    避免逐次 concat 大表。"""
    sub = df.dropna(subset=[metric])
    g = sub.groupby(["disc_month", group_col])[metric].agg(["sum", "count"]).reset_index()
    months = sub["disc_month"].unique()
    mi = {m: i for i, m in enumerate(months)}
    nm = len(months)
    aS = np.zeros(nm); aN = np.zeros(nm); bS = np.zeros(nm); bN = np.zeros(nm)
    for _, r in g.iterrows():
        i = mi[r["disc_month"]]
        if r[group_col]:
            aS[i] += r["sum"]; aN[i] += r["count"]
        else:
            bS[i] += r["sum"]; bN[i] += r["count"]

    # 点估计
    point = (aS.sum() / aN.sum() - bS.sum() / bN.sum()) if aN.sum() and bN.sum() else np.nan
    boots = np.empty(n_boot)
    for k in range(n_boot):
        pick = rng.integers(0, nm, size=nm)
        na = aN[pick].sum(); nb = bN[pick].sum()
        boots[k] = (aS[pick].sum() / na - bS[pick].sum() / nb) if (na and nb) else np.nan
    boots = boots[np.isfinite(boots)]
    if len(boots) == 0:
        return point, np.nan, (np.nan, np.nan)
    p = 2 * min((boots >= 0).mean(), (boots <= 0).mean())
    ci = (np.percentile(boots, 2.5), np.percentile(boots, 97.5))
    return point, min(p, 1.0), ci


def rank_ic_by_season(df, score_col, ret_col):
    """按披露月截面算 Spearman rank-IC,汇总均值/t/命中率(月为聚类单元)。"""
    ics = []
    for m, d in df.dropna(subset=[score_col, ret_col]).groupby("disc_month"):
        if d[score_col].nunique() < 5 or len(d) < 10:
            continue
        ic = d[score_col].rank().corr(d[ret_col].rank())  # Spearman = Pearson of ranks
        if np.isfinite(ic):
            ics.append((m, ic, len(d)))
    if not ics:
        return None
    arr = np.array([x[1] for x in ics])
    t = arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr))) if len(arr) > 1 and arr.std() > 0 else np.nan
    return {"n_seasons": len(ics), "ic_mean": arr.mean(), "ic_std": arr.std(ddof=1),
            "t": t, "hit_rate": (arr > 0).mean(), "seasons": ics}


def crash_rate(df, group_col, col, thr):
    sub = df.dropna(subset=[col])
    a = sub[sub[group_col]]
    b = sub[~sub[group_col]]
    ra = (a[col] < thr).mean() if len(a) else np.nan
    rb = (b[col] < thr).mean() if len(b) else np.nan
    return ra, rb, len(a), len(b)


def summarize(df, label):
    print(f"\n{'='*70}\n### 样本: {label}  (N={len(df)})")
    print(f"披露区间 {df['disclosure_date'].min().date()} .. {df['disclosure_date'].max().date()};"
          f" 独立票 {df['code'].nunique()}; 披露月数 {df['disc_month'].nunique()}")

    # ---------- 价值A:避暴雷(高危红旗组 vs 无红旗组)----------
    df["grp_high"] = df["has_high_flag"]
    base = df[df["has_high_flag"] | (df["n_flags"] == 0)].copy()
    base["grp_high"] = base["has_high_flag"]
    nH = int(base["grp_high"].sum()); nN = int((~base["grp_high"]).sum())
    print(f"\n-- 价值A 避暴雷: 高危红旗组 N={nH} vs 无红旗组 N={nN} --")
    for H in (20, 60):
        for metric in (f"fwd{H}", f"exc{H}", f"mdd{H}"):
            pt, p, ci = block_bootstrap_diff(base, "grp_high", metric)
            aH = base[base["grp_high"]][metric].mean()
            aN = base[~base["grp_high"]][metric].mean()
            star = "***" if p < 0.01 else "**" if p < 0.05 else "*" if p < 0.1 else ""
            print(f"   {metric}: 红旗组 {aH:+.4f} | 无旗组 {aN:+.4f} | 差 {pt:+.4f} "
                  f"(月聚类p={p:.3f}{star} CI[{ci[0]:+.3f},{ci[1]:+.3f}])")
    # 暴跌率
    for H, thr in ((60, -0.20), (60, -0.30), (20, -0.15)):
        ra, rb, na, nb = crash_rate(base, "grp_high", f"min{H}", thr)
        print(f"   暴跌率 min{H}<{thr:+.0%}: 红旗组 {ra:.1%} | 无旗组 {rb:.1%}  (差 {ra-rb:+.1%})")

    # ---------- 价值B:选alpha(quality_score 分层 + rank-IC)----------
    print(f"\n-- 价值B 选alpha: quality_score 分层 & rank-IC --")
    for ret_col in ("exc20", "exc60"):
        r = rank_ic_by_season(df, "quality_score", ret_col)
        if r:
            print(f"   rank-IC({ret_col}): 均值 {r['ic_mean']:+.4f}  t={r['t']:+.2f} "
                  f"命中率 {r['hit_rate']:.0%}  ({r['n_seasons']} 季)")
        else:
            print(f"   rank-IC({ret_col}): 样本不足")
    # 五分位分层(按 quality_score)
    d = df.dropna(subset=["quality_score", "exc60"]).copy()
    if len(d) >= 25:
        d["q"] = pd.qcut(d["quality_score"].rank(method="first"), 5, labels=[1, 2, 3, 4, 5])
        tier = d.groupby("q", observed=True)["exc60"].agg(["mean", "count"])
        print("   quality_score 五分位 exc60 均值(低→高分):")
        for q, row in tier.iterrows():
            print(f"      Q{q}: {row['mean']:+.4f} (n={int(row['count'])})")
        hi = d[d["q"] == 5]["exc60"].mean(); lo = d[d["q"] == 1]["exc60"].mean()
        print(f"   多空 Q5-Q1 exc60: {hi-lo:+.4f}  (扣来回成本 {2*COST_RT:.2%} 后 {hi-lo-2*COST_RT:+.4f})")


def main():
    panel = pd.read_parquet(PANEL)
    panel["disclosure_date"] = pd.to_datetime(panel["disclosure_date"])
    summarize(panel, "全部有财报的票(含通用兜底)")
    # 命中12行业专家的子集
    exp = panel[panel["expert"].notna()].copy()
    if len(exp) >= 50:
        summarize(exp, "命中行业财报专家子集(12行业)")
    # 仅年报+半年报(数据更全、审计更强)
    ann = panel[panel["report_type"].isin(["年报", "半年报"])].copy()
    if len(ann) >= 50:
        summarize(ann, "仅年报+半年报")


if __name__ == "__main__":
    main()
