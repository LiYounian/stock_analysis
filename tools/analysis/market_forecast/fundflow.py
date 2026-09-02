"""资金流因子(读市场级两融缓存)——大盘预测 v1 的「资金流」维。

数据源:tools.collectors.market_fundflow 落的 SSE 市场级两融日序列
(raw/market_fundflow/sse_margin.parquet;融资余额=杠杆存量、融资买入额=杠杆增量)。

因子(全部只用两融**自身历史 ≤ 该两融日**的滚动量,自含无未来):
  · ff_buy_ratio  —— 融资买入额/融资余额 相对其 20 日均值的偏离(杠杆买入强度,tanh)
  · ff_bal_mom5   —— 融资余额 5 日变化率(杠杆资金短期流入动量,tanh)
  · ff_bal_mom20  —— 融资余额 20 日变化率(杠杆资金中期趋势,tanh)

===== 防未来函数(红线) =====
两融**盘后披露**:交易日 d 的两融在 d 收盘后才公开 → 对 as_of=T 的预测,只能用 date ≤ T−1
的两融(严格早于 T)。本模块产出按**两融日 d** 索引的特征;滞后由 features.py 的
merge_asof(allow_exact_matches=False)在拼面板时强制:面板日 T 只取 d<T 的最近一行,
即至少滞后 1 个交易日(见 config「资金流滞后交易日」)。

历史未采到两融的日子(如代理指数 2018-2021 段)该维缺省 0 → composite 按覆盖率自动降权
(同消息面机制),诚实降级、可追溯。⚠️ 非投资建议。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("market_forecast.fundflow")

# 模型特征列(资金流维)
FUNDFLOW_COLS = ["ff_buy_ratio", "ff_bal_mom5", "ff_bal_mom20"]


def load_market_margin(data_root=None) -> pd.DataFrame:
    """读市场级两融缓存 → DataFrame(index=date 升序,列 rz_bal/rz_buy/rzrq_bal)。

    缓存缺失/空 → 空 DataFrame(下游按缺省 0 降级)。
    """
    from tools.collectors.market_fundflow import cache_path
    from .dataroot import ensure_data_root
    root = ensure_data_root(str(data_root) if data_root else None)
    path = cache_path(root)
    if not path.exists():
        logger.warning("未找到市场级两融缓存 %s(资金流维将全缺省)。"
                       "先跑:python -m tools.collectors.market_fundflow", path)
        return pd.DataFrame()
    try:
        df = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读市场级两融缓存失败(资金流维缺省): %s", str(exc)[:120])
        return pd.DataFrame()
    if df.empty or "date" not in df.columns:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates("date").set_index("date").sort_index()
    return df


def compute_features(data_root=None, margin_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """市场级两融 → 资金流模型特征(index=两融日 d 升序,列 FUNDFLOW_COLS)。

    全部为 ≤d 的滚动量(自含无未来);滞后到预测日由 features.py 拼接时强制(见模块 docstring)。
    """
    if margin_df is None:
        margin_df = load_market_margin(data_root)
    if margin_df is None or margin_df.empty:
        return pd.DataFrame(columns=FUNDFLOW_COLS)
    m = margin_df.sort_index()
    bal = m["rz_bal"].astype(float)
    buy = m["rz_buy"].astype(float)

    buy_ratio = buy / bal.replace(0.0, np.nan)          # 杠杆买入强度(日买入/存量)
    buy_ratio_dev = buy_ratio - buy_ratio.rolling(20, min_periods=5).mean()
    bal_mom5 = bal.pct_change(5)
    bal_mom20 = bal.pct_change(20)

    out = pd.DataFrame(index=m.index)
    out.index.name = "date"
    out["ff_buy_ratio"] = np.tanh(buy_ratio_dev * 200.0)   # 偏离量级~1e-3 → ×200 入 tanh 敏感区
    out["ff_bal_mom5"] = np.tanh(bal_mom5 * 25.0)          # 5日余额变动~±2% → ×25
    out["ff_bal_mom20"] = np.tanh(bal_mom20 * 8.0)         # 20日余额变动~±6% → ×8
    return out
