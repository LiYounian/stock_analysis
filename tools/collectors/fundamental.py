"""基本面采集:财报关键指标 + 估值 + 每股现金分红(TTM)。

数据源(本机实测可用,避开被指纹墙的东财):
  - 同花顺 `stock_financial_abstract`:营收/净利/增速/ROE/毛利率/净利率/负债率。
  - 百度 `stock_zh_valuation_baidu`:PE(TTM)/PB/总市值 + PE 近一年分位(供护栏判高估)。
  - baostock `query_dividend_data`:近 12 个月(按除权除息日)累计**每股现金分红(税前)**,
    供多因子「股息率」= 每股股利 / 最新收盘价(股息率的价格分母在分析层用 K线算,见 factor.py)。
    baostock 是数据 API(非爬虫、不封),本机实测可用。**无分红票 → 每股股利 = 0.0(真 0,非缺失)**;
    baostock 登录/查询整体失败 → None(缺失,多因子该维降级),二者严格区分。
落盘:走 store 层(kind="fundamental",json),旁记 meta.source。
契约见 docs/计划/P2_结构化情绪与基本面.md。
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.fundamental")

# 数据来源标注(同花顺财务摘要 + 百度估值 + baostock 分红)
_SOURCE = "同花顺+百度+baostock"

# 输出字段 → 同花顺财务摘要指标名
_ABSTRACT_MAP = {
    "营收": "营业总收入", "净利": "归母净利润",
    "营收增速": "营业总收入增长率", "净利增速": "归属母公司净利润增长率",
    "ROE": "净资产收益率(ROE)", "毛利率": "毛利率",
    "净利率": "销售净利率", "负债率": "资产负债率",
}
# 输出字段 → 百度估值 indicator
_BAIDU_MAP = {"PE_TTM": "市盈率(TTM)", "PB": "市净率", "总市值": "总市值"}


def _to_float(v):
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _fetch_abstract(code: str) -> dict:
    """同花顺财务摘要,取最新报告期关键指标。"""
    import akshare as ak

    df = ak.stock_financial_abstract(symbol=code)
    if df is None or df.empty or "指标" not in df.columns:
        raise ValueError("财务摘要空/结构异常")
    period = df.columns[2]                       # 第 3 列为最新报告期
    out = {"报告期": str(period)}
    for key, ind in _ABSTRACT_MAP.items():
        row = df[df["指标"] == ind]
        out[key] = _to_float(row.iloc[0][period]) if len(row) else None
    return out


def _percentile(vals: list[float], x: float) -> float | None:
    """x 在 vals 中的分位(≤x 占比,0~1)。vals 空→None。供 PE 分位护栏。"""
    if not vals:
        return None
    return round(sum(1 for v in vals if v <= x) / len(vals), 4)


def _fetch_baidu(code: str) -> dict:
    """百度估值,取各 indicator 时间序列最新值。单项失败该字段 None。

    额外产出 `PE分位`:最新 PE(TTM) 在近一年序列中的分位(0~1),供护栏判"极度高估",
    复用同一次 PE 序列拉取、无额外网络。
    """
    import akshare as ak

    out = {}
    for key, ind in _BAIDU_MAP.items():
        try:
            df = ak.stock_zh_valuation_baidu(symbol=code, indicator=ind, period="近一年")
            vals = [v for v in (_to_float(x) for x in df["value"].tolist())
                    if v is not None] if len(df) else []
            out[key] = vals[-1] if vals else None
            if key == "PE_TTM":
                out["PE分位"] = _percentile(vals, vals[-1]) if vals else None
        except Exception as e:  # 单项估值失败不影响其他字段
            logger.debug("%s 百度 %s 失败: %s", code, ind, e)
            out[key] = None
            if key == "PE_TTM":
                out["PE分位"] = None
    return out


def _dividend_ttm_ps(bs, bscode: str, as_of: str) -> float | None:
    """近 12 个月(按除权除息日 dividOperateDate)累计每股现金分红(税前)。

    bs: 已登录的 baostock 模块;bscode: sh./sz./bj. 前缀代码。
    - 有分红记录且落在 (as_of-365d, as_of] 窗口 → 求和(可为 0,如窗口内无除权)。
    - **完全查不到分红记录**(接口正常但该票无分红)→ 0.0(真 0)。
    - 接口报错(登录失效/网络)→ None(缺失,交由上层降级)。
    """
    from datetime import datetime, timedelta

    try:
        asd = datetime.strptime(as_of, "%Y-%m-%d")
    except (TypeError, ValueError):
        asd = datetime.today()
    lo = asd - timedelta(days=365)
    total = 0.0
    try:
        for yr in (asd.year, asd.year - 1):           # 跨两年覆盖 12 个月窗口
            rs = bs.query_dividend_data(code=bscode, year=str(yr), yearType="operate")
            if rs.error_code != "0":
                return None                            # 接口错误 → 缺失
            while rs.next():
                d = dict(zip(rs.fields, rs.get_row_data()))
                exd = (d.get("dividOperateDate") or "").strip()
                cash = _to_float(d.get("dividCashPsBeforeTax"))
                if not exd or cash is None or cash <= 0:
                    continue
                try:
                    exdt = datetime.strptime(exd, "%Y-%m-%d")
                except ValueError:
                    continue
                if lo < exdt <= asd:
                    total += cash
    except Exception as e:                             # noqa: BLE001 —— 任何异常→缺失,不炸整批
        logger.debug("%s 分红查询失败(降级缺失): %s", bscode, e)
        return None
    return round(total, 6)


def fetch_dividends(codes: list[str], as_of: str | None = None) -> dict[str, float | None]:
    """批量取每股现金分红 TTM {code: 每股股利}。一次 baostock 会话覆盖全批。

    baostock 会话整体建不起来(登录失败/未装)→ 返回空 dict(全体缺失,上层降级)。
    单票查不到分红 → 0.0(真 0);单票查询报错 → None(缺失)。
    """
    if not codes:
        return {}
    as_of = as_of or pd.Timestamp.today().strftime("%Y-%m-%d")
    try:
        from tools.collectors.baostock_src import bs_code, session
    except Exception as e:                             # noqa: BLE001
        logger.warning("baostock 分红源不可用(降级缺失): %s", e)
        return {}
    out: dict[str, float | None] = {}
    try:
        with session() as bs:
            for code in codes:
                out[code] = _dividend_ttm_ps(bs, bs_code(code), as_of)
    except Exception as e:                             # noqa: BLE001 —— 登录失败等 → 全体缺失
        logger.warning("baostock 分红会话失败,股息率维度整体降级缺失: %s", e)
        return {}
    return out


def fetch_fundamental(codes: list[str], as_of: str | None = None) -> dict[str, dict]:
    """拉取多票基本面并落盘。

    合并同花顺财务摘要 + 百度估值 + baostock 每股现金分红(TTM)。
    单票整体失败记 logger 并跳过,不中断整批。分红维度整体不可得时不阻断其余字段。
    """
    settings.ensure_dirs()
    div_map = fetch_dividends(codes, as_of)            # 一次 baostock 会话取全批分红(best-effort)
    out: dict[str, dict] = {}
    failed: list[str] = []
    n = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] 基本面 %s 采集...", i, n, code)
        try:
            rec = _fetch_abstract(code)
            rec.update(_fetch_baidu(code))
            rec["每股股利"] = div_map.get(code)        # TTM 每股现金分红(0.0=无分红真值;None=缺失)
            store.put_raw("fundamental", code, rec, meta={"source": _SOURCE})
            out[code] = rec
            logger.info("基本面 %s 落盘(报告期 %s,每股股利 %s)", code, rec["报告期"], rec["每股股利"])
        except Exception as e:
            failed.append(code)
            logger.error("基本面 %s 失败: %s", code, e)
        time.sleep(settings.FETCH_SLEEP_SEC)
    if failed:
        logger.warning("基本面拉取失败(%d): %s", len(failed), failed)
    return out


def load_fundamental(code: str) -> dict:
    """从本地缓存读单票基本面。缓存缺失抛 FileNotFoundError。"""
    return store.get_raw("fundamental", code)
