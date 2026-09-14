"""候选池 v2 · A/B 驱动:逐日 build_pool_v2 → DeepSeek 自选 → 落隔离 artifacts(可续跑)。

复用 build_pool_v2(不改 shadow_run) + deep_analysis.generate。真调 LLM,须走交互式 zsh(网关 env)。
只写本 artifacts 目录;--data-root 指生产 data/analysis 只读;不碰生产/live/定时任务。
用法:PYTHONPATH=<worktree> python run_v2.py [--dates d1,d2] [--limit N] [--force]
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

from tools.analysis import shadow_pool_v2 as v2, deep_analysis as da

WT = Path(__file__).resolve().parents[3]
ART = Path(__file__).resolve().parent
DR = "/Users/yqg/Documents/projects/stock_analysis/data/analysis"
EXP = "/Users/yqg/Documents/projects/stock_analysis/docs/每日分析/经验沉淀"
PROVIDER = "deepseek_v4pro"
DAYS = ["2026-08-11", "2026-08-13", "2026-08-14", "2026-08-18", "2026-08-20",
        "2026-08-24", "2026-08-26", "2026-08-28", "2026-08-31", "2026-09-01",
        "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08", "2026-09-09", "2026-09-10"]


def run_day(date, limit=None, force=False):
    out = ART / f"units_{PROVIDER}_v2_{date}.json"
    pool, meta = v2.build_pool_v2(DR, date)
    (ART / f"pool_v2_{date}.json").write_text(
        json.dumps({"pool": pool, **meta}, ensure_ascii=False, indent=2), encoding="utf-8")
    if out.exists() and out.stat().st_size > 2 and not force:
        print(f"[{date}] skip units (exists) pool={len(pool)}", file=sys.stderr)
        return 0
    if limit:
        pool = pool[:limit]
    if not pool:
        out.write_text("[]", encoding="utf-8")
        print(f"[{date}] empty pool", file=sys.stderr)
        return 0
    t0 = time.time()
    results = da.generate(date, pool, provider_id=PROVIDER, data_root=Path(DR),
                          experience_base=Path(EXP))
    units = da.units_of(results)
    errs = [(r.code, r.error) for r in results if r.error]
    out.write_text(json.dumps(units, ensure_ascii=False, indent=2), encoding="utf-8")
    buy = [u["code"] for u in units if u.get("stance") in ("买入", "可参与")]
    print(f"[{date}] pool={len(pool)} ok={len(units)} err={len(errs)} "
          f"buy={len(buy)}({','.join(buy)}) {time.time()-t0:.0f}s", file=sys.stderr)
    if errs:
        print(f"    errors: {errs}", file=sys.stderr)
    return len(results)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", default=",".join(DAYS))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    total = 0
    for d in dates:
        try:
            total += run_day(d, limit=args.limit, force=args.force)
        except Exception as e:  # noqa: BLE001
            print(f"[{d}] FAILED: {e}", file=sys.stderr)
    print(f"完成 {len(dates)} 日,约 {total} 次 LLM 调用", file=sys.stderr)


if __name__ == "__main__":
    main()
