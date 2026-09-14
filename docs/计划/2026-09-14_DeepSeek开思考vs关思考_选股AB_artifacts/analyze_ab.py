"""开思考 vs 关思考 · 五维对比分析(复用 shadow_score 打分口径)。

关思考臂:2026-09-14_候选池v2_AB_artifacts/units_deepseek_v4pro_v2_<date>.json
开思考臂:本目录/units_deepseek_v4pro_v2think_<date>.json
输出:analyze_ab.json + 控制台五维摘要。
"""
from __future__ import annotations
import json
import statistics
from pathlib import Path

from tools.analysis import shadow_score as ss

ART = Path(__file__).resolve().parent
OFF_ART = ART.parent / "2026-09-14_候选池v2_AB_artifacts"
CARD_PATH = "/Users/yqg/Documents/projects/stock_analysis/data/analysis/backtest/forward_scorecard.csv"
CARD = ss.load_scorecard(CARD_PATH)
DAYS = ["2026-08-11", "2026-08-13", "2026-08-14", "2026-08-18", "2026-08-20",
        "2026-08-24", "2026-08-26", "2026-08-28", "2026-08-31", "2026-09-01",
        "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08", "2026-09-09", "2026-09-10"]
STANCE_ORD = {"规避": 0, "观望": 1, "可参与": 2, "买入": 3}


