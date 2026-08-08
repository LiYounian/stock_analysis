"""远端数据仓库常驻采集入口(oneshot,由 stock-fetch.timer 周期触发)。

远端数据仓库 Phase 1 的"远端常驻采集"半环:远端机每 30min/1h 跑一次——
  ① 交易日守卫:非交易日直接跳过(不触网、不写库),交易日才采集
     → 复用 `tools.collectors.calendar.is_trading_day`(akshare 交易日历 + 缓存,失败回退工作日近似)
  ② 交易日:全A 主档同步 → 复用 `tools.collectors.master_sync.sync_master`
     (主档缺失/太旧→baostock 全量回填;否则 spot 当日增量;**失败回退逐只 akshare**,幂等)
  ③ 可选 --backfill:强制 baostock 逐只全历史回填(market.backfill_master)

只编排、不重写:采集/守卫逻辑全在 tools.collectors(与本地闭环同一套,不另造轮子)。采集完的
主档由本地端 `python -m tools.sync.pull` 经 ingest /pull 增量拉走(见 tools/sync/pull.py)。

CLI:python -m ops.remote_fetch [--date YYYY-MM-DD] [--backfill] [--force]
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime

logger = logging.getLogger("ops.remote_fetch")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def run_fetch(date: str | None = None, *, backfill: bool = False, force: bool = False) -> dict:
    """跑一次远端采集。非交易日(且未 --force)→ 跳过。返回 {skipped|ok, mode, ...}。"""
    from tools.collectors import calendar as cal
    date = date or _today()
    if not force and not cal.is_trading_day(date):
        logger.info("非交易日 %s,跳过采集", date)
        return {"skipped": True, "reason": "not_trading_day", "date": date}

    from tools.collectors.universe import universe_codes
    codes = universe_codes()
    if backfill:
        from tools.collectors import market
        logger.info("首次全量回填主档(baostock 全历史):%d 只", len(codes))
        res = market.backfill_master(codes)
        logger.info("回填完成:%s", res)
        return {"ok": True, "mode": "backfill", "date": date, **res}

    from tools.collectors import master_sync
    logger.info("全A 主档同步(spot 增量,失败回退逐只)@ %s:%d 只", date, len(codes))
    res = master_sync.sync_master(codes, as_of=date)     # {mode: spot|backfill|fallback|noop, ok, ...}
    logger.info("主档同步完成:%s", res)
    return {"ok": True, "date": date, **res}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="远端常驻采集:交易日守卫 → 全A 主档同步(sync_master)")
    ap.add_argument("--date", default=None, help="采集日期 YYYY-MM-DD;缺省今天")
    ap.add_argument("--backfill", action="store_true", help="首次部署:baostock 逐只拉全历史落主档")
    ap.add_argument("--force", action="store_true", help="忽略交易日守卫强制采集(调试用)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    res = run_fetch(args.date, backfill=args.backfill, force=args.force)
    if res.get("skipped"):
        print(f"跳过采集({res['reason']}):{res['date']}")
        return 0
    print(f"采集完成 {res['date']}(mode={res.get('mode')}):"
          f"{ {k: v for k, v in res.items() if k not in ('ok', 'mode', 'date')} }")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
