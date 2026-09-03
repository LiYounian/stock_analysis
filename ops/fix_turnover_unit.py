#!/usr/bin/env python
"""滚动主档 `turnover` 单位混用的**巡检 + 一次性修复**脚本。

背景:多个采集源的 turnover 原始口径不一致(sina 走 akshare `stock_zh_a_daily`,其
turnover = volume/流通股 是**小数**;baostock/东财/spot 是**百分数**),合并落主档时
没有归一 → 同一列里两种口径混存、相差 100 倍。采集层归一已在
`tools/config/units.py` + `tools/collectors/market.py` 治本(新写入不再混),
本脚本只处理**已经落在 parquet 里的历史脏行**。

判据不是"值小就可疑"(真实极低换手值也很小,且大量存在),而是
"该行 turnover 与**本票自身** volume→turnover 映射不自洽约 100 倍" ——
`turnover ≈ volume / 流通股 × 100` ⇒ `turnover/volume` 在流通股不变的区间近似常数。
详见 `tools/config/units.py` 模块 docstring(含全量分布实证与阈值依据)。

用法(默认 dry-run,**不写盘**):
    python -m ops.fix_turnover_unit                      # 全量巡检,只报告
    python -m ops.fix_turnover_unit --codes 603161,601882 # 只看/只修指定票
    python -m ops.fix_turnover_unit --apply              # 真写盘(逐票原子写)
    python -m ops.fix_turnover_unit --apply --keep-unresolved  # 无法自证的行原样保留

退出码:0 = 未检出异常;1 = 检出异常(dry-run 下用于巡检报警);2 = 运行出错。
"""
from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

from tools.config import units
from tools.store import repo as store


def scan_code(code: str) -> tuple[pd.DataFrame, dict]:
    """读一票主档,返回 (修复后的 df, 报告)。不写盘。"""
    df = store.get_master_kline(code)
    return df, units.scan_turnover_unit(df, code)


def fix_code(code: str, *, apply: bool, blank_unresolved: bool) -> dict:
    """修一票。apply=False 时只算不写。返回该票报告(rows=0 表示干净)。"""
    df = store.get_master_kline(code)
    fixed, rep = units.repair_turnover_unit(df, blank_unresolved=blank_unresolved)
    rep["code"] = code
    if apply and (rep["repaired"] or (blank_unresolved and rep["refused"])):
        meta = store.get_master_kline_meta(code) or {}
        # 保留原 source/adjust 等来源信息,补一条修复留痕
        keep = {k: v for k, v in meta.items()
                if k not in ("fetched_at", "code", "rows", "first_date", "last_date",
                             "turnover_unit_anomaly")}
        keep["turnover_unit_fixed"] = {
            "at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "repaired": rep["repaired"], "refused": rep["refused"],
            "dates": rep["dates"][:20], "refused_dates": rep["refused_dates"][:20],
        }
        store.put_master_kline(code, fixed, meta=keep)
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="主档 turnover 单位混用巡检/修复")
    ap.add_argument("--codes", help="只处理这些票(逗号分隔);缺省=全部主档")
    ap.add_argument("--apply", action="store_true", help="真写盘(缺省只报告)")
    ap.add_argument("--keep-unresolved", action="store_true",
                    help="无法自证修正倍数的行原样保留(缺省置 NaN 显式标记不可信)")
    ap.add_argument("--json", dest="as_json", action="store_true", help="报告输出 JSON")
    args = ap.parse_args(argv)

    codes = ([c.strip() for c in args.codes.split(",") if c.strip()]
             if args.codes else store.list_master_codes())
    reps, dirty = [], 0
    tot_rep = tot_ref = 0
    for code in codes:
        try:
            rep = fix_code(code, apply=args.apply,
                           blank_unresolved=not args.keep_unresolved)
        except FileNotFoundError:
            continue
        except Exception as e:                        # 单票失败不中断整批
            print(f"[ERR] {code}: {type(e).__name__} {e}", file=sys.stderr)
            continue
        if rep["repaired"] or rep["refused"]:
            dirty += 1
            tot_rep += rep["repaired"]
            tot_ref += rep["refused"]
            reps.append(rep)

    summary = {"codes_scanned": len(codes), "codes_dirty": dirty,
               "rows_repaired": tot_rep, "rows_refused": tot_ref,
               "applied": bool(args.apply)}
    if args.as_json:
        print(json.dumps({"summary": summary, "details": reps},
                         ensure_ascii=False, indent=2))
    else:
        print(f"扫描 {summary['codes_scanned']} 票 → 脏 {dirty} 票;"
              f"可证据修正 {tot_rep} 行,无法自证 {tot_ref} 行;"
              f"{'已写盘' if args.apply else 'dry-run 未写盘'}")
        for rep in reps[:20]:
            print(f"  {rep['code']}: repaired={rep['repaired']} {rep['dates'][:5]}"
                  f" refused={rep['refused']} {rep['refused_dates'][:5]}")
        if len(reps) > 20:
            print(f"  ...(其余 {len(reps) - 20} 票省略,用 --json 看全量)")
    return 1 if dirty else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
