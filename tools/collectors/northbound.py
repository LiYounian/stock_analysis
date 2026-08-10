"""北向资金采集:个股北向净流入趋势(多因子的"资金流"因子 D8)。

用途:多因子 score 的资金流维度 = 个股近 N 日(5–10)北向净流入趋势(净增持资金求和)。

现状(本机 2026-08 实测,诚实记录):
  - akshare `stock_hsgt_individual_em(symbol=code)` 接口**本机可通、未被墙**,能返回个股北向持股历史
    (含「今日增持资金」列)。
  - 但**数据源本身已停更**:港交所自 2024-08 起停止披露沪深港通**个股每日**持股明细,
    该接口最新一根 bar 停在 2024-08-16。到 as_of(如 2026-08)已陈旧约两年。
  - 结论:并非"被墙",而是**监管层面停止披露** → 无法得到"当前"北向趋势。
    若强行用两年前的数据当趋势 = 伪造信号,故按 I4 **降级为缺失**(trend 返 None)。
    实现上仍真查接口 + **新鲜度护栏**:仅当最新 bar 距 as_of 在 `_FRESH_DAYS` 天内才采用,
    否则视为不可得 → None。**一旦披露恢复,本模块自动重新生效,上层零改动。**

依赖方向:采集层。失败/陈旧静默降级(返回 None),由多因子 score 按缺失处理。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("collectors.northbound")

# 新鲜度护栏:个股北向最新 bar 距 as_of 超过此天数 → 视为停更/不可得(避免用陈旧数据伪造趋势)
_FRESH_DAYS = 15


def _fetch_individual(code: str, win: int, as_of: str | None = None):
    """取单票近 win 日北向净增持资金趋势(求和)。

    - 真查 akshare `stock_hsgt_individual_em`;
    - 过**新鲜度护栏**:最新 bar 距 as_of 超 `_FRESH_DAYS` 天 → 抛让上层降级(源已停更);
    - 数据/接口异常 → 抛让上层降级(含被墙 RemoteDisconnected)。
    """
    import akshare as ak
    import pandas as pd

    df = ak.stock_hsgt_individual_em(symbol=code)
    if df is None or len(df) == 0 or "持股日期" not in df.columns or "今日增持资金" not in df.columns:
        raise ValueError("北向个股明细空/结构异常")
    df = df.sort_values("持股日期")
    last_date = pd.to_datetime(df["持股日期"].iloc[-1])
    asd = pd.to_datetime(as_of) if as_of else pd.Timestamp.today()
    if (asd - last_date).days > _FRESH_DAYS:
        raise ValueError(
            f"北向个股明细已停更(最新 {last_date.date()},距 as_of {asd.date()} 超 {_FRESH_DAYS}d)")
    seg = df["今日增持资金"].tail(int(win))
    total = float(pd.to_numeric(seg, errors="coerce").fillna(0.0).sum())
    return total


def trend(code: str, win: int = 10, as_of: str | None = None) -> float | None:
    """个股近 win 日北向净流入趋势;取不到/已停更→None(I4 降级缺失,不抛)。"""
    try:
        return _fetch_individual(code, win, as_of)
    except Exception as e:                       # 含停更 / 网络墙 / 结构异常
        logger.debug("北向 %s 趋势不可得(降级缺失): %s", code, type(e).__name__)
        return None


def trend_map(codes: list[str], win: int = 10, as_of: str | None = None) -> dict[str, float]:
    """批量北向趋势 {code: 趋势};全不可得则返回空 dict(多因子资金流维度整体缺失)。"""
    out = {}
    for c in codes:
        t = trend(c, win, as_of)
        if t is not None:
            out[c] = t
    if not out:
        logger.info("北向个股趋势整体不可得(源自 2024-08 停更),多因子资金流维度降级缺失(I4)")
    return out
