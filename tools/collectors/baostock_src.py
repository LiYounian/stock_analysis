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

import pandas as pd

logger = logging.getLogger("collectors.baostock")

# adjust 关键字 → baostock adjustflag
_ADJUST_FLAG = {"qfq": "2", "hfq": "1", "": "3", "none": "3", None: "3"}
_BS_FIELDS = "date,open,high,low,close,volume,amount,turn,pctChg"
_STD_COLS = ["date", "open", "high", "low", "close", "volume", "amount", "turnover", "pct_chg"]


def bs_code(code: str) -> str:
    """6 位代码 → baostock 代码 sh.xxxxxx / sz.xxxxxx / bj.xxxxxx。"""
    if code[0] in ("6", "9"):
        return f"sh.{code}"
    if code[0] in ("0", "2", "3"):
        return f"sz.{code}"
    if code[0] in ("8", "4"):
        return f"bj.{code}"
    return code


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
    flag = _ADJUST_FLAG.get(adjust, "2")
    rs = bs.query_history_k_data_plus(
        bs_code(code), _BS_FIELDS,
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
