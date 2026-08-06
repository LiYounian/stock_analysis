"""新闻采集(个股新闻)。

数据源:akshare `stock_news_em`(东财)。pandas 3.0 默认 pyarrow 字符串会触发
akshare 内部正则报错,调用前关掉 `future.infer_string`(实测可绕过)。
落盘:走 store 层(kind="news",json,原始新闻保留供 L1 抽取),旁记 meta.source="eastmoney"。
契约见 docs/计划/P2C_新闻情绪LLM.md。
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.news")

_SOURCE = "eastmoney"  # 东财
_COL_MAP = {"新闻标题": "title", "新闻内容": "content", "发布时间": "time",
            "文章来源": "source", "新闻链接": "url"}


def _fetch_em(code: str) -> pd.DataFrame:
    """东财个股新闻。关掉 pyarrow 字符串推断以绕过 akshare 正则不兼容。"""
    pd.set_option("future.infer_string", False)
    import akshare as ak
    return ak.stock_news_em(symbol=code)


def fetch_news(codes: list[str], days: int | None = None) -> dict[str, list[dict]]:
    """拉取每票近 days 天新闻并落盘。

    输出:{code: [{title, content, time, source, url}, ...]}(按时间倒序)。
    单票失败记 logger 跳过,不中断整批。
    """
    settings.ensure_dirs()
    days = days or settings.NEWS_LOOKBACK_DAYS
    cutoff = (pd.Timestamp.today() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")

    out: dict[str, list[dict]] = {}
    failed: list[str] = []
    for code in codes:
        try:
            df = _fetch_em(code)
            items = []
            if df is not None and len(df):
                df = df.rename(columns=_COL_MAP)
                for _, r in df.iterrows():
                    t = str(r.get("time", ""))
                    if t[:10] < cutoff:      # 超窗丢弃
                        continue
                    items.append({"title": str(r.get("title", "")),
                                  "content": str(r.get("content", ""))[:2000],
                                  "time": t, "source": str(r.get("source", "")),
                                  "url": str(r.get("url", ""))})
                items.sort(key=lambda x: x["time"], reverse=True)
            store.put_raw("news", code, items, meta={"source": _SOURCE})
            out[code] = items
            logger.info("新闻 %s:%d 条", code, len(items))
        except Exception as e:
            failed.append(code)
            logger.error("新闻 %s 失败: %s", code, e)
        time.sleep(settings.FETCH_SLEEP_SEC)
    if failed:
        logger.warning("新闻拉取失败(%d): %s", len(failed), failed)
    return out


def load_news(code: str) -> list[dict]:
    """从本地缓存读单票新闻。缓存缺失抛 FileNotFoundError。"""
    return store.get_raw("news", code)
