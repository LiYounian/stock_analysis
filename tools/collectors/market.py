"""行情采集:日/周 K线、量价。

主数据源:akshare `stock_zh_a_hist`(前复权)。
备选:东财隐藏接口 push2his.eastmoney.com/api/qt/stock/kline/get。
落盘:data/raw/kline/{code}.parquet
"""
import pandas as pd


def fetch_kline(codes: list[str], start: str | None = None,
                end: str | None = None, adjust: str = "qfq") -> dict[str, pd.DataFrame]:
    """拉取 K线并落盘。

    输入:codes 代码列表;start/end 日期(YYYYMMDD,None=用 settings 默认);adjust 复权方式。
    输出:{code: DataFrame[date, open, high, low, close, volume, amount, ...]},同时落盘 parquet。
    """
    raise NotImplementedError("P1 阶段实现")


def load_kline(code: str) -> pd.DataFrame:
    """从本地缓存读单票 K线(分析层用,不触网)。"""
    raise NotImplementedError("P1 阶段实现")
