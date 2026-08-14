"""K线图表数据视图(展示层用)。

架构约束(§3/§9.3):展示层只读、不算、不 import 分析器。故把「K线 + 均线」
在分析层预计算成视图落盘,web 只读该视图文件。
产物:data/analysis/chart/{code}.json = {dates, open, high, low, close, ma5, ma20, ma60, volume}
(最近 limit 根)。含 OHLC 以支持蜡烛图(candlestick),折线图取 close 即可,向后兼容。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.analysis import technical as ta
from tools.collectors import market
from tools.config import stock_pool

logger = logging.getLogger("analysis.chart")

_EMPTY = {"dates": [], "open": [], "high": [], "low": [], "close": [],
          "ma5": [], "ma20": [], "ma60": [], "volume": []}


def build_chart(code: str, limit: int = 120) -> dict:
    """读 K线 + 预算 MA5/20/60,返回图表序列(最近 limit 根)。缺失返回空。

    含 OHLC(open/high/low/close)以支持蜡烛图;折线图取 close。向后兼容旧折线视图。
    """
    try:
        df = market.load_kline_recent(code).copy()
    except FileNotFoundError:
        return dict(_EMPTY)
    for w in (5, 20, 60):
        df[f"ma{w}"] = ta.ma(df["close"], w)
    df = df.tail(limit)

    def col(c):
        return [None if pd.isna(v) else round(float(v), 2) for v in df[c]]

    return {
        "dates": [str(d)[:10] for d in df["date"]],
        "open": col("open"), "high": col("high"), "low": col("low"),
        "close": col("close"), "ma5": col("ma5"), "ma20": col("ma20"),
        "ma60": col("ma60"), "volume": [float(v) for v in df["volume"]],
    }


def write_charts(limit: int = 120, codes: list[str] | None = None) -> int:
    """给定票池(缺省全池)生成图表视图,经 store 按日期落盘。返回成功数。"""
    from tools.store import repo as store
    n = 0
    for code in (codes or stock_pool.get_codes()):
        data = build_chart(code, limit)
        if data["dates"]:
            store.put_code_view("chart", code, data)
            n += 1
    logger.info("K线图表视图落盘 %d 只(store 按日期)", n)
    return n
