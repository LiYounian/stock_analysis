"""Shadow-run 编排:逐日构候选池 → 指定 provider 逐票研判 → 落隔离 artifacts(可续跑)。

真调 LLM,须在交互式 zsh(网关 env)下跑;--data-root 指生产 data/analysis 只读。
不碰生产 / live SKILL / 定时任务;只写 --art 隔离目录。
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

from tools.analysis import shadow_pool, deep_analysis as da


def run_day(date, provider, data_root, art, exp_base, think):
    pool, meta = shadow_pool.build_pool(data_root, date)
    (Path(art) / f"pool_{date}.json").write_text(
        json.dumps({"pool": pool, **meta}, ensure_ascii=False, indent=2), encoding="utf-8")
    t0 = time.time()
    results = da.generate(date, pool, provider_id=provider,
                          enable_thinking=think, data_root=Path(data_root),
                          experience_base=Path(exp_base) if exp_base else None)
    units = da.units_of(results)
    errs = [(r.code, r.error) for r in results if r.error]
    out = Path(art) / f"units_{provider}_{date}.json"
    out.write_text(json.dumps(units, ensure_ascii=False, indent=2), encoding="utf-8")
    dt = time.time() - t0
    buy = [u["code"] for u in units if u.get("stance") in ("买入", "可参与")]
    print(f"[{date}] pool={len(pool)} ok={len(units)} err={len(errs)} "
          f"buy={len(buy)}({','.join(buy)}) {dt:.0f}s", file=sys.stderr)
    if errs:
        print(f"    errors: {errs}", file=sys.stderr)
    return len(results)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="deepseek_v4pro")
    ap.add_argument("--dates", required=True, help="逗号分隔 YYYY-MM-DD")
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--art", required=True)
    ap.add_argument("--experience-base")
    ap.add_argument("--think", choices=["on", "off"])
    ap.add_argument("--force", action="store_true", help="已有 units 也重跑")
    args = ap.parse_args(argv)
    Path(args.art).mkdir(parents=True, exist_ok=True)
    think = {"on": True, "off": False}.get(args.think)
    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    total = 0
    for d in dates:
        out = Path(args.art) / f"units_{args.provider}_{d}.json"
        if out.exists() and out.stat().st_size > 2 and not args.force:
            print(f"[{d}] skip (exists)", file=sys.stderr)
            continue
        try:
            total += run_day(d, args.provider, args.data_root, args.art,
                             args.experience_base, think)
        except Exception as e:  # noqa: BLE001
            print(f"[{d}] FAILED: {e}", file=sys.stderr)
    print(f"完成 {len(dates)} 日,约 {total} 次 LLM 调用", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
