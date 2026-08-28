"""显著性与相关性统计:Wilson 区间、按交易日聚类的 bootstrap CI / p 值、rank-IC / ICIR。

核心原则(见任务⑤):**同一天选的票高度相关**,naive 逐票样本数夸大独立性。所有区间/检验
以"每个交易日的批次"为**独立聚类单元**:bootstrap 时对**交易日**重采样(而非对逐票),
把"样本薄不足为凭"量化成 CI 宽度 / p 值,而非只靠 <30 硬阈值。
"""
from __future__ import annotations

import math

import numpy as np


# ───────── 自包含统计原语(避免依赖 scipy;本 conda 环境无 scipy)─────────
def _betacf(a: float, b: float, x: float, itmax: int = 200, eps: float = 3e-12) -> float:
    """正则化不完全 Beta 的连分式(Numerical Recipes 法),供 Student-t 尾概率。"""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1e-30 if abs(d) < 1e-30 else d
    d = 1.0 / d
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1e-30 if abs(d) < 1e-30 else d
        c = 1.0 + aa / c
        c = 1e-30 if abs(c) < 1e-30 else c
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1e-30 if abs(d) < 1e-30 else d
        c = 1.0 + aa / c
        c = 1e-30 if abs(c) < 1e-30 else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """正则化不完全 Beta 函数 I_x(a,b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t: float, df: int) -> float:
    """Student-t 双边 p 值。df<1 → 1.0。"""
    if df < 1:
        return 1.0
    x = df / (df + t * t)
    return float(min(1.0, _betai(df / 2.0, 0.5, x)))


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman 秩相关 = 秩变换后的 Pearson。并列用平均秩。"""
    ra = pd_rank(a)
    rb = pd_rank(b)
    ra = ra - ra.mean()
    rb = rb - rb.mean()
    denom = math.sqrt(float((ra * ra).sum()) * float((rb * rb).sum()))
    return float((ra * rb).sum() / denom) if denom > 0 else float("nan")


def pd_rank(a: np.ndarray) -> np.ndarray:
    """平均秩(处理并列),纯 numpy。"""
    a = np.asarray(a, float)
    order = a.argsort()
    ranks = np.empty(len(a), float)
    ranks[order] = np.arange(len(a), dtype=float)
    # 并列取平均秩
    _vals, inv, counts = np.unique(a, return_inverse=True, return_counts=True)
    sums = np.zeros(len(counts))
    np.add.at(sums, inv, ranks)
    avg = sums / counts
    return avg[inv]


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """二项比例 Wilson 置信区间(返回百分比)。n=0 → (None,None)。

    注意:这是**逐票 naive** 口径,未做聚类,会**高估独立性**(区间偏窄)。仅作对照;
    真正口径以 cluster_bootstrap_ci 的按日聚类区间为准。
    """
    if n <= 0:
        return None, None
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return round((center - half) * 100, 1), round((center + half) * 100, 1)


def cluster_bootstrap_ci(day_values: list[np.ndarray], stat="mean",
                         B: int = 2000, seed: int = 20260828,
                         alpha: float = 0.05) -> dict:
    """按交易日聚类的 bootstrap 置信区间。

    day_values:list,每元素 = **某个交易日**批次内所有逐票的值(收益 或 0/1 命中)。
    重采样单元 = 交易日(整块 day 一起重采样),然后**池化**被抽中的天的所有逐票算统计量。
    这样区间宽度诚实反映"独立单元只有几天"。

    返回 {point, lo, hi, n_days, n_obs, B}。point = 全样本池化统计量。
    """
    days = [np.asarray(v, float) for v in day_values if len(v)]
    n_days = len(days)
    all_obs = np.concatenate(days) if days else np.array([])
    n_obs = len(all_obs)
    if n_obs == 0:
        return {"point": None, "lo": None, "hi": None, "n_days": 0, "n_obs": 0, "B": B}
    point = float(all_obs.mean()) if stat == "mean" else float(np.median(all_obs))
    if n_days < 2:                       # 单日无法聚类重采样 → 只给点估计
        return {"point": round(point, 4), "lo": None, "hi": None,
                "n_days": n_days, "n_obs": n_obs, "B": B, "说明": "单交易日,聚类CI不可算"}
    rng = np.random.default_rng(seed)
    boots = np.empty(B, float)
    idxs = np.arange(n_days)
    for b in range(B):
        pick = rng.choice(idxs, size=n_days, replace=True)
        pooled = np.concatenate([days[i] for i in pick])
        boots[b] = pooled.mean() if stat == "mean" else np.median(pooled)
    lo = float(np.percentile(boots, alpha / 2 * 100))
    hi = float(np.percentile(boots, (1 - alpha / 2) * 100))
    return {"point": round(point, 4), "lo": round(lo, 4), "hi": round(hi, 4),
            "n_days": n_days, "n_obs": n_obs, "B": B}


