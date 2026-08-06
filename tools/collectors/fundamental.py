"""基本面采集:财报关键指标 + 估值。

数据源(本机实测可用,避开被指纹墙的东财):
  - 同花顺 `stock_financial_abstract`:营收/净利/增速/ROE/毛利率/净利率/负债率。
  - 百度 `stock_zh_valuation_baidu`:PE(TTM)/PB/总市值。
落盘:走 store 层(kind="fundamental",json),旁记 meta.source。
契约见 docs/计划/P2_结构化情绪与基本面.md。
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.fundamental")

# 数据来源标注(同花顺财务摘要 + 百度估值)
_SOURCE = "同花顺+百度"

# 输出字段 → 同花顺财务摘要指标名
_ABSTRACT_MAP = {
    "营收": "营业总收入", "净利": "归母净利润",
    "营收增速": "营业总收入增长率", "净利增速": "归属母公司净利润增长率",
    "ROE": "净资产收益率(ROE)", "毛利率": "毛利率",
    "净利率": "销售净利率", "负债率": "资产负债率",
}
# 输出字段 → 百度估值 indicator
_BAIDU_MAP = {"PE_TTM": "市盈率(TTM)", "PB": "市净率", "总市值": "总市值"}


def _to_float(v):
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _fetch_abstract(code: str) -> dict:
    """同花顺财务摘要,取最新报告期关键指标。"""
    import akshare as ak

    df = ak.stock_financial_abstract(symbol=code)
    if df is None or df.empty or "指标" not in df.columns:
        raise ValueError("财务摘要空/结构异常")
    period = df.columns[2]                       # 第 3 列为最新报告期
    out = {"报告期": str(period)}
    for key, ind in _ABSTRACT_MAP.items():
        row = df[df["指标"] == ind]
        out[key] = _to_float(row.iloc[0][period]) if len(row) else None
    return out


def _fetch_baidu(code: str) -> dict:
    """百度估值,取各 indicator 时间序列最新值。单项失败该字段 None。"""
    import akshare as ak

    out = {}
    for key, ind in _BAIDU_MAP.items():
        try:
            df = ak.stock_zh_valuation_baidu(symbol=code, indicator=ind, period="近一年")
            out[key] = _to_float(df.iloc[-1]["value"]) if len(df) else None
        except Exception as e:  # 单项估值失败不影响其他字段
            logger.debug("%s 百度 %s 失败: %s", code, ind, e)
            out[key] = None
    return out


def fetch_fundamental(codes: list[str]) -> dict[str, dict]:
    """拉取多票基本面并落盘。

    合并同花顺财务摘要 + 百度估值。单票整体失败记 logger 并跳过,不中断整批。
    """
    settings.ensure_dirs()
    out: dict[str, dict] = {}
    failed: list[str] = []
    for code in codes:
        try:
            rec = _fetch_abstract(code)
            rec.update(_fetch_baidu(code))
            store.put_raw("fundamental", code, rec, meta={"source": _SOURCE})
            out[code] = rec
            logger.info("基本面 %s 落盘(报告期 %s)", code, rec["报告期"])
        except Exception as e:
            failed.append(code)
            logger.error("基本面 %s 失败: %s", code, e)
        time.sleep(settings.FETCH_SLEEP_SEC)
    if failed:
        logger.warning("基本面拉取失败(%d): %s", len(failed), failed)
    return out


def load_fundamental(code: str) -> dict:
    """从本地缓存读单票基本面。缓存缺失抛 FileNotFoundError。"""
    return store.get_raw("fundamental", code)
