"""机构一致预期采集(前瞻 EPS/净利)。

借鉴 a-stock-data §2.2。项目现有 fundamental 只有**已披露历史财务**,缺「市场对未来的预期」。
一致预期补上前瞻维度,可派生:
  - 预期 EPS(当年/次年)      机构一致预测每股收益。
  - 隐含预期增速             次年预期 EPS / 当年预期 EPS - 1。
  - 覆盖机构数               预测研报/机构数(<3 家时置信度低,标注供上层谨慎用)。
  - 预期 PEG(可选)          现价 / 预期EPS / (增速*100),留给分析层用现价算。

数据源:东财盈利预测(akshare `stock_profit_forecast_em`,免鉴权、字段较稳)。
  若东财该票无覆盖 → 尝试同花顺(`stock_profit_forecast_ths`)兜底(与 a-stock-data 同源 10jqka)。
  两源皆空 → 落空(降级),不伪造预期。
列名按 akshare 现行输出**防御式取数**;年度/EPS 列以关键词匹配,容忍列名漂移。
港股不覆盖 → 直接落空。
落盘:走 store 层(kind="consensus",json:{年度: {eps, 机构数}, ...} + 派生摘要),旁记 meta.source。
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.consensus")

_SOURCE_EM = "eastmoney"
_SOURCE_THS = "10jqka"


def _f(v):
    try:
        x = float(v)
        return None if pd.isna(x) else x
    except (TypeError, ValueError):
        return None


def _year_col(df: pd.DataFrame) -> str | None:
    """找「预测年度」列(容忍列名:预测年度/年度/报告期)。"""
    for c in ("预测年度", "年度", "报告期"):
        if c in df.columns:
            return c
    return None


def _eps_col(df: pd.DataFrame) -> str | None:
    """找「预测每股收益(均值)」列:优先精确名,退化为含『每股收益』且不含最小/最大的列。"""
    for c in ("预测每股收益", "每股收益", "预测每股收益(元)", "均值"):
        if c in df.columns:
            return c
    for c in df.columns:
        if "每股收益" in str(c) and "最小" not in str(c) and "最大" not in str(c):
            return c
    return None


def _inst_col(df: pd.DataFrame) -> str | None:
    """找「机构/研报数」列。"""
    for c in ("机构数", "研报数", "预测机构数", "预测家数"):
        if c in df.columns:
            return c
    return None


def _parse_forecast(df: pd.DataFrame) -> dict:
    """把盈利预测 df 解析成 {年度(str): {eps, insts}}(仅保留能解析出 EPS 的年度)。"""
    yc, ec = _year_col(df), _eps_col(df)
    if not yc or not ec:
        return {}
    ic = _inst_col(df)
    out: dict[str, dict] = {}
    for _, r in df.iterrows():
        year = str(r[yc]).strip()[:4]
        if not year.isdigit():
            continue
        eps = _f(r[ec])
        if eps is None:
            continue
        out[year] = {"eps": eps, "insts": _f(r[ic]) if ic else None}
    return out


def _fetch_em(code: str) -> dict:
    import akshare as ak
    df = ak.stock_profit_forecast_em(symbol=code)
    return _parse_forecast(df) if df is not None and len(df) else {}


def _fetch_ths(code: str) -> dict:
    import akshare as ak
    df = ak.stock_profit_forecast_ths(symbol=code, indicator="预测年报每股收益")
    return _parse_forecast(df) if df is not None and len(df) else {}


def summarize(fc: dict) -> dict:
    """派生一致预期摘要:当年/次年预期 EPS、隐含增速、覆盖机构数。

    「当年/次年」按年度键升序取最早两个 ≥ 今年的年度(缺则退化为已有最小两年)。
    """
    null = {"预期EPS当年": None, "预期EPS次年": None, "预期增速": None, "覆盖机构数": None}
    if not fc:
        return null
    years = sorted(fc.keys())
    this_year = pd.Timestamp.today().year
    fwd = [y for y in years if int(y) >= this_year] or years
    y0 = fwd[0]
    y1 = fwd[1] if len(fwd) > 1 else None
    eps0 = fc[y0]["eps"]
    eps1 = fc[y1]["eps"] if y1 else None
    growth = round(eps1 / eps0 - 1, 4) if (eps0 and eps1 and eps0 > 0) else None
    return {
        "预期EPS当年": eps0,
        "预期EPS次年": eps1,
        "预期增速": growth,
        "覆盖机构数": fc[y0].get("insts"),
    }


def fetch_one(code: str) -> tuple[dict, str]:
    """拉单票一致预期:东财优先,空则同花顺兜底。返回 (预测字典, 命中源)。两源皆空 → ({}, "")。"""
    try:
        fc = _fetch_em(code)
        if fc:
            return fc, _SOURCE_EM
    except Exception as e:
        logger.debug("东财盈利预测 %s 失败: %s", code, e)
    try:
        fc = _fetch_ths(code)
        if fc:
            return fc, _SOURCE_THS
    except Exception as e:
        logger.debug("同花顺盈利预测 %s 失败: %s", code, e)
    return {}, ""


def fetch_consensus(codes: list[str]) -> dict[str, dict]:
    """批量采集一致预期并落盘。单票两源皆空/失败 → 记 log 跳过,不中断整批;港股整体落空。"""
    from tools.config import stock_pool

    settings.ensure_dirs()
    out: dict[str, dict] = {}
    failed: list[str] = []
    n = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] 一致预期 %s 采集...", i, n, code)
        if stock_pool.is_hk(code):
            store.put_raw("consensus", code, {}, meta={"source": "none(hk)"})
            out[code] = {}
            continue
        try:
            fc, src = fetch_one(code)
            if not fc:
                failed.append(code)
                logger.warning("一致预期 %s:无机构覆盖,跳过", code)
                continue
            rec = {"forecast": fc, **summarize(fc)}
            store.put_raw("consensus", code, rec, meta={"source": src})
            out[code] = rec
            logger.info("一致预期 %s(%s):当年EPS %s 次年EPS %s 增速 %s",
                        code, src, rec["预期EPS当年"], rec["预期EPS次年"], rec["预期增速"])
        except Exception as e:
            failed.append(code)
            logger.error("一致预期 %s 失败: %s", code, e)
        time.sleep(settings.FETCH_SLEEP_SEC)
    if failed:
        logger.warning("一致预期拉取失败/跳过(%d): %s", len(failed), failed)
    return out


def load_consensus(code: str, as_of: str | None = None) -> dict:
    """读单票一致预期。缺失抛 FileNotFoundError。

    as_of point-in-time(去历史重建前视偏差):
      - as_of=None(当日/存在性检查):读全局最新快照(store.get_raw)。
      - as_of 指定(历史重建/回测):date-pin 到 **≤as_of 的最新采集分区**
        (store.get_raw_resolved),绝不返回未来分区的预期值;≤as_of 无任何分区
        (如首次采集之前的历史日)→ FileNotFoundError,交上层降级(不注入今值)。

    **点数据局限(锁死)**:一致预期是「采集当时」的机构预期快照,只能 date-pin 到
    最近的历史采集分区,**无法重构任意 as_of 当天的预期值**(源不提供历史快照)。
    故历史重建时该块要么是最近的 ≤as_of 快照、要么缺失降级——已杜绝未来函数,但
    分区颗粒度受实际采集频率限制(周级)。
    """
    if as_of is None:
        return store.get_raw("consensus", code)
    payload, _resolved, _fetched = store.get_raw_resolved("consensus", code, date=as_of)
    return payload
