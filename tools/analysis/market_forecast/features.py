"""特征面板拼装 + 标的收盘序列 + 标签(大盘预测)。

标的两选:
  · 'hs300'  —— 真·沪深300 指数(index_kline/000300,历史约 2025-04 起,窗口短,做近端校验)
  · 'proxy'  —— **全A等权代理指数**:逐日 = 全市场个股涨幅均值(mean_pct)累乘,历史回溯 2018,
                用于长回测。⚠️局限:master 只含**当前在市**个股 → 有幸存者偏差(代理指数上偏),
                方向研究可用但绝对收益别当真。

面板 = 技术因子(标的指数)⋈ 广度因子(全市场)⋈ 消息面因子,按 date 左连接到标的交易日。
标签 = 标的 T+h 前瞻收益 fwd_ret_h + 5 档 bucket(**按分位**,标签允许用未来,不违反红线)。
防未来函数红线只约束**特征**:技术/广度/消息面均只用 ≤T 信息。
"""
from __future__ import annotations

import glob
import logging

import numpy as np
import pandas as pd

from tools.config.strategy import THRESHOLDS

from . import breadth as B
from . import fundflow as FF
from . import sentiment as S
from . import technical_index as TI

logger = logging.getLogger("market_forecast.features")
_CFG = THRESHOLDS["大盘预测"]


