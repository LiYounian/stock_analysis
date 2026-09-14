"""编排:capitulation 标记 + H1 + H2,跨先验网格全报,落 JSON 到隔离 artifacts。

用法:
  PYTHONPATH=. python -m tools.backtest.capitulation.run \
      --breadth data/backtest_local/capitulation/breadth_2018_2026.parquet \
      --panels  data/backtest_local/capitulation/panels \
      --out     data/backtest_local/capitulation/results.json \
      --oos-start 2020-01-01
所有输出隔离到 backtest_local,不污染主仓。⚠️ 研究模拟,非投资建议。
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

from tools.backtest.capitulation.breadth_features import (
    build_capitulation_flags, event_dates, ordinary_dates, GRID, _grid_key)
from tools.backtest.capitulation.dataset import build_panels
from tools.backtest.capitulation.h1_bottom import compare_groups
from tools.backtest.capitulation.h2_gates import run_h2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--breadth", required=True)
    ap.add_argument("--panels", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--oos-start", default="2020-01-01")
    ap.add_argument("--horizons", default="1,5")
    ap.add_argument("--data-root", default=None)
    args = ap.parse_args()
    horizons = tuple(int(x) for x in args.horizons.split(","))

    breadth = pd.read_parquet(args.breadth)
    if "date" in breadth.columns:
        breadth = breadth.set_index("date")
    breadth.index = pd.to_datetime(breadth.index)
    flags = build_capitulation_flags(breadth)

    panels = build_panels(args.panels, data_root=args.data_root)
    close_panel, oversold_panel = panels["close"], panels["oversold"]
    from tools.backtest.capitulation.forward import ForwardBook
    book = ForwardBook(close_panel, horizons, lag=1)

    results = {"meta": {"oos_start": args.oos_start, "horizons": list(horizons),
                        "grid": GRID, "n_trading_days": int(len(breadth)),
                        "breadth_span": [str(breadth.index.min().date()),
                                         str(breadth.index.max().date())]},
               "event_counts": {}, "H1": {}, "H2": {}}

    for q_os, q_crash in GRID:
        key = _grid_key(q_os, q_crash)
        cap = event_dates(flags, key)
        ordd = ordinary_dates(flags, key)
        cap_oos = [d for d in cap if d >= pd.Timestamp(args.oos_start)]
        ord_oos = [d for d in ordd if d >= pd.Timestamp(args.oos_start)]
        results["event_counts"][key] = {
            "capitulation_all": len(cap), "capitulation_oos": len(cap_oos),
            "ordinary_down_oos": len(ord_oos)}
        results["H1"][key] = compare_groups(
            book, oversold_panel, cap, ordd, horizons, args.oos_start)
        results["H2"][key] = run_h2(
            panels, oversold_panel, cap, horizons, oos_start=args.oos_start, book=book)

    with open(args.out, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=1, default=str)
    print(f"[run] 写 {args.out} · 网格 {len(GRID)} 组合", flush=True)
    # 打印事件计数速览
    for key, ec in results["event_counts"].items():
        print(f"  {key}: cap_all={ec['capitulation_all']} "
              f"cap_oos={ec['capitulation_oos']} ord_oos={ec['ordinary_down_oos']}", flush=True)


if __name__ == "__main__":
    main()
