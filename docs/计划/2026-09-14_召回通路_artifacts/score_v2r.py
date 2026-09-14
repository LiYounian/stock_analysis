"""召回通路 A/B 打分:v2(强度打底)vs v2r(强度+召回),复用 shadow_score。

额外:召回子样本单独评(仅被召回票的买入/α),判召回是纯噪声还是接住赢家。
诚实标注样本量 N/SE。用法:PYTHONPATH=<worktree> python score_v2r.py
"""
from __future__ import annotations
import json
from pathlib import Path

from tools.analysis import shadow_score as ss

ART = Path(__file__).resolve().parent
V2_ART = ART.parent / "2026-09-14_候选池v2_AB_artifacts"
CARD = ss.load_scorecard("/Users/yqg/Documents/projects/stock_analysis/data/analysis/backtest/forward_scorecard.csv")
PROVIDER = "deepseek_v4pro"
DAYS = ["2026-08-11", "2026-08-13", "2026-08-14", "2026-08-18", "2026-08-20",
        "2026-08-24", "2026-08-26", "2026-08-28", "2026-08-31", "2026-09-01",
        "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08", "2026-09-09", "2026-09-10"]


def _load(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def score_arm(units_by_day):
    buy_days, bull_days, per_day = [], [], []
    for d in DAYS:
        units = units_by_day.get(d)
        if units is None:
            continue
        rb = ss.score_day(d, units, CARD, select=ss.is_buy)
        rl = ss.score_day(d, units, CARD, select=ss.is_bull)
        buy_days.append(rb)
        bull_days.append(rl)
        h1 = rb["horizons"]["r_1"]
        per_day.append({"date": d, "n_units": rb["n_units"], "n_buy": rb["n_buy"],
                        "buy": rb["buy_side"], "alpha1": h1["alpha_pp"], "hit1": h1["hit_rate"]})
    return {
        "per_day": per_day,
        "buy_agg": {hz: ss.aggregate(buy_days, horizon=hz) for hz in ("r_1", "r_5")},
        "bull_agg": {hz: ss.aggregate(bull_days, horizon=hz) for hz in ("r_1", "r_5")},
        "days_with_units": len(buy_days),
        "days_with_ge1_buy": sum(1 for r in buy_days if r["n_buy"] >= 1),
        "total_buys": sum(r["n_buy"] for r in buy_days),
    }


def collect(art, ver):
    by = {}
    for d in DAYS:
        u = _load(f"{art}/units_{PROVIDER}_{ver}_{d}.json")
        if u is not None:
            by[d] = u
    return by


def pool_sizes(art, ver):
    out = {}
    for d in DAYS:
        j = _load(f"{art}/pool_{ver}_{d}.json")
        if j:
            out[d] = len(j.get("pool", []))
    return out


def recall_subsample():
    """仅看被召回票:数量、被买入数、买入侧 r_1 均值/命中率、其中事后赢家(次日≥5%)数。"""
    import statistics
    n_recalled = n_buy = n_win5 = 0
    buy_r = []
    per_day = []
    for d in DAYS:
        pj = _load(f"{ART}/pool_v2r_{d}.json")
        uj = _load(f"{ART}/units_{PROVIDER}_v2r_{d}.json")
        if not pj or uj is None:
            continue
        recalled = {x["code"] for x in pj.get("recalled", [])}
        card = CARD.get(d, {})
        n_recalled += len(recalled)
        day_buy = 0
        for u in uj:
            c = u.get("code")
            if c not in recalled:
                continue
            r1 = card.get(c, {}).get("r_1")
            if r1 is not None and r1 >= 5.0:
                n_win5 += 1
            if ss.is_buy(u):
                n_buy += 1
                day_buy += 1
                if r1 is not None:
                    buy_r.append(r1)
        per_day.append({"date": d, "n_recalled": len(recalled), "n_buy_recall": day_buy})
    return {
        "n_recalled_total": n_recalled,
        "n_recalled_win5": n_win5,
        "n_buy_from_recall": n_buy,
        "buy_from_recall_mean_r1": statistics.mean(buy_r) if buy_r else None,
        "buy_from_recall_hit": (sum(1 for r in buy_r if r > 0) / len(buy_r)) if buy_r else None,
        "buy_from_recall_n_scored": len(buy_r),
        "per_day": per_day,
    }


def main():
    v2 = score_arm(collect(str(V2_ART), "v2"))
    v2r = score_arm(collect(str(ART), "v2r"))
    v2_sz = pool_sizes(str(V2_ART), "v2")
    v2r_sz = pool_sizes(str(ART), "v2r")

    def fmt(x, p="{:+.3f}"):
        return p.format(x) if isinstance(x, (int, float)) else "—"

    print("=== 候选池规模 & 买入数/日(v2 强度打底 vs v2r 强度+召回)===")
    print(f"{'date':12} {'v2_pool':>7} {'v2_buy':>6} {'v2_α1':>7} | "
          f"{'v2r_pool':>8} {'v2r_buy':>7} {'v2r_α1':>7}")
    v2d = {r["date"]: r for r in v2["per_day"]}
    v2rd = {r["date"]: r for r in v2r["per_day"]}
    for d in DAYS:
        a, b = v2d.get(d, {}), v2rd.get(d, {})
        print(f"{d:12} {v2_sz.get(d,'-'):>7} {a.get('n_buy','-'):>6} {fmt(a.get('alpha1')):>7} | "
              f"{v2r_sz.get(d,'-'):>8} {b.get('n_buy','-'):>7} {fmt(b.get('alpha1')):>7}")

    def summ(name, arm):
        b1, b5 = arm["buy_agg"]["r_1"], arm["buy_agg"]["r_5"]
        lb1 = arm["bull_agg"]["r_1"]
        print(f"\n--- {name} ---")
        print(f"  有 units 日={arm['days_with_units']}  ≥1买入日={arm['days_with_ge1_buy']}  总买入票={arm['total_buys']}")
        print(f"  买入侧 r_1: 日均α={fmt(b1['day_mean_alpha_pp'])}±{fmt(b1['day_se_alpha_pp'],'{:.3f}')} "
              f"(n_days={b1['n_days']}) pooled_n={b1['pooled_n_buys']} hit={fmt(b1['pooled_hit_rate'],'{:.2f}')}")
        print(f"  买入侧 r_5: 日均α={fmt(b5['day_mean_alpha_pp'])}±{fmt(b5['day_se_alpha_pp'],'{:.3f}')} "
              f"(n_days={b5['n_days']}) pooled_n={b5['pooled_n_buys']}")
        print(f"  看多方向侧 r_1: 日均α={fmt(lb1['day_mean_alpha_pp'])} pooled_n={lb1['pooled_n_buys']} "
              f"hit={fmt(lb1['pooled_hit_rate'],'{:.2f}')}")

    summ("v2 强度打底", v2)
    summ("v2r 强度+召回", v2r)

    rs = recall_subsample()
    print("\n--- 召回子样本(仅被召回票)---")
    print(f"  召回总票={rs['n_recalled_total']}  其中事后赢家(次日≥5%)={rs['n_recalled_win5']}")
    print(f"  被DeepSeek买入={rs['n_buy_from_recall']}  买入侧r_1均值={fmt(rs['buy_from_recall_mean_r1'])} "
          f"hit={fmt(rs['buy_from_recall_hit'],'{:.2f}')} (n_scored={rs['buy_from_recall_n_scored']})")

    out = {"v2": v2, "v2r": v2r, "v2_pool_sizes": v2_sz, "v2r_pool_sizes": v2r_sz,
           "recall_subsample": rs}
    (ART / "summary_v2r.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nwrote summary_v2r.json")


if __name__ == "__main__":
    main()