# ————————————————————————— 标的指数收盘序列 —————————————————————————
def load_hs300(data_root=None) -> pd.DataFrame:
    """合并全部快照的 index_kline/000300 → 最长的沪深300 OHLCV(date 升序,去重取最新)。"""
    from .dataroot import ensure_data_root
    root = ensure_data_root(str(data_root) if data_root else None)
    parts = []
    for p in sorted(glob.glob(str(root / "raw" / "*" / "index_kline" / "000300.parquet"))):
        try:
            parts.append(pd.read_parquet(p))
        except Exception:
            continue
    if not parts:
        raise FileNotFoundError("未找到 index_kline/000300.parquet(沪深300)")
    df = pd.concat(parts, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    return df


def build_proxy_index(breadth_df: pd.DataFrame) -> pd.DataFrame:
    """全A等权代理指数:close = 1000×∏(1+mean_pct/100)。OHLC 取 close(无盘中),volume=总家数。"""
    m = breadth_df["mean_pct"].astype(float).fillna(0.0) / 100.0
    close = 1000.0 * (1.0 + m).cumprod()
    out = pd.DataFrame({
        "date": pd.to_datetime(breadth_df.index),
        "open": close.values, "high": close.values,
        "low": close.values, "close": close.values,
        "volume": breadth_df["total"].astype(float).values,
        "pct_chg": breadth_df["mean_pct"].astype(float).values,
    })
    return out.reset_index(drop=True)


# ————————————————————————— 广度 → 模型特征 —————————————————————————
def breadth_features(breadth_df: pd.DataFrame) -> pd.DataFrame:
    """广度日序列 → 平稳化模型特征(index=date)。全部只用当日横截面(≤T)。"""
    b = breadth_df
    out = pd.DataFrame(index=pd.to_datetime(b.index))
    out.index.name = "date"
    out["br_net_adv"] = b["net_adv"].values
    out["br_limit_net"] = np.tanh(b["limit_net"].values * 20.0)
    out["br_limit_up"] = np.tanh(b["limit_up_ratio"].values * 40.0)
    out["br_nh_net20"] = np.tanh(b["nh_net_20"].values * 20.0)
    out["br_above_ma20"] = b["above_ma20_ratio"].values - 0.5
    out["br_below_ma20"] = b["below_ma20_ratio"].values - 0.5      # 破位广度(居中)
    out["br_median"] = np.tanh(b["median_pct"].values)
    return out


# ————————————————————————— 面板拼装 —————————————————————————
FEATURE_COLS = [
    # 技术
    "tech_ma_align", "tech_macd", "tech_rsi", "tech_vol", "tech_bias20",
    "tech_mom1", "tech_mom5", "tech_mom10", "tech_mom20", "tech_atr",
    # 广度
    "br_net_adv", "br_limit_net", "br_limit_up", "br_nh_net20",
    "br_above_ma20", "br_below_ma20", "br_median",
    # 消息面
    "se_net_z", "se_ratio", "se_intensity",
    # 资金流(v1:SSE 市场级两融;盘后披露→拼接时滞后≥1交易日,防未来函数)
    "ff_buy_ratio", "ff_bal_mom5", "ff_bal_mom20",
]
_TECH_COLS = [c for c in FEATURE_COLS if c.startswith("tech_")]
_BREADTH_COLS = [c for c in FEATURE_COLS if c.startswith("br_")]
_SENTI_COLS = [c for c in FEATURE_COLS if c.startswith("se_")]
_FUNDFLOW_COLS = [c for c in FEATURE_COLS if c.startswith("ff_")]


def _attach_fundflow_lagged(panel_index: pd.DatetimeIndex, ff: pd.DataFrame,
                            lag_days: int) -> pd.DataFrame:
    """把资金流特征(index=两融日 d)滞后拼到面板日 T:只取 d<T 的最近一行(≥1交易日滞后)。

    防未来函数核心:merge_asof(direction=backward, allow_exact_matches=False)保证 d<T;
    lag_days>1 时先在两融自身日序上 shift(lag_days-1)追加交易日滞后。返回 index=面板日。
    """
    cols = list(FF.FUNDFLOW_COLS)
    if ff is None or ff.empty:
        return pd.DataFrame(0.0, index=panel_index, columns=cols)
    ff = ff.sort_index()
    if lag_days > 1:
        ff = ff.shift(lag_days - 1)
    left = pd.DataFrame({"date": pd.DatetimeIndex(panel_index).astype("datetime64[ns]")})
    left = left.sort_values("date")
    right = ff.reset_index().rename(columns={ff.index.name or "index": "date"})
    right["date"] = pd.to_datetime(right["date"]).astype("datetime64[ns]")
    right = right.sort_values("date")
    merged = pd.merge_asof(left, right, on="date", direction="backward",
                           allow_exact_matches=False)
    merged = merged.set_index("date").reindex(panel_index)
    return merged[cols]


def _bucketize(fwd: pd.Series, cfg=None) -> pd.Series:
    """fwd 收益 → 5 档序号 0..4(按分位切;标签可用未来,不违反红线)。"""
    cfg = cfg or _CFG
    qs = cfg["分位边界"]
    edges = fwd.quantile(qs).tolist()
    def _b(x):
        if np.isnan(x):
            return np.nan
        for i, e in enumerate(edges):
            if x <= e:
                return i
        return len(edges)
    return fwd.apply(_b)


def build_panel(target: str = "proxy", horizon: int = 1, data_root=None,
                breadth_df: pd.DataFrame | None = None,
                cfg=None, include_fundflow: bool = True) -> pd.DataFrame:
    """拼装建模面板。返回 DataFrame(index=date):FEATURE_COLS + fwd_ret + direction + bucket。

    · target='proxy'|'hs300';horizon∈{1,5,...}
    · 消息面历史浅 → 缺失日以 0 填(降级中性),并记 se_avail 标志。
    · 资金流(v1):SSE 市场级两融,盘后披露→拼接时滞后≥1交易日(防未来函数);
      include_fundflow=False → 资金流列全 0(=v0.5 三维,A/B 回测的 A 组)。
    · 特征全 ≤T;label fwd_ret_h = close[T+h]/close[T]-1(未来,合法标签)。
    """
    cfg = cfg or _CFG
    from .dataroot import ensure_data_root
    ensure_data_root(str(data_root) if data_root else None)

    if breadth_df is None:
        breadth_df = B.compute_breadth(data_root=data_root, cfg=cfg)

    if target == "hs300":
        idx = load_hs300(data_root)
    elif target == "proxy":
        idx = build_proxy_index(breadth_df)
    else:
        raise ValueError(f"未知 target:{target!r}")

    tech = TI.tech_features(idx)                       # index=date
    brf = breadth_features(breadth_df)
    se = S.normalize_features(S.compute_sentiment(data_root))

    panel = tech.join(brf, how="left")                # 标的交易日为基准
    if not se.empty:
        panel = panel.join(se, how="left")
        panel["se_avail"] = panel[_SENTI_COLS].notna().any(axis=1).astype(int)
    else:
        for c in _SENTI_COLS:
            panel[c] = np.nan
        panel["se_avail"] = 0
    # 消息面缺失→0(降级中性);广度缺失极少(标的日历⊆广度日历)→0
    panel[_SENTI_COLS] = panel[_SENTI_COLS].fillna(0.0)

    # 资金流维(v1):滞后拼接(防未来函数);include_fundflow=False → 全 0(A/B 的 A 组)
    lag = int(cfg.get("资金流滞后交易日", 1))
    if include_fundflow:
        ff = FF.compute_features(data_root)
        ff_lagged = _attach_fundflow_lagged(panel.index, ff, lag)
        for c in _FUNDFLOW_COLS:
            panel[c] = ff_lagged[c].values
        panel["ff_avail"] = panel[_FUNDFLOW_COLS].notna().any(axis=1).astype(int)
    else:
        for c in _FUNDFLOW_COLS:
            panel[c] = np.nan
        panel["ff_avail"] = 0
    # 资金流缺失(未采到/滞后越界)→0:composite 按覆盖率自动降权(诚实降级)
    panel[_FUNDFLOW_COLS] = panel[_FUNDFLOW_COLS].fillna(0.0)

    # 标签:标的收盘前瞻收益
    close = idx.set_index("date")["close"].reindex(panel.index)
    fwd = close.shift(-horizon) / close - 1.0
    panel["fwd_ret"] = fwd
    panel["direction"] = np.sign(fwd)
    panel["bucket"] = _bucketize(fwd, cfg)
    panel.attrs["target"] = target
    panel.attrs["horizon"] = horizon
    return panel
