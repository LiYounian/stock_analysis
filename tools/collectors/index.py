"""指数采集:宽基/主要指数日 K线。

用途:形态选股策略的 **RS 基准**(板块指数 vs 沪深300;沪深300 是市场基准),
以及模块一市场状态的"指数多头"因子。属采集层(与 market/universe 同级),
策略层只经 store 读、不直接触网。

多源 fallback(与 market.py 同思路):
  - 新浪 `stock_zh_index_daily`(symbol 需 sh/sz 前缀):OHLCV。
  - 东财 `index_zh_a_hist`(纯 6 位代码):中文列,含成交额/涨跌幅。
落盘:store kind="index_kline",code=指数 6 位代码,parquet。
列归一化复用 market._normalize(统一为 _STD_COLS)。
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from tools.collectors import market
from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.index")

# 常用指数别名 → 6 位代码(RS 与市场状态用)
INDEX_ALIASES = {
    "沪深300": "000300", "上证指数": "000001", "深证成指": "399001",
    "创业板指": "399006", "科创50": "000688", "中证500": "000905",
    "中证1000": "000852", "北证50": "899050",
}
BENCHMARK = "000300"          # RS 顶层基准:沪深300


def index_prefix(code: str) -> str:
    """指数 6 位代码 → 带交易所前缀(新浪接口需要)。

    上证/中证系(000/9 开头)→ sh;深证系(399 开头)→ sz;北证(899)→ bj。
    """
    if code.startswith("399"):
        return f"sz{code}"
    if code.startswith("899"):
        return f"bj{code}"
    return f"sh{code}"        # 000xxx / 9xxxxx 归上证/中证


def _fetch_sina(code: str, start: str, end: str) -> pd.DataFrame:
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol=index_prefix(code))   # 全历史,后面按 start/end 截
    if df is not None and len(df) and "date" in df.columns:
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        s, e = pd.to_datetime(start), pd.to_datetime(end)
        df = df[(df["date"] >= s) & (df["date"] <= e)]
    return df


def _fetch_eastmoney(code: str, start: str, end: str) -> pd.DataFrame:
    import akshare as ak
    return ak.index_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end)


_FETCHERS = {"sina": _fetch_sina, "eastmoney": _fetch_eastmoney}
DEFAULT_SOURCES = ("sina", "eastmoney")


def _fetch_one_with_source(code: str, start: str, end: str,
                           sources: tuple[str, ...] = DEFAULT_SOURCES
                           ) -> tuple[pd.DataFrame, str]:
    """拉单指数日 K线,返回 (归一化 df, 命中源)。全失败抛 ConnectionError。"""
    errors = []
    for src in sources:
        try:
            df = _FETCHERS[src](code, start, end)
            if df is None or len(df) == 0:
                raise ValueError("空数据")
            return market._normalize(df), src
        except Exception as e:
            errors.append(f"{src}: {type(e).__name__} {str(e)[:40]}")
    raise ConnectionError(f"指数 {code} 所有源均失败: {errors}")


def fetch_index(codes: list[str], start: str | None = None,
                end: str | None = None) -> dict[str, pd.DataFrame]:
    """拉多个指数日 K线并落盘。别名(如 '沪深300')自动转 6 位代码。

    start/end 缺省同 market:end=今天,start≈今天往前 KLINE_DAYS×2 自然日。
    单指数失败记 log 跳过,不中断整批;返回成功的 {code: df}。
    """
    settings.ensure_dirs()
    if start is None:
        start = (pd.Timestamp.today() - pd.Timedelta(days=settings.KLINE_DAYS * 2)
                 ).strftime("%Y%m%d")
    if end is None:
        end = pd.Timestamp.today().strftime("%Y%m%d")

    out: dict[str, pd.DataFrame] = {}
    for raw in codes:
        code = INDEX_ALIASES.get(raw, raw)
        try:
            df, src = _fetch_one_with_source(code, start, end)
            store.put_raw("index_kline", code, df, meta={"source": src, "alias": raw})
            out[code] = df
            logger.info("指数 %s 落盘 %d 根(源 %s)", code, len(df), src)
        except Exception as e:
            logger.error("指数 %s 失败: %s", code, e)
        time.sleep(settings.FETCH_SLEEP_SEC)
    return out


def load_index(code: str) -> pd.DataFrame:
    """从本地读单指数日 K线(策略层用,不触网)。别名自动转码。缺失抛 FileNotFoundError。"""
    return store.get_raw("index_kline", INDEX_ALIASES.get(code, code))
