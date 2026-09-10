#!/usr/bin/env python
"""滚动主档 `turnover` 近端整段缺失(NaN)的**巡检 + 一次性回填**脚本。

背景:采集回退路径(`master_sync._advance_master_from_raw`,源=腾讯 fqkline)不返回
turnover,而其换源补齐网(`_enrich_turnover_amount` 走 baostock)是 best-effort、失败
只记日志 → 近端连续多个交易日 turnover 静默落 NaN(source=`fallback_advance`),但
**volume 在位**。全量实调(2026-09-04):5552 只中 5211 只近端整段缺 turnover,起点集中在
2026-08-13 / 2026-08-18 两波回退日,缺失行 volume 完好率 99.9%。

治标不靠外部流通股源:恒等式 `turnover% = 100 × volume / 流通股` ⇒ `turnover%/volume`
在流通股不变的窗口近似常数,故用本票自身**最近的正常行** ratio × 当日 volume 还原
turnover(见 `tools.config.units.backfill_turnover_from_volume`)。自证 0.00% 误差:用 gap
前 ratio 重建 603161 被 baostock 补齐过的两根近端行,recon 与实值完全一致。

治本仍在采集层(回退补齐网别再静默失败);本脚本只处理**已落 parquet 的历史缺失行**,
且只回填能被本票自身证明的行,其余诚实留 NaN(交下游有声降级)。

用法(默认 dry-run,**不写盘**):
    python -m ops.backfill_turnover                       # 全量巡检,只报告
    python -m ops.backfill_turnover --codes 603161,000001 # 只看/只回填指定票
    python -m ops.backfill_turnover --apply               # 真写盘(逐票原子写)
    python -m ops.backfill_turnover --json                # 报告输出 JSON

退出码:0 = 无缺失可回填;1 = 检出缺失(dry-run 下用于巡检报警);2 = 运行出错。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd

from tools.config import units
from tools.store import repo as store


def _write_alert_marker(path: str, summary: dict, reps: list) -> None:
    """把"当日 volume 自证回填量异常大"落成结构化告警文件(主动信号,非埋进大日志)。

    语义:每日兜底 step 正常应回填 ~0 行(补齐网在位时当日 turnover 不缺)。一旦单日
    回填量冲高,等价于**当日 baostock 补齐网大面积失效、靠 volume 自证兜底救回**——这正是
    需要主动看见的根因事件(补齐网 error 埋在 72MB pull_refresh.log 里没人翻)。marker 落在
    当日产物目录,供巡检/上层一眼可见;写失败不抛(告警不得拖垮兜底主流程)。
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "reason": "当日 turnover 自证回填量异常——疑 baostock 补齐网大面积失效,已用 volume 兜底救回",
            "summary": summary,
            "top_codes": [{"code": r["code"], "filled": r["filled"]}
                          for r in sorted(reps, key=lambda r: -r["filled"])[:20]],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:                            # 告警不得拖垮兜底主流程
        print(f"[WARN] 写 turnover 兜底告警 marker 失败({path}): "
              f"{type(e).__name__} {e}", file=sys.stderr)


def backfill_code(code: str, *, apply: bool) -> dict:
    """回填一票。apply=False 时只算不写。返回该票报告(filled=refused=0 表示无缺失)。"""
    df = store.get_master_kline(code)
    fixed, rep = units.backfill_turnover_from_volume(df)
    rep["code"] = code
    if apply and rep["filled"]:
        meta = store.get_master_kline_meta(code) or {}
        # 保留原 source/adjust 等来源信息,补一条回填留痕(不覆盖 fetched_at/rows 等由写入层重算的字段)
        keep = {k: v for k, v in meta.items()
                if k not in ("fetched_at", "code", "rows", "first_date", "last_date",
                             "turnover_unit_anomaly")}
        keep["turnover_backfilled"] = {
            "at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "method": "volume/流通股(本票自证 ratio)",
            "filled": rep["filled"], "refused": rep["refused"],
            "dates": rep["dates"][:20], "refused_dates": rep["refused_dates"][:20],
        }
        store.put_master_kline(code, fixed, meta=keep)
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="主档 turnover 近端缺失 volume÷流通股 回填")
    ap.add_argument("--codes", help="只处理这些票(逗号分隔);缺省=全部主档")
    ap.add_argument("--apply", action="store_true", help="真写盘(缺省只报告)")
    ap.add_argument("--json", dest="as_json", action="store_true", help="报告输出 JSON")
    ap.add_argument("--alert-marker", metavar="PATH",
                    help="回填量 ≥ --alert-threshold 时,把结构化告警写到此路径"
                         "(补齐网当日大面积失效的主动信号,非埋进大日志)")
    ap.add_argument("--alert-threshold", type=int, default=200,
                    help="rows_filled ≥ 此值视为补齐网当日大面积失效并触发 --alert-marker(默认 200)")
    args = ap.parse_args(argv)

    codes = ([c.strip() for c in args.codes.split(",") if c.strip()]
             if args.codes else store.list_master_codes())
    reps, dirty = [], 0
    tot_fill = tot_ref = 0
    for code in codes:
        try:
            rep = backfill_code(code, apply=args.apply)
        except FileNotFoundError:
            continue
        except Exception as e:                        # 单票失败不中断整批
            print(f"[ERR] {code}: {type(e).__name__} {e}", file=sys.stderr)
            continue
        if rep["filled"] or rep["refused"]:
            dirty += 1
            tot_fill += rep["filled"]
            tot_ref += rep["refused"]
            reps.append(rep)

    summary = {"codes_scanned": len(codes), "codes_dirty": dirty,
               "rows_filled": tot_fill, "rows_refused": tot_ref,
               "applied": bool(args.apply)}
    if args.as_json:
        print(json.dumps({"summary": summary, "details": reps},
                         ensure_ascii=False, indent=2))
    else:
        print(f"扫描 {summary['codes_scanned']} 票 → 有缺失 {dirty} 票;"
              f"可自证回填 {tot_fill} 行,无法自证 {tot_ref} 行;"
              f"{'已写盘' if args.apply else 'dry-run 未写盘'}")
        for rep in reps[:20]:
            print(f"  {rep['code']}: filled={rep['filled']} {rep['dates'][:5]}"
                  f" refused={rep['refused']} {rep['refused_dates'][:5]}")
        if len(reps) > 20:
            print(f"  ...(其余 {len(reps) - 20} 票省略,用 --json 看全量)")

    # 补齐网健康主动告警:回填量冲高 ⇒ 当日 baostock 补齐网大面积失效、靠本步救回。
    if args.alert_marker and tot_fill >= args.alert_threshold:
        _write_alert_marker(args.alert_marker, summary, reps)
        print(f"[ALERT] 当日 turnover 自证回填 {tot_fill} 行(≥{args.alert_threshold} 阈值)"
              f"——疑 baostock 补齐网大面积失效,已 volume 兜底救回;告警已写 {args.alert_marker}",
              file=sys.stderr)
    return 1 if dirty else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
