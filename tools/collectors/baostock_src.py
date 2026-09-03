"""baostock 行情源:全量历史日K(前复权),供"滚动主档"一次性落地用。

baostock 是数据 API(非爬虫)→ 不封、无需 0.5s sleep;登录态会超时,故用
`session()` 上下文管理器统一 login/logout + 异常兜底。

口径(基准实测,见 docs/计划/全A采集优化方案.md 交接件):baostock 前复权
(adjustflag=2)与 akshare 前复权**最新交易日收盘精确一致**;历史较老 bar 因分红
回溯因子实现差异有 <2% 的平滑偏差(同一分红区间内形态不变),口径一致可换源。

输出列与 market._STD_COLS 对齐:
    date, open, high, low, close, volume, amount, turnover, pct_chg
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

from tools.config import exchange

import pandas as pd

logger = logging.getLogger("collectors.baostock")

# adjust 关键字 → baostock adjustflag
_ADJUST_FLAG = {"qfq": "2", "hfq": "1", "": "3", "none": "3", None: "3"}
_BS_FIELDS = "date,open,high,low,close,volume,amount,turn,pctChg"
_STD_COLS = ["date", "open", "high", "low", "close", "volume", "amount", "turnover", "pct_chg"]


def bs_code(code: str) -> str | None:
    """6 位代码 → baostock 代码 `sh.600000` / `sz.000001`;**北交所与判不出的返回 `None`**。

    判据在 `tools.config.exchange`(**单一真源**)。

    ⚠️ baostock **不覆盖北交所**(2026-09-03 实测 query_history_k_data_plus):

        sh.600000  error_code 0 success       11 行 ✅
        bj.920002  error_code 10004011「股票代码未标识sh或sz」  0 行
        bj.430047  error_code 10004011        0 行
        sz.920002  error_code 0 **success**   **0 行** ← 静默空
        sh.920002  error_code 0 **success**   **0 行** ← 静默空

    协议只认 `sh.`/`sz.`。原实现把 920 段按"9 开头"映到 `sh.920002` → success + 0 行,
    调用方看到的是"这只票没有历史数据",而不是"这个源不支持北交所"。故这里显式返回
    None 表达"源不支持",由调用方记降级。
    """
    return exchange.dotted(code)


@contextmanager
def session():
    """登录 baostock,退出时登出。登录失败抛 ConnectionError。"""
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise ConnectionError(f"baostock 登录失败 {lg.error_code}: {lg.error_msg}")
    logger.info("baostock 登录成功")
    try:
        yield bs
    finally:
        bs.logout()
        logger.info("baostock 登出")


def fetch_one(code: str, start: str, end: str, adjust: str = "qfq") -> pd.DataFrame:
    """拉单票日K(baostock)。start/end 用 YYYY-MM-DD。需在 session() 内调用。

    返回归一化 df(_STD_COLS);空数据抛 ValueError,接口错误抛 ConnectionError。
    """
    import baostock as bs
    sym = bs_code(code)
    if sym is None:                    # 北交所/判不出 → 源不支持,显式降级(不静默返回空 df)
        logger.warning("baostock %s 降级跳过:该源不覆盖北交所(实测 sz./sh. 前缀返 success+0行)", code)
        raise ValueError(f"baostock 不支持该代码(北交所或非A股): {code}")
    flag = _ADJUST_FLAG.get(adjust, "2")
    rs = bs.query_history_k_data_plus(
        sym, _BS_FIELDS,
        start_date=start, end_date=end, frequency="d", adjustflag=flag)
    if rs.error_code != "0":
        raise ConnectionError(f"baostock {code} error {rs.error_code}: {rs.error_msg}")
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        raise ValueError(f"baostock {code} 空数据")
    df = pd.DataFrame(rows, columns=rs.fields)
    df = df.rename(columns={"turn": "turnover", "pctChg": "pct_chg"})
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "high", "low", "close", "volume", "amount", "turnover", "pct_chg"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)
    for c in _STD_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    return df[_STD_COLS]
