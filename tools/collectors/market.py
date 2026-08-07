"""行情采集:日 K线、量价。

多源 fallback:腾讯 → 新浪 → 东财。
  - 主源腾讯 `stock_zh_a_hist_tx`:本机实测可用,返回 OHLCV+成交额+换手率。
  - 东财 `stock_zh_a_hist`:本机被其 TLS 指纹反爬(python-requests 被 RST),留作其他环境备选。
    详见 docs/问题/问题台账.md R4。
落盘:走 store 层(kind="kline",parquet),旁记 meta.source=实际命中源。
契约见 docs/计划/P1_技术面打通.md Step 1。
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.market")

# 统一输出列
_STD_COLS = ["date", "open", "high", "low", "close", "volume", "amount", "turnover", "pct_chg"]
# 东财中文列 → 标准列
_EM_COL_MAP = {
    "日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low",
    "成交量": "volume", "成交额": "amount", "涨跌幅": "pct_chg", "换手率": "turnover",
}
# 源优先级(本机腾讯可用;东财被指纹墙,置末)
DEFAULT_SOURCES = ("tencent", "sina", "eastmoney")


def market_prefix(code: str) -> str:
    """6 位代码 → 带交易所前缀(sh/sz/bj)。腾讯/新浪接口需带前缀。"""
    if code[0] in ("6", "9"):
        return f"sh{code}"
    if code[0] in ("0", "2", "3"):
        return f"sz{code}"
    if code[0] in ("8", "4"):
        return f"bj{code}"
    return code


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """统一列名/类型/排序,补算 pct_chg。"""
    df = df.rename(columns=_EM_COL_MAP)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    for c in ("open", "high", "low", "close", "volume", "amount", "turnover"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "pct_chg" not in df.columns or df["pct_chg"].isna().all():
        df["pct_chg"] = df["close"].pct_change() * 100  # 首行 NaN,正常
    for c in _STD_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    return df[_STD_COLS]


def _fetch_tencent(code, start, end, adjust) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_zh_a_hist_tx(symbol=market_prefix(code),
                                 start_date=start, end_date=end, adjust=adjust)


def _fetch_sina(code, start, end, adjust) -> pd.DataFrame:
    import akshare as ak
    df = ak.stock_zh_a_daily(symbol=market_prefix(code),
                             start_date=start, end_date=end, adjust=adjust)
    return df


def _fetch_eastmoney(code, start, end, adjust) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_zh_a_hist(symbol=code, period="daily",
                              start_date=start, end_date=end, adjust=adjust)


_FETCHERS = {"tencent": _fetch_tencent, "sina": _fetch_sina, "eastmoney": _fetch_eastmoney}


def _fetch_one_with_source(code: str, start: str, end: str, adjust: str,
                           sources: tuple[str, ...] = DEFAULT_SOURCES
                           ) -> tuple[pd.DataFrame, str]:
    """拉单票日 K线,返回 (归一化 df, 命中源名)。全失败抛 ConnectionError。

    落盘时要把实际命中的源写进 raw meta.source,故此处把命中源一并透出。
    """
    errors = []
    for src in sources:
        try:
            df = _FETCHERS[src](code, start, end, adjust)
            if df is None or len(df) == 0:
                raise ValueError("空数据")
            out = _normalize(df)
            logger.debug("K线 %s 命中源 %s", code, src)
            return out, src
        except Exception as e:  # 换下一个源
            errors.append(f"{src}: {type(e).__name__} {str(e)[:40]}")
    raise ConnectionError(f"{code} 所有源均失败: {errors}")


def fetch_one(code: str, start: str, end: str, adjust: str,
              sources: tuple[str, ...] = DEFAULT_SOURCES) -> pd.DataFrame:
    """拉单票日 K线(多源 fallback,不落盘)。

    依次尝试 sources 各源;全失败抛 ConnectionError(不返回空 df 伪装成功)。
    """
    df, _src = _fetch_one_with_source(code, start, end, adjust, sources)
    return df


def fetch_kline(codes: list[str], start: str | None = None,
                end: str | None = None, adjust: str = settings.KLINE_ADJUST
                ) -> dict[str, pd.DataFrame]:
    """拉取多票 K线并落盘 parquet。

    start/end 为 None 时:end=今天,start≈今天往前 KLINE_DAYS×2 自然日(覆盖非交易日)。
    单票失败记 logger 并跳过,不中断整批;返回成功票的 {code: DataFrame}。
    """
    settings.ensure_dirs()
    if start is None:
        start = (pd.Timestamp.today() - pd.Timedelta(days=settings.KLINE_DAYS * 2)
                 ).strftime("%Y%m%d")
    if end is None:
        end = pd.Timestamp.today().strftime("%Y%m%d")

    out: dict[str, pd.DataFrame] = {}
    failed: list[str] = []
    n = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] K线 %s 采集...", i, n, code)
        try:
            df, src = _fetch_one_with_source(code, start, end, adjust)
            store.put_raw("kline", code, df, meta={"source": src})
            out[code] = df
            logger.info("K线 %s 落盘 %d 根(源 %s)", code, len(df), src)
        except Exception as e:
            failed.append(code)
            logger.error("K线 %s 失败: %s", code, e)
        time.sleep(settings.FETCH_SLEEP_SEC)
    if failed:
        logger.warning("拉取失败票(%d): %s", len(failed), failed)
    return out


def load_kline(code: str) -> pd.DataFrame:
    """从本地缓存读单票 K线(分析层用,不触网)。缓存缺失抛 FileNotFoundError。"""
    return store.get_raw("kline", code)
