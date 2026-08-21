"""Tushare Pro 全 A 盘后日线批量源。

`daily(trade_date=...)` 一次返回全市场当日 OHLCV，适合作为全 A 主档的
日增量与历史首灌均使用 ``daily + adj_factor``。价格乘复权因子后，和前复权
价格仅相差每只股票固定比例，不改变本项目的均线、涨幅及高低点类策略判断。
Token 仅从环境变量 ``TUSHARE_TOKEN`` 读取，不入库、不打印。
"""
from __future__ import annotations

import pandas as pd

from tools.config import settings


def is_configured() -> bool:
    return bool(settings.TUSHARE_TOKEN)


def fetch_daily_all(trade_date: str) -> pd.DataFrame:
    """取某交易日全 A 日线，返回 market.update_master_from_spot 可消费的标准列。"""
    if not is_configured():
        raise RuntimeError("TUSHARE_TOKEN 未配置")
    try:
        import tushare as ts
    except ImportError as e:
        raise RuntimeError("未安装 tushare；请 pip install -r requirements.txt") from e

    day = str(trade_date).replace("-", "")
    pro = ts.pro_api(settings.TUSHARE_TOKEN)
    df = pro.daily(trade_date=day,
                   fields="ts_code,trade_date,open,high,low,close,vol,amount,pct_chg")
    if df is None or df.empty:
        raise ConnectionError(f"Tushare daily({day}) 返回空数据（可能非交易日或未收盘）")
    out = pd.DataFrame({
        "code": df["ts_code"].astype(str).str.split(".").str[0].str.zfill(6),
        "open": pd.to_numeric(df["open"], errors="coerce"),
        "high": pd.to_numeric(df["high"], errors="coerce"),
        "low": pd.to_numeric(df["low"], errors="coerce"),
        "close": pd.to_numeric(df["close"], errors="coerce"),
        # Tushare vol=手、amount=千元；项目统一为股、元。
        "volume": pd.to_numeric(df["vol"], errors="coerce") * 100,
        "amount": pd.to_numeric(df["amount"], errors="coerce") * 1000,
        "pct_chg": pd.to_numeric(df["pct_chg"], errors="coerce"),
        "turnover": pd.NA,
    })
    out = out.dropna(subset=["code", "open", "high", "low", "close"])
    if out.empty:
        raise ConnectionError(f"Tushare daily({day}) 无有效 OHLC 行")
    return out


def _pro():
    if not is_configured():
        raise RuntimeError("TUSHARE_TOKEN 未配置")
    import tushare as ts
    return ts.pro_api(settings.TUSHARE_TOKEN)


def trade_dates(start: str, end: str) -> list[str]:
    df = _pro().trade_cal(exchange="", start_date=start.replace("-", ""),
                           end_date=end.replace("-", ""), is_open="1", fields="cal_date,is_open")
    return [pd.Timestamp(x).strftime("%Y-%m-%d") for x in df["cal_date"].tolist()]


def fetch_daily_adjusted(trade_date: str) -> pd.DataFrame:
    """全 A 某日复权 OHLCV；每交易日仅 daily + adj_factor 两次批量请求。"""
    day = str(trade_date).replace("-", "")
    pro = _pro()
    daily = pro.daily(trade_date=day, fields="ts_code,trade_date,open,high,low,close,vol,amount,pct_chg")
    factor = pro.adj_factor(trade_date=day, fields="ts_code,trade_date,adj_factor")
    if daily is None or daily.empty or factor is None or factor.empty:
        raise ConnectionError(f"Tushare {day} 日线或复权因子为空")
    df = daily.merge(factor, on=["ts_code", "trade_date"], how="inner")
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce") * pd.to_numeric(df["adj_factor"], errors="coerce")
    return pd.DataFrame({"code": df.ts_code.astype(str).str.split(".").str[0].str.zfill(6),
        "date": pd.to_datetime(df.trade_date), "open": df.open, "high": df.high, "low": df.low,
        "close": df.close, "volume": pd.to_numeric(df.vol, errors="coerce")*100,
        "amount": pd.to_numeric(df.amount, errors="coerce")*1000, "turnover": pd.NA,
        "pct_chg": pd.to_numeric(df.pct_chg, errors="coerce")}).dropna(subset=["code", "open", "high", "low", "close"])


def fetch_daily_basic_all(trade_date: str) -> pd.DataFrame:
    """取全市场当日换手率。TDX 的 HSL 与 turnover_rate 都是百分比口径。"""
    day = str(trade_date).replace("-", "")
    df = _pro().daily_basic(
        trade_date=day,
        fields="ts_code,trade_date,turnover_rate,float_share",
    )
    if df is None or df.empty:
        raise ConnectionError(f"Tushare daily_basic({day}) 返回空数据")
    return pd.DataFrame({
        "code": df.ts_code.astype(str).str.split(".").str[0].str.zfill(6),
        "turnover": pd.to_numeric(df.turnover_rate, errors="coerce"),
        # Tushare float_share 单位为万股，保留以便日后复核/计算。
        "float_share": pd.to_numeric(df.float_share, errors="coerce"),
    }).dropna(subset=["code"])


def bootstrap_daily_basic(start: str, end: str) -> dict:
    """按交易日补齐策略回放需要的全市场换手率快照；已落盘日期跳过。"""
    from tools.store import repo as store
    days = trade_dates(start, end)
    written = 0
    for i, day in enumerate(days, 1):
        try:
            store.get_master_daily_basic(day)
            continue
        except FileNotFoundError:
            pass
        store.put_master_daily_basic(day, fetch_daily_basic_all(day))
        written += 1
        if i % 20 == 0 or i == len(days):
            import logging
            logging.getLogger("collectors.tushare_daily").info("daily_basic %d/%d: %s", i, len(days), day)
    return {"days": len(days), "written": written}


def bootstrap_master(start: str, end: str) -> dict:
    """按交易日批量首灌全 A 主档。"""
    from tools.store import repo as store
    days, frames = trade_dates(start, end), []
    for i, day in enumerate(days, 1):
        frames.append(fetch_daily_adjusted(day))
        if i % 20 == 0 or i == len(days):
            import logging
            logging.getLogger("collectors.tushare_daily").info("Tushare 历史下载 %d/%d: %s", i, len(days), day)
    all_days = pd.concat(frames, ignore_index=True)
    for code, df in all_days.groupby("code", sort=False):
        store.put_master_kline(str(code), df.drop(columns="code").sort_values("date"),
                               meta={"source": "tushare_daily+adj_factor", "adjust": "factor_scaled"})
    return {"days": len(days), "stocks": int(all_days.code.nunique()), "rows": len(all_days)}
