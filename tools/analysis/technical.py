"""技术指标分析(纯本地计算,不触网)。

输入 K线 DataFrame,输出均线/MACD/KDJ/RSI/量价信号。
"""
import pandas as pd


def compute(kline: pd.DataFrame) -> dict:
    """对单票 K线算全套技术指标。

    输入:kline DataFrame[date, open, high, low, close, volume]。
    输出:{
        ma:   {ma5, ma10, ma20, ma60, 多空排列},
        macd: {dif, dea, macd, 金叉/死叉},
        kdj:  {k, d, j, 超买/超卖},
        rsi:  {rsi6, rsi12, rsi24},
        vol:  {量比, 放量/缩量},
        signal: 综合技术评级(偏多/中性/偏空),
    }
    """
    raise NotImplementedError("P1 阶段实现")


def ma(close: pd.Series, window: int) -> pd.Series:
    raise NotImplementedError("P1 阶段实现")


def macd(close: pd.Series) -> pd.DataFrame:
    raise NotImplementedError("P1 阶段实现")


def kdj(kline: pd.DataFrame) -> pd.DataFrame:
    raise NotImplementedError("P1 阶段实现")


def rsi(close: pd.Series, window: int) -> pd.Series:
    raise NotImplementedError("P1 阶段实现")
