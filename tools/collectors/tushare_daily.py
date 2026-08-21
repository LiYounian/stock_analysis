"""Tushare Pro 全 A 盘后日线批量源(可选增强,免费源之上)。

`daily(trade_date=...)` 一次返回全市场当日 OHLCV，配 `daily_basic` 的换手率，
适合作全 A 主档的**日增量优先源**；取不到时由上层静默回退免费源(baostock / 腾讯-新浪)。
另提供 `fetch_chip` 取筹码获利比例(`cyq_perf`)，供「最强」策略——**免费源拿不到**。

设计约束(见 docs/计划/PR15_Tushare数据源与四策略集成.md §J5):
  - Token 仅从环境变量 ``TUSHARE_TOKEN`` 读取，不入库、不打印。
  - 本层只负责取数并归一列名/单位；**所有失败都抛异常**，由上层 try/except 回退，不在此吞掉。
  - 不另起独立复权 master(避免与项目 qfq 主档口径不一致)；`daily` 增量视同 spot 未复权当日 bar
    追加进现有 qfq 主档，语义与现有 akshare-spot 增量一致。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.config import settings

logger = logging.getLogger("collectors.tushare_daily")


def is_configured() -> bool:
    """是否配置了 Tushare token(未配则上层回退免费源)。"""
    return bool(settings.TUSHARE_TOKEN)


def _pro():
    """惰性初始化 Tushare pro_api(token 仅 env 读)。未配/未装抛 RuntimeError。"""
    if not is_configured():
        raise RuntimeError("TUSHARE_TOKEN 未配置")
    try:
        import tushare as ts
    except ImportError as e:  # 未装 tushare 时上层回退,不崩
        raise RuntimeError("未安装 tushare；启用 Tushare 源需 pip install tushare(见 requirements.txt)") from e
    return ts.pro_api(settings.TUSHARE_TOKEN)


def _norm_code(s: pd.Series) -> pd.Series:
    """ts_code(如 600000.SH)→ 6 位裸代码(与项目内部 code 口径一致)。"""
    return s.astype(str).str.split(".").str[0].str.zfill(6)


def fetch_daily_all(trade_date: str) -> pd.DataFrame:
    """取某交易日全 A 日线 + 换手率，返回 ``market.update_master_from_spot`` 可消费的标准列。

    列:code, open, high, low, close, volume(股), amount(元), turnover(换手率%), pct_chg。
    单位换算:Tushare vol=手 → ×100 股;amount=千元 → ×1000 元;换手率取自 daily_basic.turnover_rate
    (与 baostock/akshare-spot 的 turnover 口径一致=百分比)。换手取不到不致命 → 置 NA
    (主档通常已有免费源换手;此处仅尽量补齐,避免以 Tushare 为增量源那天丢换手列)。
    空数据(非交易日/未收盘)抛 ConnectionError,由上层回退免费源。
    """
    pro = _pro()
    day = str(trade_date).replace("-", "")
    df = pro.daily(trade_date=day,
                   fields="ts_code,trade_date,open,high,low,close,vol,amount,pct_chg")
    if df is None or df.empty:
        raise ConnectionError(f"Tushare daily({day}) 返回空(非交易日或未收盘)")
    out = pd.DataFrame({
        "code": _norm_code(df["ts_code"]),
        "open": pd.to_numeric(df["open"], errors="coerce"),
        "high": pd.to_numeric(df["high"], errors="coerce"),
        "low": pd.to_numeric(df["low"], errors="coerce"),
        "close": pd.to_numeric(df["close"], errors="coerce"),
        "volume": pd.to_numeric(df["vol"], errors="coerce") * 100,
        "amount": pd.to_numeric(df["amount"], errors="coerce") * 1000,
        "pct_chg": pd.to_numeric(df["pct_chg"], errors="coerce"),
        "turnover": pd.NA,
    })
    try:
        basic = pro.daily_basic(trade_date=day, fields="ts_code,trade_date,turnover_rate")
        if basic is not None and not basic.empty:
            tmap = dict(zip(_norm_code(basic["ts_code"]),
                            pd.to_numeric(basic["turnover_rate"], errors="coerce")))
            out["turnover"] = out["code"].map(tmap)
    except Exception as e:  # 换手补齐失败不致命
        logger.warning("Tushare daily_basic(%s) 换手率取失败(不致命,置 NA):%s", day, e)
    out = out.dropna(subset=["code", "open", "high", "low", "close"])
    if out.empty:
        raise ConnectionError(f"Tushare daily({day}) 无有效 OHLC 行")
    return out


def fetch_chip(trade_date: str) -> pd.DataFrame:
    """取某交易日全 A 筹码获利比例(cyq_perf),供「最强」策略。**免费源拿不到**。

    列:code, winner_rate(获利比例%,= TDX ``WINNER(CLOSE)``),
        cost_95pct(95 分位成本价,用于近似 ``WINNER(HIGH)``:判 HIGH ≥ cost_95pct)。
    空数据抛 ConnectionError,由上层决定跳过该策略(不用免费源硬凑)。
    """
    pro = _pro()
    day = str(trade_date).replace("-", "")
    df = pro.cyq_perf(trade_date=day, fields="ts_code,trade_date,winner_rate,cost_95pct")
    if df is None or df.empty:
        raise ConnectionError(f"Tushare cyq_perf({day}) 返回空")
    return pd.DataFrame({
        "code": _norm_code(df["ts_code"]),
        "winner_rate": pd.to_numeric(df["winner_rate"], errors="coerce"),
        "cost_95pct": pd.to_numeric(df["cost_95pct"], errors="coerce"),
    }).dropna(subset=["code"])


def trade_dates(start: str, end: str) -> list[str]:
    """交易日历(is_open=1)→ ['YYYY-MM-DD', ...]。供历史回补 / 回测取交易日序列。"""
    df = _pro().trade_cal(exchange="", start_date=str(start).replace("-", ""),
                          end_date=str(end).replace("-", ""), is_open="1",
                          fields="cal_date,is_open")
    return [pd.Timestamp(x).strftime("%Y-%m-%d") for x in df["cal_date"].tolist()]
