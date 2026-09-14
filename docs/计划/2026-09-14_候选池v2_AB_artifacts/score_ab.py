"""候选池 A/B 打分对比:v1(机械,复用扩样 units)vs v2(质量化,本轮 units)。

复用 shadow_score(买入侧 stance∈{买入,可参与} vs 全A等权 forward_scorecard)。
诚实标注样本量 N/SE;买入侧 α + 看多方向侧(dir=偏多)双报。
用法:PYTHONPATH=<worktree> python score_ab.py
"""
from __future__ import annotations
import json
import glob
from pathlib import Path

from tools.analysis import shadow_score as ss

AB = Path(__file__).resolve().parent
V1_ART = AB.parent / "2026-09-13_headless历史日dryrun证据_扩样_artifacts"
CARD_PATH = "/Users/yqg/Documents/projects/stock_analysis/data/analysis/backtest/forward_scorecard.csv"
CARD = ss.load_scorecard(CARD_PATH)

DAYS = ["2026-08-11", "2026-08-13", "2026-08-14", "2026-08-18", "2026-08-20",
        "2026-08-24", "2026-08-26", "2026-08-28", "2026-08-31", "2026-09-01",
        "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08", "2026-09-09", "2026-09-10"]


def _load(path):
    try:
        return json.load(open(path, encoding="utf-8"))
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
        per_day.append({
            "date": d, "n_units": rb["n_units"], "n_buy": rb["n_buy"],
            "buy": rb["buy_side"], "alpha1": h1["alpha_pp"], "hit1": h1["hit_rate"],
            "bench1": h1["bench_mean"], "bull_n": rl["n_buy"],
        })
    return {
        "per_day": per_day,
        "buy_agg": {hz: ss.aggregate(buy_days, horizon=hz) for hz in ("r_1", "r_5")},
        "bull_agg": {hz: ss.aggregate(bull_days, horizon=hz) for hz in ("r_1", "r_5")},
        "days_with_units": len(buy_days),
        "days_with_ge1_buy": sum(1 for r in buy_days if r["n_buy"] >= 1),
        "total_buys": sum(r["n_buy"] for r in buy_days),
    }


def collect(pattern_dir, tag):
    by_day = {}
    for d in DAYS:
        if tag == "v1":
            p = f"{pattern_dir}/units_deepseek_v4pro_{d}.json"
        else:
            p = f"{pattern_dir}/units_deepseek_v4pro_v2_{d}.json"
        u = _load(p)
        if u is not None:
            by_day[d] = u
    return by_day


def pool_sizes(pattern_dir, tag):
    out = {}
    for d in DAYS:
        p = (f"{pattern_dir}/pool_{d}.json" if tag == "v1"
             else f"{pattern_dir}/pool_v2_{d}.json")
        j = _load(p)
        if j:
            out[d] = len(j.get("pool", []))
    return out


def main():
    v1 = score_arm(collect(str(V1_ART), "v1"))
    v2 = score_arm(collect(str(AB), "v2"))
    v1_sz = pool_sizes(str(V1_ART), "v1")
    v2_sz = pool_sizes(str(AB), "v2")

    def fmt(x, p="{:+.3f}"):
        return p.format(x) if isinstance(x, (int, float)) else "—"

    print("=== 候选池规模 & 买入数/日(v1 机械 vs v2 质量化)===")
    print(f"{'date':12} {'v1_pool':>7} {'v1_buy':>6} {'v1_α1':>7} | "
          f"{'v2_pool':>7} {'v2_buy':>6} {'v2_α1':>7}")
    v1d = {r["date"]: r for r in v1["per_day"]}
    v2d = {r["date"]: r for r in v2["per_day"]}
    for d in DAYS:
        a, b = v1d.get(d, {}), v2d.get(d, {})
        print(f"{d:12} {v1_sz.get(d,'-'):>7} {a.get('n_buy','-'):>6} "
              f"{fmt(a.get('alpha1')):>7} | {v2_sz.get(d,'-'):>7} "
              f"{b.get('n_buy','-'):>6} {fmt(b.get('alpha1')):>7}")

    def summ(name, arm):
        b1 = arm["buy_agg"]["r_1"]
        b5 = arm["buy_agg"]["r_5"]
        print(f"\n--- {name} ---")
        print(f"  有 units 日={arm['days_with_units']}  ≥1买入日={arm['days_with_ge1_buy']}  "
              f"总买入票={arm['total_buys']}")
        print(f"  买入侧 r_1: 日均α={fmt(b1['day_mean_alpha_pp'])}±{fmt(b1['day_se_alpha_pp'],'{:.3f}')} "
              f"(n_days={b1['n_days']}) pooled_n={b1['pooled_n_buys']} "
              f"hit={fmt(b1['pooled_hit_rate'],'{:.2f}')}")
        print(f"  买入侧 r_5: 日均α={fmt(b5['day_mean_alpha_pp'])}±{fmt(b5['day_se_alpha_pp'],'{:.3f}')} "
              f"(n_days={b5['n_days']}) pooled_n={b5['pooled_n_buys']}")
        lb1 = arm["bull_agg"]["r_1"]
        print(f"  看多方向侧 r_1: 日均α={fmt(lb1['day_mean_alpha_pp'])} pooled_n={lb1['pooled_n_buys']} "
              f"hit={fmt(lb1['pooled_hit_rate'],'{:.2f}')}")

    summ("v1 机械候选池", v1)
    summ("v2 质量化候选池", v2)

    out = {"v1": v1, "v2": v2, "v1_pool_sizes": v1_sz, "v2_pool_sizes": v2_sz}
    (AB / "summary_ab.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nwrote summary_ab.json")


if __name__ == "__main__":
    main()
