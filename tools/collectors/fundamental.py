"""基本面采集:财报关键指标 + 估值 + 每股现金分红(TTM)。

数据源(本机实测可用,避开被指纹墙的东财):
  - 同花顺 `stock_financial_abstract`:营收/净利/增速/ROE/毛利率/净利率/负债率。
  - 百度 `stock_zh_valuation_baidu`:PE(TTM)/PB/总市值 + PE 分位(窗口见 settings.PE_PCTL_PERIOD,
    默认全历史;供护栏判高估。记录旁记 `PE分位窗口` 口径)。
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


def fetch_valuation_series(code: str, *, hk: bool = False) -> pd.DataFrame:
    """百度估值**整条历史序列** → DataFrame[date, PE_TTM, PB, 总市值](按 date 外连接对齐、升序)。

    旧实现只取 `vals[-1]` 丢了整条历史 → store 里 V 维只有零散近端快照、无法历史回测。本函数
    保留 date+value 全序列(供落盘 kind="valuation"),让 REVS 等能按 as_of 读任意历史日的 V。
    逐 indicator 拉取:单项失败该列缺失(不拖累其它列);全失败返回空帧(列头保留)。
    period 用 `settings.PE_PCTL_PERIOD`(默认全历史),与 PE 分位窗口同口径、复用同一次网络。
    hk=True 走港股端点 `stock_hk_valuation_baidu`(indicator 口径与 A 股同)。
    """
    import akshare as ak

    fn = ak.stock_hk_valuation_baidu if hk else ak.stock_zh_valuation_baidu
    period = settings.PE_PCTL_PERIOD
    merged = None
    for key, ind in _BAIDU_MAP.items():
        try:
            df = fn(symbol=code, indicator=ind, period=period)
            if df is None or not len(df) or "value" not in getattr(df, "columns", []):
                continue
            date_col = "date" if "date" in df.columns else df.columns[0]
            part = pd.DataFrame({
                "date": pd.to_datetime(df[date_col], errors="coerce"),
                key: [_to_float(x) for x in df["value"].tolist()],
            }).dropna(subset=["date"])
            merged = part if merged is None else merged.merge(part, on="date", how="outer")
        except Exception as e:  # 单项估值失败不影响其他指标
            logger.debug("%s 百度估值序列 %s 失败: %s", code, ind, e)
    if merged is None or not len(merged):
        return pd.DataFrame(columns=["date", *_BAIDU_MAP.keys()])
    return merged.sort_values("date").reset_index(drop=True)


def _valuation_scalars(series: pd.DataFrame) -> dict:
    """从估值整条序列派生标量:各指标**最新值** + `PE分位`(最新 PE(TTM) 在整条 PE 序列中的分位)。

    口径与旧 `_fetch_baidu` 完全一致(vals[-1] 取最新、_percentile 取分位、单指标缺失→None),
    只是数据来源从"每次现拉"改为"从已拉好的整条序列派生",避免与序列落盘重复网络。
    """
    out = {"PE分位窗口": settings.PE_PCTL_PERIOD}
    cols = getattr(series, "columns", [])
    for key in _BAIDU_MAP:
        vals = ([v for v in series[key].tolist() if v is not None and not pd.isna(v)]
                if key in cols else [])
        out[key] = vals[-1] if vals else None
        if key == "PE_TTM":
            out["PE分位"] = _percentile(vals, vals[-1]) if vals else None
    return out


def _fetch_baidu(code: str) -> dict:
    """百度估值标量(最新值 + PE 分位)。整条历史序列由 `fetch_valuation_series` 提供、单独落盘
    (kind="valuation",供 V 维历史回测);本函数只从序列派生标量,口径不变(向后兼容旧调用)。"""
    return _valuation_scalars(fetch_valuation_series(code))


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
    港股不经 baostock,直接置 None(缺失,上层降级)。
    """
    from tools.config import stock_pool

    if not codes:
        return {}
    as_of = as_of or pd.Timestamp.today().strftime("%Y-%m-%d")
    # 港股 baostock 不支持,直接标缺失
    a_codes = [c for c in codes if not stock_pool.is_hk(c)]
    hk_codes = [c for c in codes if stock_pool.is_hk(c)]
    out: dict[str, float | None] = {c: None for c in hk_codes}
    if not a_codes:
        return out
    try:
        from tools.collectors.baostock_src import bs_code, session
    except Exception as e:                             # noqa: BLE001
        logger.warning("baostock 分红源不可用(降级缺失): %s", e)
        return out
    try:
        with session() as bs:
            for code in a_codes:
                sym = bs_code(code)
                if sym is None:        # 北交所:baostock 不覆盖 → 显式记降级、标缺失
                    logger.warning("股息率 %s 降级缺失:baostock 不覆盖北交所", code)
                    out[code] = None
                    continue
                out[code] = _dividend_ttm_ps(bs, sym, as_of)
    except Exception as e:                             # noqa: BLE001 —— 登录失败等 → 全体缺失
        logger.warning("baostock 分红会话失败,股息率维度整体降级缺失: %s", e)
    return out