def cluster_bootstrap_excess(strat_day: list[np.ndarray], mkt_day: list[float],
                             B: int = 2000, seed: int = 20260828,
                             alpha: float = 0.05) -> dict:
    """按日聚类的**超额收益**bootstrap:每日超额 e_d = 该日策略票均收益 − 该日市场均收益。

    strat_day[i] = 第 i 个预测日策略票的逐票收益数组;mkt_day[i] = 同日全市场均收益(标量)。
    对**天**重采样,估计 mean(e_d) 的 CI 与双边 p 值(H0: 平均超额=0)。

    返回 {excess, lo, hi, p_value, n_days}。excess = 各日超额的等权均值(聚类单元=日)。
    """
    e = []
    for arr, m in zip(strat_day, mkt_day):
        arr = np.asarray(arr, float)
        if len(arr) and m is not None and not math.isnan(m):
            e.append(float(arr.mean()) - float(m))
    e = np.asarray(e, float)
    n_days = len(e)
    if n_days == 0:
        return {"excess": None, "lo": None, "hi": None, "p_value": None, "n_days": 0}
    point = float(e.mean())
    if n_days < 2:
        return {"excess": round(point, 4), "lo": None, "hi": None,
                "p_value": None, "n_days": n_days, "说明": "单交易日,聚类检验不可算"}
    rng = np.random.default_rng(seed)
    boots = np.empty(B, float)
    for b in range(B):
        boots[b] = rng.choice(e, size=n_days, replace=True).mean()
    lo = float(np.percentile(boots, alpha / 2 * 100))
    hi = float(np.percentile(boots, (1 - alpha / 2) * 100))
    # 双边 bootstrap p:以 0 为原点,居中后看 |boot−point| ≥ |point| 的占比。
    centered = boots - point
    p = float(2 * min((centered >= abs(point)).mean(), (centered <= -abs(point)).mean()))
    p = min(1.0, max(0.0, p if point != 0 else 1.0))
    return {"excess": round(point, 4), "lo": round(lo, 4), "hi": round(hi, 4),
            "p_value": round(p, 4), "n_days": n_days}


def bootstrap_random_pick(strat_day_means: list[float], day_universe: list[np.ndarray],
                          pick_sizes: list[int], B: int = 2000,
                          seed: int = 20260828) -> dict:
    """随机选同数量票 bootstrap(任务④基准3):检验策略是否显著优于随机。

    对每个预测日 d:从该日全市场已到期票(day_universe[d])随机抽 pick_sizes[d] 只,取均值;
    跨日等权平均 → 一次随机组合的整体收益。重采样 B 次得随机分布。
    strat_day_means[d] = 策略在该日票的均收益。

    返回 {strat_mean, rand_mean, rand_p10, rand_p90, p_value(策略≤随机的占比,单边), n_days}。
    p_value 小 → 策略显著优于随机。
    """
    valid = [(sm, uni, k) for sm, uni, k in zip(strat_day_means, day_universe, pick_sizes)
             if sm is not None and uni is not None and len(uni) > 0 and k > 0]
    if not valid:
        return {"strat_mean": None, "rand_mean": None, "rand_p10": None,
                "rand_p90": None, "p_value": None, "n_days": 0}
    strat_overall = float(np.mean([v[0] for v in valid]))
    rng = np.random.default_rng(seed)
    boots = np.empty(B, float)
    for b in range(B):
        day_means = []
        for _sm, uni, k in valid:
            uni = np.asarray(uni, float)
            kk = min(k, len(uni))
            day_means.append(rng.choice(uni, size=kk, replace=False).mean())
        boots[b] = float(np.mean(day_means))
    p = float((boots >= strat_overall).mean())    # 随机≥策略的比例(单边:策略更优则 p 小)
    return {"strat_mean": round(strat_overall, 4), "rand_mean": round(float(boots.mean()), 4),
            "rand_p10": round(float(np.percentile(boots, 10)), 4),
            "rand_p90": round(float(np.percentile(boots, 90)), 4),
            "p_value": round(p, 4), "n_days": len(valid)}


def rank_ic(daily_pairs: list[tuple[np.ndarray, np.ndarray]],
            method: str = "spearman") -> dict:
    """截面 rank-IC / ICIR(任务⑥,排序型策略专用)。

    daily_pairs:list,每元素 = 某预测日的 (rank_score 数组, 未来实现收益数组)(同序、等长)。
    对每日算截面 Spearman(默认)相关 = 当日 IC;IC 序列 → 均值 IC、ICIR=mean/std、t 与 p。

    返回 {mean_ic, icir, t_stat, p_value, n_days, ic_series_len, pos_ratio}。
    每日有效需 ≥3 个非退化样本;不足的日跳过。
    """
    ics = []
    for score, fwd in daily_pairs:
        score = np.asarray(score, float)
        fwd = np.asarray(fwd, float)
        m = np.isfinite(score) & np.isfinite(fwd)
        score, fwd = score[m], fwd[m]
        if len(score) < 3 or np.ptp(score) == 0 or np.ptp(fwd) == 0:
            continue
        if method == "spearman":
            ic = _spearman(score, fwd)
        else:
            sc = score - score.mean()
            fw = fwd - fwd.mean()
            den = math.sqrt(float((sc * sc).sum()) * float((fw * fw).sum()))
            ic = float((sc * fw).sum() / den) if den > 0 else float("nan")
        if np.isfinite(ic):
            ics.append(float(ic))
    n = len(ics)
    if n == 0:
        return {"mean_ic": None, "icir": None, "t_stat": None, "p_value": None,
                "n_days": 0, "pos_ratio": None}
    arr = np.asarray(ics, float)
    mean_ic = float(arr.mean())
    sd = float(arr.std(ddof=1)) if n >= 2 else 0.0
    icir = round(mean_ic / sd, 3) if sd > 0 else None
    if n >= 2 and sd > 0:
        t = mean_ic / (sd / math.sqrt(n))
        p = t_two_sided_p(abs(t), df=n - 1)
    else:
        t = p = None
    return {"mean_ic": round(mean_ic, 4), "icir": icir,
            "t_stat": round(t, 3) if t is not None else None,
            "p_value": round(p, 4) if p is not None else None,
            "n_days": n, "pos_ratio": round(float((arr > 0).mean()), 3)}
