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

from tools.config import exchange, units

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


def login():
    """显式登录 baostock,返回 bs 模块;登录失败抛 ConnectionError。

    供"可重建会话"手动管理登录态用(坏会话 → logout()+login() 重建)。
    `session()` 内部也复用它,行为对既有调用者透明。
    """
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise ConnectionError(f"baostock 登录失败 {lg.error_code}: {lg.error_msg}")
    logger.info("baostock 登录成功")
    return bs


def logout():
    """显式登出 baostock(幂等、不抛)。会话重建/收尾都经它,失败静默(登出失败无碍)。"""
    try:
        import baostock as bs
        bs.logout()
        logger.info("baostock 登出")
    except Exception as e:  # noqa: BLE001
        logger.debug("baostock 登出忽略异常: %s", e)


@contextmanager
def session():
    """登录 baostock,退出时登出。登录失败抛 ConnectionError。"""
    bs = login()
    try:
        yield bs
    finally:
        logout()


# —— 会话级/网络级 vs 数据级 错误分类(单一真源在本模块:baostock 报错形态它最懂)——
# 会话级/网络级 → 可 logout+重登+重试;数据级/源不支持 → 终态 skip,**绝不重建**(防误伤
# 正常会话)。判据用描述性一般特征(类型 + 中/英关键词),不写死具体 error_code 清单,
# 未来 baostock 换措辞也不易误判。
_DATA_LEVEL_ZH = ("空数据", "不支持", "未标识", "无数据")
_DATA_LEVEL_EN = ("no data", "not identified", "empty", "unsupported")
_SESSION_LEVEL_ZH = ("登录失败", "网络", "连接", "接收", "发送", "超时")
_SESSION_LEVEL_EN = ("login", "network", "connection", "connect", "socket",
                     "timed out", "timeout", "reset", "refused", "closed",
                     "receive", "recv", "unreachable", "broken pipe")


def is_session_error(exc: BaseException) -> bool:
    """该异常是否为 baostock **会话级/网络级**错误(可 logout+重登+重试)。

    返回 False 表示**数据级/源不支持**(空数据、北交所不支持等)——这类是"这只票本就
    没数据",应按单票 skip,**绝不触发会话重建**(否则健康会话被无数据票误拖去重登 = 误伤)。

    判据(数据级优先短路):
      · ValueError → 数据级(fetch_one 用它表达"空数据 / 源不支持")。
      · 命中数据级关键词 → 数据级。
      · 命中会话级/网络关键词 → 会话级。
      · 兜底:ConnectionError / OSError 等网络异常类型 → 会话级。
    """
    msg = str(exc)
    low = f"{type(exc).__name__} {msg}".lower()
    # 1) 数据级短路:明确"本就没数据/源不支持"→ 绝不重建
    if isinstance(exc, ValueError):
        return False
    if any(m in msg for m in _DATA_LEVEL_ZH) or any(m in low for m in _DATA_LEVEL_EN):
        return False
    # 2) 会话级/网络级 → 可重建重试
    if any(m in msg for m in _SESSION_LEVEL_ZH) or any(m in low for m in _SESSION_LEVEL_EN):
        return True
    # 3) 兜底:网络类异常类型视为会话级
    return isinstance(exc, (ConnectionError, OSError))


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
    # turnover 口径声明:baostock 的 turn 本已是**百分数** → to_percent 无操作。
    # 走单一真源(tools.config.units)而不是"这里注释一句"——新增源必须在口径表登记。
    df = units.to_percent(df, "baostock")
    return df[_STD_COLS]
