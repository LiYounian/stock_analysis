"""票池增删的编排层(与 run.py 同级)。

职责:改票池 →(新增时联网采集该票 / 删除时清该票缓存)→ 重建 data/analysis 全部产物。
web 展示层的写操作(POST /api/pool)委托本模块完成,故 web 不直接 import 采集/分析层。
向下依赖:config.stock_pool · collectors · analysis · store;向上不被分析层依赖。

日期分区:存储按 <日期>/ 分区,web 读最新分区。故增删都锚定到「最新分析日期」
(无则今天),经 store.set_active_date 让采集/重建都落到同一分区,改动即时可见。
"""
from __future__ import annotations

import logging
from datetime import datetime

from tools.config import stock_pool
from tools.store import repo

logger = logging.getLogger("pool_service")


def _target_date() -> str:
    """增删锚定的日期分区 = 最新分析日期 > 最新 raw 日期 > 今天。"""
    dates = repo.list_dates("analysis") or repo.list_dates("raw")
    return dates[-1] if dates else datetime.now().strftime("%Y-%m-%d")


def rebuild_artifacts(date: str) -> None:
    """离线重建指定日期分区的全部产物(不触网):中心记录 + K线图表 + 横向总表 + 选股。

    增删票后调用,使总表/选股/概览随票池即时更新。全部读 raw 缓存,不联网。
    """
    from tools.analysis import chart, panel, portfolio, serialize
    from tools.screener import screen as sc

    codes = stock_pool.get_codes()
    repo.set_active_date(date)
    try:
        serialize.serialize_all(as_of=date, codes=codes)
        chart.write_charts(codes=codes)
        panel.write_panel(codes=codes)
        recs = {}
        for code in codes:
            try:
                recs[code] = serialize.load_record(code, date=date)
            except FileNotFoundError:
                pass
        agg = portfolio.aggregate(recs)
        presets = sc.run_presets(recs)
        repo.put_view("screen", {"aggregate": agg, "presets": presets}, date=date)
    finally:
        repo.set_active_date(None)
    logger.info("产物重建完成(日期 %s):%d 只", date, len(codes))


def collect_one(code: str, date: str) -> dict:
    """联网采集单票 K线/基本面/公告/资金流,落到指定日期分区。单项失败不阻断其余。"""
    from tools.collectors import announcement as an
    from tools.collectors import fundamental as fd
    from tools.collectors import fundflow as ff
    from tools.collectors import market

    result: dict[str, bool] = {}
    repo.set_active_date(date)
    try:
        for label, fn in (
            ("kline", market.fetch_kline),
            ("fundamental", fd.fetch_fundamental),
            ("announcement", an.fetch_announcements),
            ("fundflow", ff.fetch_fundflow),
        ):
            try:
                got = fn([code])
                result[label] = code in got
            except Exception as e:
                logger.error("采集 %s %s 失败: %s", label, code, e)
                result[label] = False
    finally:
        repo.set_active_date(None)
    logger.info("单票采集 %s(日期 %s): %s", code, date, result)
    return result


def add_and_collect(code: str, name: str, industry: str, sector: str,
                    market: str = "A") -> dict:
    """新增一只票 → 联网采集 → 重建产物。代码非法/重复抛 ValueError(由上层转 4xx)。"""
    s = stock_pool.add_stock(code, name, industry, sector, market=market)
    date = _target_date()
    collected = collect_one(s.code, date)
    rebuild_artifacts(date)
    return {
        "stock": {"code": s.code, "name": s.name, "industry": s.industry,
                  "sector": s.sector, "market": s.market},
        "date": date,
        "collected": collected,
    }


def enqueue_pending(op: str, code: str, *, name: str = "", industry: str = "",
                    sector: str = "", market: str = "A", source: str | None = None) -> dict:
    """远端模式(方案2):把加/删提案写入 pool_pending 表,**不采集不重建**。返回 {ok, queued, note}。

    远端网页写 config/stock_pool.json 会被 autoupdate 的 5min `git reset --hard` 抹掉,故改
    入队提案;本地闭环经 /pull 拉走 → 合并裁决 → add_and_collect/remove_and_cleanup(本地有 raw,
    panel 不塌)→ upload 带回执标 consumed。名单真源恒为本地 stock_pool.json。

    轻校验:op∈{add,remove}、code 非空(pool_pending_store.enqueue 兜底)。格式/重复的**深校验
    不在此做**——留给本地消化时的 stock_pool.add_stock(它才是名单真源的裁决者),避免两处校验漂移。
    """
    from tools.config import settings
    from tools.sync import pool_pending_store

    src = source or settings.POOL_PENDING_SOURCE
    rid = pool_pending_store.enqueue(code=code, name=name, industry=industry,
                                     sector=sector, market=market, op=op, source=src)
    logger.info("入队提案 #%d:%s %s(%s)", rid, op, code, market)
    return {"ok": True,
            "queued": {"id": rid, "op": op, "code": code, "market": market},
            "note": "已提交,将于下次本地闭环采集后生效(非实时)"}


def remove_and_cleanup(code: str) -> dict:
    """移除一只票 → 删除其 raw/analysis 缓存(全部日期分区)→ 重建产物。不存在抛 ValueError。"""
    s = stock_pool.remove_stock(code)
    removed = repo.delete_stock(s.code)
    date = _target_date()
    rebuild_artifacts(date)
    logger.info("删除 %s(%s),清理 %d 个文件", s.code, s.name, len(removed))
    return {
        "stock": {"code": s.code, "name": s.name},
        "removed_files": len(removed),
    }