def _load(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _by_code(units):
    return {u["code"]: u for u in (units or []) if u.get("code")}


# ---- 维度1:字段一致率 + 表态迁移 ----
def field_diff():
    fields = ["stance", "dir_1d", "dir_5d", "dir_1d_conf", "dir_5d_conf", "type"]
    agree = {f: [0, 0] for f in fields}   # [一致, 总]
    migrations = []                        # 边界票:买入侧从属变化
    both_n = 0
    for d in DAYS:
        off = _by_code(_load(OFF_ART / f"units_deepseek_v4pro_v2_{d}.json"))
        on = _by_code(_load(ART / f"units_deepseek_v4pro_v2think_{d}.json"))
        for code in set(off) & set(on):
            both_n += 1
            o, n = off[code], on[code]
            for f in fields:
                agree[f][1] += 1
                if o.get(f) == n.get(f):
                    agree[f][0] += 1
            o_buy = ss.is_buy(o)
            n_buy = ss.is_buy(n)
            if o_buy != n_buy:
                migrations.append({
                    "date": d, "code": code,
                    "off": o.get("stance"), "on": n.get("stance"),
                    "dir": "off→on 转买入侧" if n_buy else "off→on 退出买入侧",
                })
    return {
        "both_n": both_n,
        "agree_rate": {f: (a[0] / a[1] if a[1] else None) for f, a in agree.items()},
        "agree_raw": agree,
        "buy_migrations": migrations,
    }


# ---- 维度2:买入侧 α/命中(复用 shadow_score)----
def score_arm(loader):
    buy_days, bull_days = [], []
    per_day = []
    for d in DAYS:
        units = loader(d)
        if units is None:
            continue
        rb = ss.score_day(d, units, CARD, select=ss.is_buy)
        rl = ss.score_day(d, units, CARD, select=ss.is_bull)
        buy_days.append(rb)
        bull_days.append(rl)
        h1 = rb["horizons"]["r_1"]
        per_day.append({"date": d, "n_units": rb["n_units"], "n_buy": rb["n_buy"],
                        "buy": rb["buy_side"], "alpha1": h1["alpha_pp"],
                        "hit1": h1["hit_rate"]})
    return {
        "per_day": per_day,
        "buy_agg": {hz: ss.aggregate(buy_days, horizon=hz) for hz in ("r_1", "r_5")},
        "bull_agg": {hz: ss.aggregate(bull_days, horizon=hz) for hz in ("r_1", "r_5")},
        "days_with_units": len(buy_days),
        "days_with_ge1_buy": sum(1 for r in buy_days if r["n_buy"] >= 1),
        "total_buys": sum(r["n_buy"] for r in buy_days),
    }


# ---- 维度5:成本/时延 ----
def cost():
    lat, comp, reason, prompt = [], [], [], []
    polluted = empty = 0
    per_day = {}
    for d in DAYS:
        s = _load(ART / f"call_stats_{d}.json")
        if not s:
            continue
        dl = [x["latency_s"] for x in s if x.get("latency_s")]
        per_day[d] = {"n": len(s), "lat_mean": round(statistics.mean(dl), 1) if dl else None,
                      "lat_max": max(dl) if dl else None}
        for x in s:
            if x.get("latency_s"): lat.append(x["latency_s"])
            if x.get("completion_tok"): comp.append(x["completion_tok"])
            if x.get("reasoning_tok"): reason.append(x["reasoning_tok"])
            if x.get("prompt_tok"): prompt.append(x["prompt_tok"])
            polluted += 1 if x.get("content_polluted") else 0
            empty += 1 if x.get("content_empty") else 0
    n = len(lat)
    return {
        "n_calls": n,
        "lat_mean_s": round(statistics.mean(lat), 1) if lat else None,
        "lat_median_s": round(statistics.median(lat), 1) if lat else None,
        "lat_p90_s": round(sorted(lat)[int(0.9 * n)], 1) if n else None,
        "lat_max_s": max(lat) if lat else None,
        "completion_tok_mean": round(statistics.mean(comp)) if comp else None,
        "reasoning_tok_mean": round(statistics.mean(reason)) if reason else None,
        "prompt_tok_mean": round(statistics.mean(prompt)) if prompt else None,
        "total_tok": (sum(comp) + sum(prompt)),
        "content_polluted": polluted, "content_empty": empty,
        "per_day": per_day,
    }


def main():
    off = score_arm(lambda d: _load(OFF_ART / f"units_deepseek_v4pro_v2_{d}.json"))
    on = score_arm(lambda d: _load(ART / f"units_deepseek_v4pro_v2think_{d}.json"))
    fd = field_diff()
    cst = cost()

    def f(x, p="{:+.3f}"):
        return p.format(x) if isinstance(x, (int, float)) else "—"

    print("=== 维度1:字段一致率(开vs关,共同票 n=%d)===" % fd["both_n"])
    for k, v in fd["agree_rate"].items():
        print(f"  {k:14} {f(v,'{:.1%}') if v is not None else '—'}")
    print(f"  买入侧迁移 {len(fd['buy_migrations'])} 例:")
    for m in fd["buy_migrations"]:
        print(f"    {m['date']} {m['code']} {m['off']}→{m['on']} [{m['dir']}]")

    print("\n=== 维度2:买入数/日 & 买入侧α(关 | 开)===")
    offd = {r["date"]: r for r in off["per_day"]}
    ond = {r["date"]: r for r in on["per_day"]}
    print(f"{'date':12} {'off_buy':>7} {'off_α1':>8} | {'on_buy':>6} {'on_α1':>8}")
    for d in DAYS:
        a, b = offd.get(d, {}), ond.get(d, {})
        print(f"{d:12} {a.get('n_buy','-'):>7} {f(a.get('alpha1')):>8} | "
              f"{b.get('n_buy','-'):>6} {f(b.get('alpha1')):>8}")

    for name, arm in (("关思考", off), ("开思考", on)):
        b1, b5 = arm["buy_agg"]["r_1"], arm["buy_agg"]["r_5"]
        lb1 = arm["bull_agg"]["r_1"]
        print(f"\n--- {name} ---")
        print(f"  ≥1买入日={arm['days_with_ge1_buy']}/{arm['days_with_units']}  总买入票={arm['total_buys']}")
        print(f"  买入侧 r_1: 日均α={f(b1['day_mean_alpha_pp'])}±{f(b1['day_se_alpha_pp'],'{:.3f}')} "
              f"pooled_n={b1['pooled_n_buys']} hit={f(b1['pooled_hit_rate'],'{:.2f}')}")
        print(f"  买入侧 r_5: 日均α={f(b5['day_mean_alpha_pp'])} pooled_n={b5['pooled_n_buys']} "
              f"hit={f(b5['pooled_hit_rate'],'{:.2f}')}")
        print(f"  看多方向侧 r_1: 日均α={f(lb1['day_mean_alpha_pp'])} pooled_n={lb1['pooled_n_buys']} "
              f"hit={f(lb1['pooled_hit_rate'],'{:.2f}')}")

    print("\n=== 维度3:09-02(关思考 0 买入日)===")
    print(f"  关: buy={offd.get('2026-09-02',{}).get('buy')}")
    print(f"  开: buy={ond.get('2026-09-02',{}).get('buy')} α1={f(ond.get('2026-09-02',{}).get('alpha1'))}")

    print("\n=== 维度5:成本/时延(开思考)===")
    for k in ("n_calls", "lat_mean_s", "lat_median_s", "lat_p90_s", "lat_max_s",
              "completion_tok_mean", "reasoning_tok_mean", "prompt_tok_mean",
              "total_tok", "content_polluted", "content_empty"):
        print(f"  {k:22} {cst[k]}")

    out = {"off": off, "on": on, "field_diff": fd, "cost": cst}
    (ART / "analyze_ab.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nwrote analyze_ab.json")


if __name__ == "__main__":
    main()
