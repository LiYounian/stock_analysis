"""指数技术因子(复用 tools.analysis.technical 的算子)——大盘预测的"技术"维。

对指数 K线(沪深300 / 全A等权代理)向量化算**每个交易日的 as-of 技术特征**:
MA 排列分、MACD 柱、RSI、量能环比、动量(1/5/10/20 日)、ATR、乖离(bias20)。

关键(无未来函数):rolling / ewm 都是**因果**算子——第 T 行的指标只用 ≤T 的收盘。
故一次性全序列向量化即可,取第 T 行作为 T 日特征,天然不含未来信息。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.analysis import technical as T


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """ATR(真实波幅均值),归一为占收盘的比例。因果 rolling。"""
    h, l, c = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return (tr.rolling(n, min_periods=n).mean() / c)


def tech_features(index_df: pd.DataFrame) -> pd.DataFrame:
    """指数 K线 → 逐日技术特征 DataFrame(index=date 升序)。列均为模型可用数值。

    复用 technical.ma / technical.macd / technical.rsi(通达信口径)。方向类特征做有符号编码。
    """
    d = index_df.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").reset_index(drop=True)
    close = d["close"].astype(float)

    ma5, ma10, ma20, ma60 = (T.ma(close, w) for w in (5, 10, 20, 60))
    # MA 排列分:多头(5>10>20)=+1;空头=-1;纠缠=0
    align = pd.Series(0.0, index=close.index)
    align[(ma5 > ma10) & (ma10 > ma20)] = 1.0
    align[(ma5 < ma10) & (ma10 < ma20)] = -1.0

    md = T.macd(close)
    macd_hist = md["macd"]
    # 归一:MACD 柱 / 收盘(尺度无关),再 tanh 挤压
    macd_norm = np.tanh(macd_hist / close * 100.0)

    rsi12 = T.rsi(close, 12)
    rsi_z = (rsi12 - 50.0) / 50.0

    vol = d["volume"].astype(float)
    vol_ma5_prev = vol.shift(1).rolling(5, min_periods=5).mean()
    vol_ratio = (vol / vol_ma5_prev).replace([np.inf, -np.inf], np.nan)
    vol_z = np.tanh((vol_ratio - 1.0))

    bias20 = ((close - ma20) / ma20)

    out = pd.DataFrame({
        "tech_ma_align": align,
        "tech_macd": macd_norm,
        "tech_rsi": rsi_z,
        "tech_vol": vol_z,
        "tech_bias20": np.tanh(bias20 * 5.0),
        "tech_mom1": close.pct_change(1),
        "tech_mom5": close.pct_change(5),
        "tech_mom10": close.pct_change(10),
        "tech_mom20": close.pct_change(20),
        "tech_atr": _atr(d).values,
    })
    out.index = d["date"]
    out.index.name = "date"
    return out
