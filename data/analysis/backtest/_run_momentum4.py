"""驱动:动量4回放当前口径评测 → 落 json。仅评测,复用 v3 打分层。非投资建议。"""
import json, logging, time
from tools.backtest.eval_v3 import replay_source as rs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
t0 = time.time()
agg, meta, scored = rs.run_momentum_replay(universe_n=800, lookback_days=250, stride=1, horizons=(1, 5))
elapsed = round(time.time() - t0, 1)
out = {"track": "replay", "strategy": "4·动量组合(A腿)", "replay_meta": meta,
       "agg": agg, "elapsed_sec": elapsed,
       "scored_rows": int(len(scored)), "带rank_score": bool(scored["rank_score"].notna().any()) if len(scored) else False}
p = "/Users/yqg/Documents/projects/worktrees/stock_analysis/feat/replay-momentum/data/analysis/backtest/eval_v3_momentum4.json"
with open(p, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("DONE", elapsed, "sec ->", p)
