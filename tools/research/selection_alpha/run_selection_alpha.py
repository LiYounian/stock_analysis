"""选股真实次日 α 诊断 · 编排入口。

流程:load 真实选股候选流 → 一次性 build 全A宇宙 → 次日执行口径评估 → 机械动量 baseline
配对 → 聚合(日聚类)+ OOS + 分维 → 落 events + summary(data/backtest_local/selection_alpha/)。

用法:
  python -m tools.research.selection_alpha.run_selection_alpha \
      --data-root /Users/yqg/Documents/projects/stock_analysis \
      --repo-root <本worktree> --out-dir data/backtest_local/selection_alpha

⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import argparse
import json
import os
import numpy as np
import pandas as pd

from . import picks_loader as pl
from . import nextday_exec as nx
from . import baseline as bl
from . import aggregate as agg

RULES = ("limit_pc_0.0", "limit_pc_0.01", "open")


def _buffer_start(start: str) -> str:
    """β/MA60 需前置历史;min_date 往前推约 4 个月由 kernel 行裁保证,这里返回 start 即可。"""
    return start


def run(data_root: str, repo_root: str, out_dir: str, start: str, end: str,
        cost_bps: float, oos_split: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    # 1) 真实选股候选流
    core = pl.load_picks(data_root, start=start, end=end)
    md = pl.load_md_picks(repo_root, start=start, end=end)
    picks = pd.concat([core, md], ignore_index=True)
    print(f"[picks] core={len(core)} md={len(md)} strategies={picks.strategy.nunique()} "
          f"dates={picks.date.nunique()} range={picks.date.min()}..{picks.date.max()}")

    # 2) 全A宇宙(一次性)
    uni = nx.Universe(data_root, min_date=start)
    max_exec = uni.max_settled_date()
    print(f"[universe] codes={len(uni.feats)} max_settled_exec={max_exec}")

    # 3) 评估真实选股
    ev = uni.evaluate(picks, cost_bps=cost_bps, rules=RULES, max_exec_date=max_exec)
    ev.to_parquet(os.path.join(out_dir, "events_picks.parquet"), index=False)
    print(f"[events] our={len(ev)} rows ({ev.date.nunique()} decision days)")

    # 4) 机械动量 baseline:每决策日取数 = 当日核心并集入选数
    core_only = picks[picks.strategy != "md_blended"]
    n_by_day = core_only.groupby("date")["code"].nunique().to_dict()
    base_picks = bl.momentum_proxy_picks(uni, sorted(n_by_day), n_by_day)
    base_ev = uni.evaluate(base_picks, cost_bps=cost_bps, rules=RULES, max_exec_date=max_exec)
    base_ev.to_parquet(os.path.join(out_dir, "events_baseline.parquet"), index=False)
    print(f"[baseline] mom picks={len(base_picks)} events={len(base_ev)}")

    # 5) 聚合
    summary: dict = {"meta": dict(start=start, end=end, cost_bps=cost_bps,
                                  oos_split=oos_split, max_exec=max_exec,
                                  n_core=len(core), n_md=len(md), n_universe=len(uni.feats),
                                  non_investment_advice=True)}
    for rule in RULES:
        s = agg.summarize(ev, rule)
        oos = agg.oos_split(ev, rule, oos_split)
        paired = agg.paired_vs_baseline(ev, base_ev, rule)
        base_all = agg.summarize(base_ev, rule)
        base_row = base_all[base_all.strategy == "__ALL_CORE__"].to_dict("records")
        summary[rule] = dict(
            by_strategy=s.to_dict("records"),
            oos=oos.to_dict("records"),
            vs_baseline_paired=paired,
            baseline_all=base_row[0] if base_row else {},
        )
        print(f"\n===== rule={rule} (buyable-only, net of {cost_bps}bps) =====")
        with pd.option_context("display.width", 200, "display.max_columns", 30):
            print(s.to_string(index=False))
        print(f"  OOS: {oos.to_dict('records')}")
        print(f"  vs 动量baseline(配对): {paired}")

    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, default=_jsond)
    print(f"\n[out] {out_dir}/summary.json")
    return summary


def _jsond(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", required=True, help="主仓根(读 data/master/kline + data/analysis)")
    ap.add_argument("--repo-root", default=None, help="本 worktree 根(读 docs md 锚点),默认=data-root")
    ap.add_argument("--out-dir", default="data/backtest_local/selection_alpha")
    ap.add_argument("--start", default="2026-08-08")
    ap.add_argument("--end", default="2026-09-14")
    ap.add_argument("--cost-bps", type=float, default=10.0)
    ap.add_argument("--oos-split", default="2026-09-04")
    args = ap.parse_args(argv)
    repo_root = args.repo_root or args.data_root
    run(args.data_root, repo_root, args.out_dir, args.start, args.end,
        args.cost_bps, args.oos_split)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
