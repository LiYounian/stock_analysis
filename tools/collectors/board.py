"""板块(行业)采集:申万一级行业指数日 K线 + 个股→板块映射(供 RS 中间层)。

用途:形态选股 RS 的中间层要「个股 vs 所属行业板块」「板块 vs 沪深300」,需板块指数
与个股所属板块。属采集层,策略层只经 store 读。

数据源选择(本机实测,详见 market.py R4 东财指纹墙):
  - 东财板块接口(name/hist/cons_em)整体被 TLS 指纹墙(RemoteDisconnected)——不可用。
  - THS 行业指数可用但本机 akshare 无成分接口。
  - **申万一级** `index_hist_sw`(裸代码)实测通、数据实时 → 作板块指数源;
    `sw_index_first_info` 作板块清单(31 个一级行业)。
  - 个股→板块**成分映射**:本机所有 akshare 成分接口均不可用(东财墙/THS缺/申万三级返0行)。
    故 membership 设为**可插拔槽**:传入 cons_fetcher 即启用;缺省记 warning 落空映射,
    绝不静默伪装(RS 个股vs板块层届时无数据,由编排层决定降级策略)。
落盘:
  - board_kline    parquet,code=一级行业名(与 membership 的板块名对齐)
  - board_membership json,code="all",{股票代码: 板块名}
列归一化复用 market._normalize(index_hist_sw 列名 开盘/收盘/最高/最低/成交量/成交额 命中映射)。
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from tools.collectors import market
from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.board")

_MEMBERSHIP_CODE = "all"      # 全市场映射存一份,固定 code


def _safe(name: str) -> str:
    """板块名做文件名时的兜底(申万一级行业名通常安全,仅防斜杠)。"""
    return name.replace("/", "_").strip()


def _sw_code(raw: str) -> str:
    """申万行业代码去交易所后缀(801010.SI → 801010;index_hist_sw 要裸代码)。"""
    return str(raw).split(".")[0]


def fetch_board_list() -> list[dict]:
    """申万一级行业清单 [{name, code(裸)}, ...]。空/失败抛 ConnectionError。"""
    import akshare as ak
    df = ak.sw_index_first_info()
    if df is None or len(df) == 0:
        raise ConnectionError("申万一级行业清单为空")
    out = [{"name": str(r["行业名称"]), "code": _sw_code(r["行业代码"])}
           for _, r in df.iterrows()]
    logger.info("申万一级行业清单:%d 个", len(out))
    return out


def _fetch_board_hist(code: str, start: str, end: str) -> pd.DataFrame:
    """申万一级指数日 K线(全历史→按 start/end 截)。"""
    import akshare as ak
    df = ak.index_hist_sw(symbol=code, period="day")
    if df is not None and len(df) and "日期" in df.columns:
        df = df.copy()
        df["日期"] = pd.to_datetime(df["日期"])
        s, e = pd.to_datetime(start), pd.to_datetime(end)
        df = df[(df["日期"] >= s) & (df["日期"] <= e)]
    return df


def fetch_board_kline(names: list[str], start: str | None = None,
                      end: str | None = None) -> dict[str, pd.DataFrame]:
    """拉多个一级行业指数日 K线并落盘(按行业名传入)。单板块失败记 log 跳过。返回 {name: df}。"""
    settings.ensure_dirs()
    if start is None:
        start = (pd.Timestamp.today() - pd.Timedelta(days=settings.KLINE_DAYS * 2)
                 ).strftime("%Y%m%d")
    if end is None:
        end = pd.Timestamp.today().strftime("%Y%m%d")

    code_of = {b["name"]: b["code"] for b in fetch_board_list()}
    out: dict[str, pd.DataFrame] = {}
    for name in names:
        code = code_of.get(name)
        if not code:
            logger.error("板块 %s 不在申万一级清单,跳过", name)
            continue
        try:
            df = _fetch_board_hist(code, start, end)
            if df is None or len(df) == 0:
                raise ValueError("空数据")
            df = market._normalize(df)
            store.put_raw("board_kline", _safe(name), df,
                          meta={"source": "sw", "code": code})
            out[name] = df
            logger.info("板块 %s(%s)落盘 %d 根", name, code, len(df))
        except Exception as e:
            logger.error("板块 %s K线失败: %s", name, e)
        time.sleep(settings.FETCH_SLEEP_SEC)
    return out


def fetch_membership(cons_fetcher=None, boards: list[dict] | None = None) -> dict[str, str]:
    """构建 {股票代码: 板块名} 全市场映射并落盘。

    cons_fetcher(board_name) -> list[股票代码]:成分获取器,**可插拔**。
      缺省 None:本机无可用成分源,记 warning 落空映射(不静默伪装、不空跑网络)。
    一只股票落**首个命中**板块(与 board_kline 的板块名对齐)。
    """
    settings.ensure_dirs()
    membership: dict[str, str] = {}
    if cons_fetcher is None:
        logger.warning("成分源不可用(东财墙/THS缺/申万三级返0);个股→板块映射置空,"
                       "RS 个股vs板块层无数据,降级策略交编排层")
    else:
        boards = boards or fetch_board_list()
        for b in boards:
            name = b["name"]
            try:
                for code in cons_fetcher(name):
                    membership.setdefault(str(code), name)   # 首个命中的板块
            except Exception as e:
                logger.error("板块 %s 成分失败: %s", name, e)
            time.sleep(settings.FETCH_SLEEP_SEC)
        if not membership:
            logger.warning("成分获取器未产出任何映射:检查数据源")
    store.put_raw("board_membership", _MEMBERSHIP_CODE, membership,
                  meta={"source": "n/a" if cons_fetcher is None else "custom",
                        "mapped_stocks": len(membership)})
    logger.info("个股→板块映射:%d 只股票", len(membership))
    return membership


def fetch_membership_baostock() -> dict[str, str]:
    """baostock 全市场「个股→证监会行业」映射(一次查询,不走东财/申万)并落盘。

    baostock 自有协议(非东财 push2,不受本机路径墙影响),query_stock_industry 返回
    全A行业分类;约 94% 覆盖(空的多为退市/僵尸股,跳过)。code 去 sh./sz. 前缀存 6 位。
    """
    settings.ensure_dirs()
    import baostock as bs
    import contextlib
    import io
    buf = io.StringIO()
    membership: dict[str, str] = {}
    with contextlib.redirect_stdout(buf):        # baostock 登录/登出会打印,吞掉
        bs.login()
        try:
            rs = bs.query_stock_industry()
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()          # [updateDate, code, code_name, industry, cls]
                code, industry = row[1], (row[3] or "").strip()
                if industry:
                    membership[code.split(".")[-1]] = industry   # sh.600000 → 600000
        finally:
            bs.logout()
    store.put_raw("board_membership", _MEMBERSHIP_CODE, membership,
                  meta={"source": "baostock", "分类": "证监会行业",
                        "mapped_stocks": len(membership)})
    logger.info("个股→板块映射(baostock 证监会):%d 只", len(membership))
    return membership


def load_board_kline(name: str) -> pd.DataFrame:
    """读单板块指数日 K线(策略层用,不触网)。缺失抛 FileNotFoundError。"""
    return store.get_raw("board_kline", _safe(name))


def load_membership() -> dict[str, str]:
    """读全市场 {股票代码: 板块名} 映射。缺失抛 FileNotFoundError。"""
    return store.get_raw("board_membership", _MEMBERSHIP_CODE)


def board_of(code: str) -> str | None:
    """查单只股票所属板块;映射缺失或未收录返回 None(advisory,不抛)。"""
    try:
        return load_membership().get(code)
    except FileNotFoundError:
        return None
