"""开思考臂 · A/B 驱动:复用 v2 候选池(同 16 日同池)→ 注入 ThinkingClient 逐票研判。

- 候选池:直接读关思考臂已存的 pool_v2_<date>.json(保证两臂候选集完全一致,不重建)。
- 客户端:ThinkingClient(QWEN_BASE_URL compatible-mode + deepseek-v4-pro + 开思考),只注入不改生产。
- 并发:每日池内 ThreadPoolExecutor(WORKERS) 并行(思考 ~54s/次,串行 16 日太久)。
- 产出:units_deepseek_v4pro_v2think_<date>.json + call_stats_<date>.json,可续跑(存在即跳过)。
真调 LLM,须走交互式 zsh。--data-root 指生产只读;不碰生产/live/定时任务。
用法:PYTHONPATH=<worktree> python run_think.py [--dates d1,d2] [--workers N] [--force]
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from thinking_client import ThinkingClient
from tools.analysis import deep_analysis as da

WT = Path(__file__).resolve().parents[3]
ART = Path(__file__).resolve().parent
V2_ART = ART.parent / "2026-09-14_候选池v2_AB_artifacts"   # 关思考臂 pools 复用源
DR = "/Users/yqg/Documents/projects/stock_analysis/data/analysis"
EXP = "/Users/yqg/Documents/projects/stock_analysis/docs/每日分析/经验沉淀"
DAYS = ["2026-08-11", "2026-08-13", "2026-08-14", "2026-08-18", "2026-08-20",
        "2026-08-24", "2026-08-26", "2026-08-28", "2026-08-31", "2026-09-01",
        "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08", "2026-09-09", "2026-09-10"]


def _pool_for(date):
    p = V2_ART / f"pool_v2_{date}.json"
    return json.loads(p.read_text(encoding="utf-8")).get("pool", []) if p.exists() else []


def run_day(date, workers, force=False):
    out = ART / f"units_deepseek_v4pro_v2think_{date}.json"
    if out.exists() and out.stat().st_size > 2 and not force:
        print(f"[{date}] skip (exists)", file=sys.stderr)
        return 0
    pool = _pool_for(date)
    if not pool:
        out.write_text("[]", encoding="utf-8")
        print(f"[{date}] empty pool", file=sys.stderr)
        return 0
    cli = ThinkingClient(os.environ["QWEN_BASE_URL"], os.environ["QWEN_API_KEY"])
    t0 = time.time()
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(da.generate_one, code, date, client=cli,
                          data_root=Path(DR), experience_base=Path(EXP)): code
                for code in pool}
        for f in as_completed(futs):
            r = f.result()
            results[r.code] = r
    # 按池顺序落盘(稳定)
    ordered = [results[c] for c in pool if c in results]
    units = da.units_of(ordered)
    errs = [(r.code, r.error) for r in ordered if r.error]
    out.write_text(json.dumps(units, ensure_ascii=False, indent=2), encoding="utf-8")
    (ART / f"call_stats_{date}.json").write_text(
        json.dumps(cli.stats, ensure_ascii=False, indent=2), encoding="utf-8")
    buy = [u["code"] for u in units if u.get("stance") in ("买入", "可参与")]
    print(f"[{date}] pool={len(pool)} ok={len(units)} err={len(errs)} "
          f"buy={len(buy)}({','.join(buy)}) {time.time()-t0:.0f}s", file=sys.stderr)
    if errs:
        print(f"    errors: {errs}", file=sys.stderr)
    return len(ordered)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", default=",".join(DAYS))
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    total = 0
    for d in dates:
        try:
            total += run_day(d, args.workers, force=args.force)
        except Exception as e:  # noqa: BLE001
            print(f"[{d}] FAILED: {e}", file=sys.stderr)
    print(f"完成 {len(dates)} 日,约 {total} 次 LLM 调用", file=sys.stderr)


if __name__ == "__main__":
    main()
