"""S5 选股合成 CLI:`python -m tools.analysis.selection_synth_run --date YYYY-MM-DD`。

真跑走网关(zsh -ic env,DeepSeek 默认模型);离线测试用 monkeypatch(见 tests)。
--data-root 指主仓数据根(worktree 本地 data 空)。--boards 逗号分隔限定板块。
--no-write 只算不落盘。
"""
from __future__ import annotations

import argparse
import logging
import sys

import pandas as pd


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="S5 选股合成(消息×策略×财报×形态·DeepSeek)")
    ap.add_argument("--date", help="决策日 YYYY-MM-DD(默认今天)")
    ap.add_argument("--data-root", help="数据根(worktree 指主仓;缺省自动探测)")
    ap.add_argument("--boards", help="逗号分隔限定利好板块(缺省=消息驱动块全部)")
    ap.add_argument("--no-write", action="store_true", help="只算不落盘")
    a = ap.parse_args(argv)

    date = a.date or pd.Timestamp.today().strftime("%Y-%m-%d")
    from tools.analysis.market_forecast import dataroot
    root = dataroot.ensure_data_root(a.data_root)
    boards = [b.strip() for b in a.boards.split(",") if b.strip()] if a.boards else None

    from tools.analysis import selection_synth
    result = selection_synth.run(date, data_root=root, boards=boards,
                                 write=not a.no_write)
    n_pick = sum(1 for b in result.get("板块", []) for s in b.get("个股", [])
                 if s.get("档") == "推荐")
    n_cut = sum(1 for b in result.get("板块", []) for s in b.get("个股", [])
                if s.get("档") == "剔除")
    print(f"完成:板块 {len(result.get('板块', []))} / 推荐 {n_pick} / 剔除 {n_cut}")
    if result.get("_产物"):
        print(f"产物:{result['_产物']['md']}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
