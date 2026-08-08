"""交易日历守卫:判定某日是否 A 股交易日(周末/节假日不跑盘后闭环)。

数据源:akshare `tool_trade_date_hist_sina()`(沪深交易日历,含节假日)。
带本地缓存(data/raw/calendar/trade_dates.json),避免每次触网;缓存过期或缺失时刷新。
取不到(离线/接口变更)→ 回退"周一~周五"近似(节假日会误判为交易日,但不阻断闭环;
盘后闭环本身对非交易日只是空跑,近似偏保守方向可接受)。

对外:
    is_trading_day(date=None) -> bool     # 缺省今天
    trading_dates(...) -> set[str]        # 全部交易日(YYYY-MM-DD)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from tools.config import settings

logger = logging.getLogger("collectors.calendar")

# 缓存文件:交易日历不常变,默认 7 天刷新一次即可覆盖新年度日历发布
_CACHE_PATH = settings.DATA_RAW / "calendar" / "trade_dates.json"
_CACHE_TTL_DAYS = 7


def _norm(date=None) -> str:
    """归一化为 YYYY-MM-DD;None→今天。接受 YYYYMMDD / YYYY-MM-DD / datetime。"""
    if date is None:
        return datetime.now().strftime("%Y-%m-%d")
    if isinstance(date, datetime):
        return date.strftime("%Y-%m-%d")
    s = str(date)
    if "-" not in s and len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def _weekday_approx(d: str) -> bool:
    """回退近似:周一~周五视为交易日(无法识别节假日,偏保守——宁可空跑不漏跑)。"""
    return datetime.strptime(d, "%Y-%m-%d").weekday() < 5


def _load_cache() -> dict | None:
    if not _CACHE_PATH.exists():
        return None
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("交易日历缓存读取失败:%s", e)
        return None


def _cache_stale(cache: dict) -> bool:
    ts = cache.get("fetched_at")
    if not ts:
        return True
    try:
        return datetime.now() - datetime.fromisoformat(ts) > timedelta(days=_CACHE_TTL_DAYS)
    except Exception:
        return True


def _save_cache(dates: list[str]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps({"fetched_at": datetime.now().isoformat(), "dates": dates},
                       ensure_ascii=False),
            encoding="utf-8")
    except Exception as e:
        logger.warning("交易日历缓存写入失败(不影响判定):%s", e)


def _fetch_from_akshare() -> list[str]:
    """akshare 拉全量交易日历,归一化为 YYYY-MM-DD 字符串列表。"""
    import akshare as ak
    import pandas as pd
    df = ak.tool_trade_date_hist_sina()
    if df is None or len(df) == 0:
        raise ConnectionError("akshare 交易日历为空")
    col = "trade_date" if "trade_date" in df.columns else df.columns[0]
    return [pd.to_datetime(x).strftime("%Y-%m-%d") for x in df[col]]


def trading_dates(*, allow_fetch: bool = True, refresh: bool = False) -> set[str]:
    """全部交易日集合(YYYY-MM-DD)。

    优先用未过期缓存;过期/缺失且 allow_fetch → 采集刷新;采集失败但有旧缓存 → 用旧缓存;
    彻底拿不到 → 返回空集(调用方据此回退周内近似)。
    """
    cache = _load_cache()
    if cache and cache.get("dates") and not refresh and not _cache_stale(cache):
        return set(cache["dates"])
    if allow_fetch:
        try:
            dates = _fetch_from_akshare()
            _save_cache(dates)
            logger.info("交易日历刷新:%d 个交易日", len(dates))
            return set(dates)
        except Exception as e:
            logger.warning("交易日历采集失败,尝试用旧缓存/近似:%s", e)
    if cache and cache.get("dates"):
        return set(cache["dates"])
    return set()


def is_trading_day(date=None, *, allow_fetch: bool = True) -> bool:
    """date(缺省今天)是否为 A 股交易日。

    日历可用 → 精确判定;日历取不到(空集)→ 回退周一~周五近似并 WARNING。
    """
    d = _norm(date)
    try:
        dates = trading_dates(allow_fetch=allow_fetch)
        if dates:
            return d in dates
    except Exception as e:
        logger.warning("交易日历判定异常,回退周内近似:%s", e)
    logger.warning("交易日历不可用,回退'周一~周五'近似判定 %s", d)
    return _weekday_approx(d)
