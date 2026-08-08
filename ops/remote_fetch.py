"""远端数据仓库常驻采集入口(oneshot,由 stock-fetch.timer 周期触发)。

远端数据仓库 Phase 1 的"远端常驻采集"半环:远端机每 30min/1h 跑一次——
  ① 交易日守卫:非交易日直接跳过(不触网、不写库),交易日才采集
  ② 交易日:全A 当日增量 append 到滚动主档(market.update_master_from_spot,按 date 幂等)
  ③ 可选 --backfill:首次部署时用 baostock 逐只拉全历史落主档(market.backfill_master)

只编排、不重写采集层:采集逻辑全在 tools.collectors.market。采集完的主档由本地端
`python -m tools.sync.pull` 经 ingest /pull 增量拉走(见 tools/sync/pull.py)。

交易日判定:优先用 akshare 交易日历(tool_trade_date_hist_sina),不可用时回退
"工作日近似"(周一~周五视为交易日,周末跳过)——回退会漏节假日,但只影响
"多跑一次空 spot",幂等且无害;精确日历可用时以其为准。

CLI:python -m ops.remote_fetch [--date YYYY-MM-DD] [--backfill] [--force]
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime

logger = logging.getLogger("ops.remote_fetch")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def is_trading_day(date: str | None = None) -> bool:
    """A股交易日判定。优先 akshare 交易日历;不可用回退工作日近似(周末=非交易日)。

    回退近似只会把节假日误判为交易日(多跑一次空 spot,幂等无害),绝不会把交易日
    误判为休市而漏采;故对"常驻采集守卫"这一用途是安全侧偏。
    """
    d = datetime.strptime(date, "%Y-%m-%d").date() if date else datetime.now().date()
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()          # DataFrame,列 trade_date(datetime.date)
        import pandas as pd
        days = set(pd.to_datetime(cal["trade_date"]).dt.strftime("%Y-%m-%d"))
        return d.strftime("%Y-%m-%d") in days
    except Exception as ex:                            # 网络/依赖不可用 → 工作日近似兜底
        logger.warning("交易日历不可用(%s),回退工作日近似", ex)
        return d.weekday() < 5                         # 0=周一 … 4=周五


def run_fetch(date: str | None = None, *, backfill: bool = False, force: bool = False) -> dict:
    """跑一次远端采集。非交易日(且未 --force)→ 跳过。返回 {skipped|ok, ...}。"""
    date = date or _today()
    if not force and not is_trading_day(date):
        logger.info("非交易日 %s,跳过采集", date)
        return {"skipped": True, "reason": "not_trading_day", "date": date}

    from tools.collectors import market
    if backfill:
        from tools.collectors.universe import universe_codes
        codes = universe_codes()
        logger.info("首次全量回填主档(baostock 全历史):%d 只", len(codes))
        res = market.backfill_master(codes)
        logger.info("回填完成:%s", res)
        return {"ok": True, "mode": "backfill", "date": date, **res}

    logger.info("全A 当日增量 append 到主档 @ %s", date)
    res = market.update_master_from_spot(date=date)
    logger.info("增量完成:%s", res)
    return {"ok": True, "mode": "spot_increment", "date": date, **res}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="远端常驻采集:交易日守卫 → 全A 增量落主档")
    ap.add_argument("--date", default=None, help="采集日期 YYYY-MM-DD;缺省今天")
    ap.add_argument("--backfill", action="store_true", help="首次部署:baostock 逐只拉全历史落主档")
    ap.add_argument("--force", action="store_true", help="忽略交易日守卫强制采集(调试用)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    res = run_fetch(args.date, backfill=args.backfill, force=args.force)
    if res.get("skipped"):
        print(f"跳过采集({res['reason']}):{res['date']}")
        return 0
    print(f"采集完成 {res['date']}(mode={res['mode']}):{ {k: v for k, v in res.items() if k not in ('ok', 'mode', 'date')} }")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
