"""统一大复盘 CLI 编排：逐日 load_picks → model_a → attribution + native → render。

  loaders(读产物) → model_a(Model-A撮合+r_exit) → attribution(规则归因) → native(原生口径join)
  → render(每日复盘MD·docs/预测复盘/ + 汇总CSV·data/analysis/backtest/·幂等upsert)

搭路径本轮：脚本+测试跑通即可，全量 N 日运行等用户发令。防未来：只用 t≤date 数据 + 事后 master
kline；未到期 pending 留 None（再跑=回填幂等覆盖当日行）。
⚠️ 测试环境研究模拟，非投资建议。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from tools.review.attribution import classify
from tools.review.loaders import load_market_context, load_picks
from tools.review.model_a import compute_all
from tools.review.native import join_native
from tools.review.render import summarize, to_row, write_daily_md, write_grand_csv

logger = logging.getLogger("review.build_grand_review")


def review_date(date: str, data_root: str | None = None, include_curated: bool = True,
                build_ew: bool = False) -> list[dict]:
    """单日 → 逐票行（含 Model-A 收益 + 规则归因 + 原生口径）。"""
    picks = load_picks(date, data_root=data_root, include_curated=include_curated)
    if not picks:
        logger.warning("date=%s 无 picks（跳过）", date)
        return []
    market_ctx = load_market_context(date, data_root=data_root)
    labeled = compute_all(picks, data_root=data_root, build_if_missing=build_ew)
    native_cache: dict = {}
    rows: list[dict] = []
    for pick, lab in labeled:
        attr = classify(pick, lab, market_ctx=market_ctx)
        native = join_native(pick, data_root=data_root, cache=native_cache)
        rows.append(to_row(pick, lab, attr, native))
    return rows


def run(dates: list[str], data_root: str | None = None, include_curated: bool = True,
        build_ew: bool = False, csv_path: str | None = None, md_dir: str | None = None,
        write: bool = True) -> dict:
    """多日编排。write=False → 只算不落盘（dry-run）。返回 {rows, per_day_summary}。"""
    all_rows: list[dict] = []
    per_day: dict[str, dict] = {}
    for date in dates:
        rows = review_date(date, data_root=data_root, include_curated=include_curated,
                           build_ew=build_ew)
        if not rows:
            continue
        per_day[date] = summarize(rows)
        all_rows.extend(rows)
        if write:
            write_daily_md(date, rows, md_dir=md_dir)
    if write and all_rows:
        write_grand_csv(all_rows, csv_path=csv_path)
    return {"rows": all_rows, "per_day_summary": per_day}


def _parse_dates(args) -> list[str]:
    if args.dates:
        return [d.strip() for d in args.dates.split(",") if d.strip()]
    return [args.date]


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="统一大复盘生成路径（Model-A 口径 + 规则归因）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--date", help="单个复盘日 YYYY-MM-DD")
    g.add_argument("--dates", help="逗号分隔多日 YYYY-MM-DD,YYYY-MM-DD")
    ap.add_argument("--data-root", help="data/ 目录（worktree 指主仓 data/）")
    ap.add_argument("--out", help="汇总 CSV 路径，缺省 data/analysis/backtest/grand_review_scorecard.csv")
    ap.add_argument("--md-dir", help="每日复盘 MD 目录，缺省 docs/预测复盘/")
    ap.add_argument("--no-curated", action="store_true", help="不含统筹精选 v2/v3 对照版")
    ap.add_argument("--build-ew", action="store_true", help="market_ew.parquet 缺时尝试构建（否则 α=null）")
    ap.add_argument("--dry-run", action="store_true", help="只算不落盘（打印每日汇总）")
    args = ap.parse_args(argv)

    out = run(_parse_dates(args), data_root=args.data_root,
              include_curated=not args.no_curated, build_ew=args.build_ew,
              csv_path=args.out, md_dir=args.md_dir, write=not args.dry_run)
    print(json.dumps({"n_rows": len(out["rows"]), "per_day_summary": out["per_day_summary"]},
                     ensure_ascii=False, indent=2))
    return 0 if out["rows"] else 1


if __name__ == "__main__":
    sys.exit(main())