def _fetch_hk_fundamental(code: str) -> tuple[dict, pd.DataFrame]:
    """港股基本面:东财核心指标 + 百度港股估值。返回 (记录 dict, 估值整条序列 df)。

    序列 df 由调用方落盘 kind="valuation"(供 V 维历史回测);估值标量口径与 A 股一致。
    """
    import akshare as ak

    rec: dict = {"报告期": None}
    try:
        df = ak.stock_hk_financial_indicator_em(symbol=code)
        if df is not None and len(df):
            row = df.iloc[0]
            rec["营收"] = _to_float(row.get("营业总收入"))
            rec["净利"] = _to_float(row.get("净利润"))
            rec["营收增速"] = _to_float(row.get("营业总收入滚动环比增长(%)"))
            rec["净利增速"] = _to_float(row.get("净利润滚动环比增长(%)"))
            rec["ROE"] = _to_float(row.get("股东权益回报率(%)"))
            rec["毛利率"] = None
            rec["净利率"] = _to_float(row.get("销售净利率(%)"))
            rec["负债率"] = None
            rec["每股股利"] = _to_float(row.get("每股股息TTM(港元)"))
    except Exception as e:
        logger.warning("港股 %s 东财财务指标失败: %s", code, e)
    # 百度港股估值:整条序列 + 派生标量(PE 分位窗口同 A 股口径,见 settings.PE_PCTL_PERIOD,#32)
    vseries = fetch_valuation_series(code, hk=True)
    rec.update(_valuation_scalars(vseries))
    return rec, vseries


def fetch_fundamental(codes: list[str], as_of: str | None = None) -> dict[str, dict]:
    """拉取多票基本面并落盘。

    A股:同花顺财务摘要 + 百度估值 + baostock 每股现金分红(TTM)。
    港股:东财核心指标 + 百度港股估值。
    单票整体失败记 logger 并跳过,不中断整批。分红维度整体不可得时不阻断其余字段。
    """
    from tools.config import stock_pool

    settings.ensure_dirs()
    div_map = fetch_dividends(codes, as_of)            # 一次 baostock 会话取全批分红(best-effort)
    out: dict[str, dict] = {}
    failed: list[str] = []
    n = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] 基本面 %s 采集...", i, n, code)
        try:
            if stock_pool.is_hk(code):
                rec, vseries = _fetch_hk_fundamental(code)
                store.put_raw("fundamental", code, rec, meta={"source": "eastmoney_hk+百度"})
                if len(vseries):
                    store.put_raw("valuation", code, vseries, meta={"source": "百度港股估值序列"})
            else:
                rec = _fetch_abstract(code)
                vseries = fetch_valuation_series(code)             # 整条历史序列(一次网络)
                rec.update(_valuation_scalars(vseries))           # 派生标量(口径不变)
                rec["每股股利"] = div_map.get(code)
                store.put_raw("fundamental", code, rec, meta={"source": _SOURCE})
                if len(vseries):                                  # 整条序列落盘,供 V 维历史回测
                    store.put_raw("valuation", code, vseries, meta={"source": "百度估值序列"})
            out[code] = rec
            logger.info("基本面 %s 落盘(报告期 %s)", code, rec.get("报告期"))
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
