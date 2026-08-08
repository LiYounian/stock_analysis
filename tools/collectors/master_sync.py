"""盘后闭环 K线采集编排:滚动主档 + spot 增量,失败回退逐只 akshare。

把已就绪的采集能力(market.backfill_master / fetch_spot_all+update_master_from_spot /
fetch_kline)编排成一条稳的闭环入口,决策 + fallback + 幂等由本模块负责,**不改** market.py
已有函数逻辑。

决策(sync_master):
  ① 主档缺失 / 覆盖不足 / 太旧 → backfill_master(baostock 全量落地,首次几分钟)。
  ② 否则 → fetch_spot_all + update_master_from_spot(当日增量 append,秒级,幂等)。
  ③ 后续分析走 market.load_kline(已优先读主档)。
必带 fallback:①/② 任一失败 → 回退现有逐只 akshare fetch_kline(可 workers 并发),
  绝不让闭环崩;降级 logger 明示。

返回 {"mode": "backfill"|"spot"|"fallback"|"noop", "ok": n, "failed"/"skipped": n, ...}。
"""
from __future__ import annotations

import logging
import os
import socket

import pandas as pd

from tools.collectors import market
from tools.store import repo as store

logger = logging.getLogger("collectors.master_sync")

# 主档判定阈值(可按需调)
_MIN_COVERAGE = 0.9      # 请求票在主档中的覆盖率下限,低于此视为需全量落地
_MAX_GAP_DAYS = 7        # 主档最新交易日距今超过此天数视为陈旧,需全量重算
_SAMPLE = 30             # 抽样多少只读 meta 判新鲜度(避免全量读)


def _latest_master_date(codes: list[str], master: set[str]):
    """抽样已有主档的 meta,取最新 last_date(Timestamp);全无则 None。"""
    present = [c for c in codes if c in master][:_SAMPLE]
    latest = None
    for c in present:
        meta = store.get_master_kline_meta(c)
        ld = (meta or {}).get("last_date")
        if not ld:
            continue
        t = pd.to_datetime(ld)
        if latest is None or t > latest:
            latest = t
    return latest


def _needs_backfill(codes: list[str], as_of: str) -> tuple[bool, str]:
    """判定是否需要 baostock 全量落地(vs spot 增量)。返回 (need, 原因)。"""
    master = set(store.list_master_codes())
    if not master:
        return True, "主档为空(首次落地)"
    present = sum(1 for c in codes if c in master)
    cov = present / max(1, len(codes))
    if cov < _MIN_COVERAGE:
        return True, f"主档覆盖不足({present}/{len(codes)}={cov:.0%})"
    latest = _latest_master_date(codes, master)
    if latest is None:
        return True, "主档缺 last_date 元数据"
    gap = (pd.Timestamp(as_of).normalize() - latest.normalize()).days
    if gap > _MAX_GAP_DAYS:
        return True, f"主档陈旧(最新 {latest.date()},距今 {gap} 天 > {_MAX_GAP_DAYS})"
    return False, f"主档就绪(覆盖 {cov:.0%},最新 {latest.date()})"


def _fetch_timeout() -> float:
    return float(os.getenv("FETCH_TIMEOUT", "10"))


def _fallback(codes: list[str], workers: int | None, reason: str) -> dict:
    """回退逐只 akshare fetch_kline(多源 fallback + 可选并发)。套采集期短超时快速失败。"""
    logger.warning("主档路径失败 → 回退逐只 akshare fetch_kline(%d 只,workers=%s):%s",
                   len(codes), workers, reason)
    _old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_fetch_timeout())
    try:
        out = market.fetch_kline(codes, workers=workers)
    finally:
        socket.setdefaulttimeout(_old)
    return {"mode": "fallback", "ok": len(out), "failed": len(codes) - len(out),
            "reason": reason}


def sync_master(codes: list[str], as_of: str | None = None, *,
                workers: int | None = None, fallback: bool = True) -> dict:
    """闭环 K线采集编排(主档 + spot 增量,失败回退逐只)。

    codes:本轮分析票池(全A 或子集)。as_of:当日 YYYY-MM-DD(缺省今天)。
    workers:回退逐只路径的并发度(None→settings.FETCH_WORKERS)。
    fallback=False:主档路径失败时不回退(直接抛出/返回失败),仅测试用。
    """
    if not codes:
        return {"mode": "noop", "ok": 0}
    as_of = as_of or pd.Timestamp.today().strftime("%Y-%m-%d")
    need, reason = _needs_backfill(codes, as_of)

    if need:
        logger.info("K线主档:全量落地(%s)→ backfill_master(baostock)", reason)
        try:
            r = market.backfill_master(codes)
            if r.get("ok", 0) == 0:
                raise RuntimeError(f"backfill 全失败({r})")
            logger.info("主档全量落地完成:成功 %d / 失败 %d", r.get("ok"), r.get("failed"))
            return {"mode": "backfill", **r}
        except Exception as e:
            if not fallback:
                raise
            return _fallback(codes, workers, f"backfill_master 异常: {e}")

    logger.info("K线主档:当日增量(%s)→ fetch_spot_all + update_master_from_spot", reason)
    try:
        spot = market.fetch_spot_all()
        r = market.update_master_from_spot(codes=codes, date=as_of, spot=spot)
        if r.get("ok", 0) == 0:
            raise RuntimeError(f"spot 增量 0 只更新({r})")
        logger.info("spot 当日增量完成:更新 %d / 跳过(停牌/无bar)%d @ %s",
                    r.get("ok"), r.get("skipped"), as_of)
        return {"mode": "spot", **r}
    except Exception as e:
        if not fallback:
            raise
        return _fallback(codes, workers, f"spot 增量异常: {e}")
