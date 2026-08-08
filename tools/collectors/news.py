"""新闻采集(个股新闻)。

主源:akshare `stock_news_em`(东财,个股维度)。pandas 3.0 默认 pyarrow 字符串
会触发 akshare 内部正则报错,调用前关掉 `future.infer_string`(实测可绕过)。
**备源(消单点)**:东财失败/无结果时回落 `stock_info_global_cls`(财联社电报,
全市场快讯,不同信源)按**股票名过滤**——电报无个股维度,只能名称子串命中,
召回低、属降级保底,但避免个股新闻全押东财一家。meta.source 记实际命中源。
落盘:走 store 层(kind="news",json,原始新闻保留供 L1 抽取)。
契约见 docs/计划/P2C_新闻情绪LLM.md。
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.news")

_SOURCE = "eastmoney"        # 东财(主源,个股维度)
_SOURCE_CLS = "财联社电报"    # 备源(全市场快讯,按名过滤)
_COL_MAP = {"新闻标题": "title", "新闻内容": "content", "发布时间": "time",
            "文章来源": "source", "新闻链接": "url"}


def _fetch_em(code: str) -> pd.DataFrame:
    """东财个股新闻。关掉 pyarrow 字符串推断以绕过 akshare 正则不兼容。"""
    pd.set_option("future.infer_string", False)
    import akshare as ak
    return ak.stock_news_em(symbol=code)


def _parse_em(df: pd.DataFrame, cutoff: str) -> list[dict]:
    """东财 df → 归一新闻条目(列映射 + 时间窗过滤 + 倒序)。"""
    items: list[dict] = []
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
    return items


def _fetch_cls(code: str, cutoff: str) -> list[dict]:
    """备源:财联社电报(全市场)按股票名过滤成个股新闻。

    电报无个股维度,只能按 stock_pool 里的股票名做子串命中(召回低,属降级)。
    命中条目归一成新闻契约(source=财联社电报,url 缺置空)。取不到名/无命中返回 []。
    """
    from tools.config import stock_pool
    s = stock_pool.get(code)
    name = s.name if s else ""
    if not name:
        return []
    pd.set_option("future.infer_string", False)
    import akshare as ak
    df = ak.stock_info_global_cls()
    items: list[dict] = []
    if df is not None and len(df):
        for _, r in df.iterrows():
            title = str(r.get("标题", ""))
            content = str(r.get("内容", ""))
            if name not in title and name not in content:
                continue
            d = str(r.get("发布日期", ""))
            if d and d < cutoff:
                continue
            items.append({"title": title or content[:30],
                          "content": content[:2000],
                          "time": f"{d} {str(r.get('发布时间', ''))}".strip(),
                          "source": _SOURCE_CLS, "url": ""})
        items.sort(key=lambda x: x["time"], reverse=True)
    return items


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
    n = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] 新闻 %s 采集...", i, n, code)
        src, items, err = _SOURCE, [], None
        # 主源:东财个股新闻
        try:
            items = _parse_em(_fetch_em(code), cutoff)
        except Exception as e:
            err = e
            logger.warning("新闻 %s 东财失败,尝试备源: %s", code, e)
        # 主源空/挂 → 备源:财联社电报按名过滤(降级保底,消单点)
        if not items:
            try:
                fb = _fetch_cls(code, cutoff)
                if fb:
                    items, src, err = fb, _SOURCE_CLS, None
            except Exception as e:
                logger.warning("新闻 %s 备源(财联社)失败: %s", code, e)
                err = err or e
        store.put_raw("news", code, items, meta={"source": src})
        out[code] = items
        if err and not items:                    # 两源皆挂且无数据才算失败(不静默)
            failed.append(code)
        logger.info("新闻 %s:%d 条(源=%s)", code, len(items), src)
        time.sleep(settings.FETCH_SLEEP_SEC)
    if failed:
        logger.warning("新闻拉取失败(%d): %s", len(failed), failed)
    return out


def load_news(code: str) -> list[dict]:
    """从本地缓存读单票新闻。缓存缺失抛 FileNotFoundError。"""
    return store.get_raw("news", code)
