import json, glob, sys
from pathlib import Path
sys.path.insert(0, "/Users/yqg/Documents/projects/stock_analysis/.claude/worktrees/keen-payne-1de045")
from tools.analysis import shadow_score as ss

ART = "/Users/yqg/Documents/projects/stock_analysis/.claude/worktrees/keen-payne-1de045/docs/计划/2026-09-13_headless历史日dryrun证据_扩样_artifacts"
CARD = ss.load_scorecard("/Users/yqg/Documents/projects/stock_analysis/data/analysis/backtest/forward_scorecard.csv")

files = sorted(glob.glob(f"{ART}/units_deepseek_v4pro_*.json"))
buy_days, bull_days = [], []
print("=== 逐日(买入侧 stance∈{买入,可参与} | 看多方向侧 dir=偏多)===")
rows = []
for f in files:
    d = f.split("_")[-1][:10]
    units = json.load(open(f))
    rb = ss.score_day(d, units, CARD, select=ss.is_buy)
    rl = ss.score_day(d, units, CARD, select=ss.is_bull)
    buy_days.append(rb); bull_days.append(rl)
    h1b = rb["horizons"]["r_1"]; h1l = rl["horizons"]["r_1"]
    rows.append((d, rb["n_units"], rb["n_buy"], rb["buy_side"],
                 h1b["alpha_pp"], h1b["hit_rate"], h1b["bench_mean"],
                 rl["n_buy"], h1l["alpha_pp"], h1l["hit_rate"]))
    ph = lambda x: f"{x:+.2f}" if isinstance(x,(int,float)) else "—"
    print(f"{d} u={rb['n_units']:2d} | BUY n={rb['n_buy']}({','.join(rb['buy_side'])}) "
          f"α1={ph(h1b['alpha_pp'])} hit={h1b['hit_rate']} bench={ph(h1b['bench_mean'])} "
          f"| BULL n={rl['n_buy']} α1={ph(h1l['alpha_pp'])} hit={ph(h1l['hit_rate'])}")

print("\n=== 汇总 · 买入侧(stance) ===")
for hz in ("r_1","r_5"):
    agg = ss.aggregate(buy_days, horizon=hz)
    print(hz, json.dumps(agg, ensure_ascii=False))
print("\n=== 汇总 · 看多方向侧(dir=偏多) ===")
for hz in ("r_1","r_5"):
    agg = ss.aggregate(bull_days, horizon=hz)
    print(hz, json.dumps(agg, ensure_ascii=False))

# 买入侧逐票收益明细
print("\n=== 买入侧逐票 r ===")
for rb in buy_days:
    for hz in ("r_1","r_5"):
        h=rb["horizons"].get(hz)
        if h and h["per_code"]:
            print(rb["date"], hz, h["per_code"], "bench", round(h["bench_mean"],3) if h["bench_mean"] else None)

# 存汇总 json
out = {
  "per_day": [{"date":r[0],"n_units":r[1],"buy":r[3],"alpha1":r[4],"hit1":r[5],
               "bull_n":r[7],"bull_alpha1":r[8]} for r in rows],
  "buy_agg": {hz: ss.aggregate(buy_days,horizon=hz) for hz in ("r_1","r_5")},
  "bull_agg": {hz: ss.aggregate(bull_days,horizon=hz) for hz in ("r_1","r_5")},
}
Path(f"{ART}/summary_deepseek.json").write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")
print("\nwrote summary_deepseek.json")
