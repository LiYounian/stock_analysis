"""事件驱动数据采集(F7):业绩预告 yjyg / 业绩快报 yjkb / 高管增减持 ggcg。

数据源:AKShare(封装东财)——`stock_yjyg_em(date=报告期)` / `stock_yjkb_em(date=报告期)` /
`stock_ggcg_em()`。AKShare 列名随版本/上游改版会变,故**列名按关键字模糊匹配 + 全程 try/except 降级**:
任一步失败 → 记 logger、返回空,绝不抛断整条流水(东财被墙/限流时优雅降级,见问题台账 B2/R5)。

缓存:走 store.put_raw(kind, tag, df);tag 用报告期/日期。消费见 tools/analysis/event_driven/summary.py。
⚠️ 未接入 run.py 定时(run.py 不在本轮改动范围);需要精数值时手动/后续流水调用本模块的 fetch_*。

依赖:pandas + (可选)akshare;不 import web/serialize。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.store import repo as store

logger = logging.getLogger("collectors.event_driven")


def _akshare():
    """惰性导入 akshare;未安装 → None(降级)。"""
    try:
        import akshare as ak
        return ak
    except Exception as e:                      # noqa: BLE001
        logger.warning("akshare 不可用,事件采集降级: %s", e)
        return None


def _find_col(df: pd.DataFrame, *keywords) -> str | None:
    """按关键字在列名里模糊找第一个命中列(容忍 AKShare 列名漂移)。"""
    for kw in keywords:
        for c in df.columns:
            if kw in str(c):
                return c
    return None


def _norm_code(x) -> str | None:
    s = "".join(ch for ch in str(x) if ch.isdigit())
    return s.zfill(6) if s else None


# ———————————————————— 业绩预告 / 快报 ————————————————————
def fetch_earnings_forecast(period: str, kind: str = "yjyg") -> pd.DataFrame:
    """拉某报告期业绩预告(kind=yjyg)或快报(kind=yjkb)并落盘。失败返回空 df。

    Args:
        period: 报告期 "YYYYMMDD"(如 "20240930")。
        kind: "yjyg"(预告)| "yjkb"(快报)。
    Returns:
        规整 df[code, 增速, 类型, 报告期];失败/无数据 → 空 df。
    """
    ak = _akshare()
    if ak is None:
        return pd.DataFrame()
    fn = {"yjyg": "stock_yjyg_em", "yjkb": "stock_yjkb_em"}.get(kind)
    try:
        raw = getattr(ak, fn)(date=period)
    except Exception as e:                       # noqa: BLE001
        logger.warning("%s(%s) 采集失败,降级: %s", fn, period, e)
        return pd.DataFrame()
    if raw is None or len(raw) == 0:
        return pd.DataFrame()

    code_col = _find_col(raw, "代码")
    # 增速优先"同比"/"变动幅度"/"净利润变动"等;快报可能是"净利润同比"
    growth_col = _find_col(raw, "同比", "变动幅度", "增长", "净利润变动")
    rows = []
    for _, r in raw.iterrows():
        code = _norm_code(r.get(code_col)) if code_col else None
        if not code:
            continue
        growth = None
        if growth_col is not None:
            try:
                growth = float(r.get(growth_col))
            except (TypeError, ValueError):
                growth = None
        rows.append({"code": code, "增速": growth, "类型": kind, "报告期": period})
    df = pd.DataFrame(rows)
    if not df.empty:
        try:
            store.put_raw(f"event_{kind}", period, df, meta={"source": "akshare-em"})
        except Exception as e:                   # noqa: BLE001
            logger.warning("事件缓存落盘失败(%s %s): %s", kind, period, e)
    logger.info("事件采集 %s %s:%d 条", kind, period, len(df))
    return df


def load_earnings(period: str, kind: str = "yjyg") -> pd.DataFrame:
    """读某报告期业绩预告/快报缓存。无缓存 → 空 df(不抛)。"""
    try:
        return store.get_raw(f"event_{kind}", period)
    except FileNotFoundError:
        return pd.DataFrame()


# ———————————————————— 高管/股东增减持 ————————————————————
def fetch_insider_trades(tag: str = "latest") -> pd.DataFrame:
    """拉高管/股东增减持(stock_ggcg_em)并落盘。失败返回空 df。

    Returns 规整 df[code, 方向(增持/减持), 变动股数, 方式, 日期];列名模糊匹配 + 降级。
    「方式」= 变动途径(如"协议转让"/"集中竞价"/"大宗交易"),供减持性质区分(协议转让给
    产业方 ≠ 二级市场抛售);源列缺失时为空,不影响其它字段。
    """
    ak = _akshare()
    if ak is None:
        return pd.DataFrame()
    try:
        raw = ak.stock_ggcg_em()
    except Exception as e:                       # noqa: BLE001
        logger.warning("stock_ggcg_em 采集失败,降级: %s", e)
        return pd.DataFrame()
    if raw is None or len(raw) == 0:
        return pd.DataFrame()

    code_col = _find_col(raw, "代码")
    dir_col = _find_col(raw, "变动方向", "增减")
    qty_col = _find_col(raw, "变动数量", "变动股数", "数量")
    method_col = _find_col(raw, "变动方式", "变动途径", "减持方式", "方式")
    date_col = _find_col(raw, "变动日期", "日期", "公告日")
    rows = []
    for _, r in raw.iterrows():
        code = _norm_code(r.get(code_col)) if code_col else None
        if not code:
            continue
        d = str(r.get(dir_col)) if dir_col else ""
        方向 = "增持" if ("增" in d) else ("减持" if ("减" in d) else None)
        qty = None
        if qty_col is not None:
            try:
                qty = float(r.get(qty_col))
            except (TypeError, ValueError):
                qty = None
        method = None
        if method_col is not None:
            mv = r.get(method_col)
            method = str(mv) if mv is not None and str(mv).strip() not in ("", "nan", "None") else None
        rows.append({"code": code, "方向": 方向, "变动股数": qty, "方式": method,
                     "日期": str(r.get(date_col)) if date_col else None})
    df = pd.DataFrame(rows)
    if not df.empty:
        try:
            store.put_raw("event_ggcg", tag, df, meta={"source": "akshare-em"})
        except Exception as e:                   # noqa: BLE001
            logger.warning("增减持缓存落盘失败: %s", e)
    logger.info("增减持采集:%d 条", len(df))
    return df


def load_insider_trades(tag: str = "latest") -> pd.DataFrame:
    """读增减持缓存。无缓存 → 空 df(不抛)。"""
    try:
        return store.get_raw("event_ggcg", tag)
    except FileNotFoundError:
        return pd.DataFrame()
