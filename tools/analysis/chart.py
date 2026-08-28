"""K线图表数据视图(展示层用)。

架构约束(§3/§9.3):展示层只读、不算、不 import 分析器。故把「K线 + 均线 + 布林带
+ MACD + RSI + KDJ」在分析层预计算成视图落盘,web 只读该视图文件。
产物:data/analysis/chart/{code}.json = {dates, open, high, low, close, ma5, ma20, ma60,
volume, boll_up/boll_mid/boll_low(叠加主图), dif/dea/macd_hist(MACD子图),
rsi6/rsi12/rsi24(RSI子图), kdj_k/kdj_d/kdj_j(KDJ子图)}(最近 limit 根)。
含 OHLC 以支持蜡烛图(candlestick),折线图取 close 即可。新增指标序列只增不改,旧折线/
蜡烛视图向后兼容(前端对缺失新键跳过)。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.analysis import technical as ta
from tools.collectors import market
from tools.config import stock_pool

logger = logging.getLogger("analysis.chart")

_EMPTY = {"dates": [], "open": [], "high": [], "low": [], "close": [],
          "ma5": [], "ma20": [], "ma60": [], "volume": [],
          "boll_up": [], "boll_mid": [], "boll_low": [],
          "dif": [], "dea": [], "macd_hist": [],
          "rsi6": [], "rsi12": [], "rsi24": [],
          "kdj_k": [], "kdj_d": [], "kdj_j": []}


def build_chart(code: str, limit: int = 120) -> dict:
    """读 K线 + 预算 MA5/20/60,返回图表序列(最近 limit 根)。缺失返回空。

    含 OHLC(open/high/low/close)以支持蜡烛图;折线图取 close。向后兼容旧折线视图。
    """
    try:
        df = market.load_kline_recent(code).copy()
    except FileNotFoundError:
        return dict(_EMPTY)
    # 均线(叠加主图)
    for w in (5, 20, 60):
        df[f"ma{w}"] = ta.ma(df["close"], w)
    # 布林带(叠加主图):上/中/下轨,通达信口径(ddof=0)
    bl = ta.boll(df["close"])
    df["boll_up"], df["boll_mid"], df["boll_low"] = bl["upper"], bl["mid"], bl["lower"]
    # MACD 子图:dif/dea 两线 + macd_hist 柱(2×(dif-dea))
    md = ta.macd(df["close"])
    df["dif"], df["dea"], df["macd_hist"] = md["dif"], md["dea"], md["macd"]
    # RSI 子图:6/12/24
    for w in (6, 12, 24):
        df[f"rsi{w}"] = ta.rsi(df["close"], w)
    # KDJ 子图:k/d/j(9,3,3)
    kd = ta.kdj(df)
    df["kdj_k"], df["kdj_d"], df["kdj_j"] = kd["k"], kd["d"], kd["j"]

    # 指标在全量近史上算完(预热充分)再截到 limit
    df = df.tail(limit)

    def col(c, nd=2):
        return [None if pd.isna(v) else round(float(v), nd) for v in df[c]]

    return {
        "dates": [str(d)[:10] for d in df["date"]],
        "open": col("open"), "high": col("high"), "low": col("low"),
        "close": col("close"), "ma5": col("ma5"), "ma20": col("ma20"),
        "ma60": col("ma60"), "volume": [float(v) for v in df["volume"]],
        "boll_up": col("boll_up"), "boll_mid": col("boll_mid"), "boll_low": col("boll_low"),
        "dif": col("dif", 3), "dea": col("dea", 3), "macd_hist": col("macd_hist", 3),
        "rsi6": col("rsi6"), "rsi12": col("rsi12"), "rsi24": col("rsi24"),
        "kdj_k": col("kdj_k"), "kdj_d": col("kdj_d"), "kdj_j": col("kdj_j"),
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
