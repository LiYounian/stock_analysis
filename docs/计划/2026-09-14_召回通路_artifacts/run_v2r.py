"""召回通路 · A/B 驱动:逐日 build_pool_v2r → 仅对**新召回票**跑 DeepSeek → 合成 v2r units。

省钱设计:v2 强度主池的 units 复用 v2 AB artifacts(units_deepseek_v4pro_v2_<日>.json,不重跑),
只对召回新增票(base 之外)真调 DeepSeek,再 merge。真调 LLM 须走交互式 zsh(网关 env)。
只写本 artifacts 目录;--data-root 指生产 data/analysis 只读;不碰生产/live/定时任务。
用法:PYTHONPATH=<worktree> python run_v2r.py [--dates d1,d2] [--force]
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

from tools.analysis import shadow_recall as sr, deep_analysis as da

ART = Path(__file__).resolve().parent
V2_ART = ART.parent / "2026-09-14_候选池v2_AB_artifacts"
DR = "/Users/yqg/Documents/projects/stock_analysis/data/analysis"
EXP = "/Users/yqg/Documents/projects/stock_analysis/docs/每日分析/经验沉淀"
PROVIDER = "deepseek_v4pro"
DAYS = ["2026-08-11", "2026-08-13", "2026-08-14", "2026-08-18", "2026-08-20",
        "2026-08-24", "2026-08-26", "2026-08-28", "2026-08-31", "2026-09-01",
        "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08", "2026-09-09", "2026-09-10"]


def run_day(date, force=False):
    pool, meta = sr.build_pool_v2r(DR, date)
    (ART / f"pool_v2r_{date}.json").write_text(
        json.dumps({"pool": pool, **meta}, ensure_ascii=False, indent=2), encoding="utf-8")
    out = ART / f"units_{PROVIDER}_v2r_{date}.json"
    if out.exists() and out.stat().st_size > 2 and not force:
        print(f"[{date}] skip units (exists) pool={len(pool)}", file=sys.stderr)
        return 0
    # 复用 v2 units(强度主池已研判过)
    v2_units_path = V2_ART / f"units_{PROVIDER}_v2_{date}.json"
    v2_units = json.loads(v2_units_path.read_text(encoding="utf-8")) if v2_units_path.exists() else []
    v2_codes = {u.get("code") for u in v2_units}
    recalled = [x["code"] for x in meta["recalled"]]
    todo = [c for c in recalled if c not in v2_codes]  # 只对新召回票跑
    new_units = []
    n_calls = 0
    if todo:
        t0 = time.time()
        results = da.generate(date, todo, provider_id=PROVIDER, data_root=Path(DR),
                              experience_base=Path(EXP))
        new_units = da.units_of(results)
        n_calls = len(results)
        errs = [(r.code, r.error) for r in results if r.error]
        if errs:
            print(f"[{date}] recall errors: {errs}", file=sys.stderr)
        dt = time.time() - t0
    else:
        dt = 0.0
    v2r_units = list(v2_units) + list(new_units)
    out.write_text(json.dumps(v2r_units, ensure_ascii=False, indent=2), encoding="utf-8")
    buy_all = [u["code"] for u in v2r_units if u.get("stance") in ("买入", "可参与")]
    buy_recall = [u["code"] for u in new_units if u.get("stance") in ("买入", "可参与")]
    print(f"[{date}] pool={len(pool)} base={meta['base_pool_size']} recall={len(recalled)} "
          f"newcalls={len(todo)} buy_total={len(buy_all)} buy_from_recall={len(buy_recall)}"
          f"({','.join(buy_recall)}) {dt:.0f}s", file=sys.stderr)
    return n_calls


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", default=",".join(DAYS))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    total = 0
    for d in dates:
        try:
            total += run_day(d, force=args.force)
        except Exception as e:  # noqa: BLE001
            print(f"[{d}] FAILED: {e}", file=sys.stderr)
    print(f"完成 {len(dates)} 日,约 {total} 次新增 LLM 调用", file=sys.stderr)


if __name__ == "__main__":
    main()
