"""Shadow-run α 打分:DeepSeek/千问 自选买入侧 vs 全A等权(forward_scorecard)。

买入侧 = stance ∈ {买入, 可参与}。基准"全A等权" = 当日 forward_scorecard 全样本 r 均值。
α(pp) = mean(买入侧 r) − mean(全样本 r)。命中率 = 买入侧 r>0 占比。
r_1 恒算;r_5 仅当日 <= T5_CUTOFF(已结算)时算。诚实标注样本量 N 与覆盖。
"""
from __future__ import annotations
import csv
import statistics
from pathlib import Path

T5_CUTOFF = "2026-09-04"  # T+5 已结算的最后一个选股日(>此日 r_5 未落)
BUY = {"买入", "可参与"}


def load_scorecard(path):
    """{date: {code: {'r_1':float|None,'r_5':float|None}}}。"""
    out: dict[str, dict[str, dict]] = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            d = row.get("date")
            code = row.get("code")
            if not d or not code:
                continue
            def num(k):
                v = row.get(k)
                try:
                    return float(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    return None
            out.setdefault(d, {})[code] = {"r_1": num("r_1"), "r_5": num("r_5")}
    return out


def _bench(day_card: dict, horizon: str):
    vals = [v[horizon] for v in day_card.values() if v.get(horizon) is not None]
    return statistics.mean(vals) if vals else None, len(vals)


def score_day(date, units, scorecard, want_t5=None):
    """units: list[dict](含 code/stance)。返回该日买入侧打分。"""
    if want_t5 is None:
        want_t5 = date <= T5_CUTOFF
    day_card = scorecard.get(date, {})
    buy = [u["code"] for u in units if u.get("stance") in BUY and u.get("code")]
    res = {"date": date, "n_units": len(units), "buy_side": buy, "n_buy": len(buy),
           "horizons": {}}
    for hz in (["r_1", "r_5"] if want_t5 else ["r_1"]):
        bench_mean, bench_n = _bench(day_card, hz)
        rs = [(c, day_card.get(c, {}).get(hz)) for c in buy]
        scored = [(c, r) for c, r in rs if r is not None]
        vals = [r for _, r in scored]
        rec = {
            "bench_mean": bench_mean, "bench_n": bench_n,
            "n_scored": len(scored), "n_unscored": len(buy) - len(scored),
            "buy_mean": statistics.mean(vals) if vals else None,
            "hit_rate": (sum(1 for v in vals if v > 0) / len(vals)) if vals else None,
            "alpha_pp": (statistics.mean(vals) - bench_mean)
                        if (vals and bench_mean is not None) else None,
            "per_code": {c: r for c, r in scored},
        }
        res["horizons"][hz] = rec
    return res


def aggregate(day_results, horizon="r_1"):
    """跨日汇总:pooled(合并所有买入票) + 日均 α ± SE。"""
    per_day_alpha = []
    pooled_vals = []
    pooled_hits = 0
    pooled_n = 0
    for r in day_results:
        h = r["horizons"].get(horizon)
        if not h:
            continue
        if h["alpha_pp"] is not None:
            per_day_alpha.append(h["alpha_pp"])
        for c, v in h["per_code"].items():
            pooled_vals.append(v)
            pooled_hits += 1 if v > 0 else 0
            pooled_n += 1
    def mean_se(xs):
        if not xs:
            return None, None, 0
        m = statistics.mean(xs)
        se = (statistics.stdev(xs) / (len(xs) ** 0.5)) if len(xs) > 1 else None
        return m, se, len(xs)
    day_m, day_se, day_k = mean_se(per_day_alpha)
    return {
        "horizon": horizon,
        "n_days": day_k,
        "day_mean_alpha_pp": day_m, "day_se_alpha_pp": day_se,
        "pooled_n_buys": pooled_n,
        "pooled_hit_rate": (pooled_hits / pooled_n) if pooled_n else None,
        "pooled_buy_mean_r": (statistics.mean(pooled_vals) if pooled_vals else None),
    }
