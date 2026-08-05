"""K线图表数据视图(展示层用)。

架构约束(§3/§9.3):展示层只读、不算、不 import 分析器。故把「K线 + 均线」
在分析层预计算成视图落盘,web 只读该视图文件。
产物:data/analysis/chart/{code}.json = {dates, close, ma5, ma20, ma60, volume}(最近 limit 根)。
"""
from __future__ import annotations

import json
import logging

import pandas as pd

from tools.analysis import technical as ta
from tools.collectors import market
from tools.config import settings, stock_pool

logger = logging.getLogger("analysis.chart")

_CHART_DIR = settings.PROJECT_ROOT / "data" / "analysis" / "chart"
_EMPTY = {"dates": [], "close": [], "ma5": [], "ma20": [], "ma60": [], "volume": []}


def build_chart(code: str, limit: int = 120) -> dict:
    """读 K线 + 预算 MA5/20/60,返回 Chart.js 所需序列(最近 limit 根)。缓存缺失返回空。"""
    try:
        df = market.load_kline(code).copy()
    except FileNotFoundError:
        return dict(_EMPTY)
    for w in (5, 20, 60):
        df[f"ma{w}"] = ta.ma(df["close"], w)
    df = df.tail(limit)

    def col(c):
        return [None if pd.isna(v) else round(float(v), 2) for v in df[c]]

    return {
        "dates": [str(d)[:10] for d in df["date"]],
        "close": col("close"), "ma5": col("ma5"), "ma20": col("ma20"),
        "ma60": col("ma60"), "volume": [float(v) for v in df["volume"]],
    }


def write_charts(limit: int = 120) -> int:
    """全池生成图表视图落盘。返回成功数。"""
    _CHART_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for code in stock_pool.get_codes():
        data = build_chart(code, limit)
        if data["dates"]:
            (_CHART_DIR / f"{code}.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
            n += 1
    logger.info("K线图表视图落盘 %d 只 → %s", n, _CHART_DIR)
    return n
