"""个股两融明细采集(融资买入额 / 融资余额)—— 资金流「融资盘背离甄别」治本数据源。

需求:docs/每日分析/策略建议/资金流融资盘甄别.md §3.1(治本)。
动机:东财「主力净流入」是按成交额/手数对**已成交逐笔**分类反推的口径,无法区分大买单
背后是机构/游资还是**融资加杠杆**;高两融题材票的「巨量流入」极易被误读为主力吸筹
(样本外靶子:新易盛300502 的「20亿」实为 16-17 亿融资买入撑起)。本采集器补齐个股
融资买入额/融资余额,供 analysis.margin_divergence 做「主力净流入 vs 融资买入」背离甄别。

数据源:akshare(交易所权威披露口径,盘后公布)
  · 沪:stock_margin_detail_sse(date)   —— 信用交易日期/标的证券代码/融资余额/融资买入额/融资偿还额/融券…
  · 深:stock_margin_detail_szse(date)  —— 证券代码/融资买入额/融资余额/融券卖出量/融券余量…(无日期列,由入参补)
  · 北:stock_margin_detail_bse(date)   —— 同深结构
数据天然按**日**返回全市场,故按日期一次拉取 → 分发到各票分区累积(照 collectors.lhb 模式)。

===== 防未来函数(红线) =====
两融明细为**盘后披露**(交易日 T 的融资数据在 T 收盘后才公开)。落盘按票记 `date`(=交易日 T)
的日序列;任何消费方(背离甄别/回测)按 **date ≤ as_of** 取数,严禁用未来交易日。
本采集器只搬运披露值,不放松未来性约束。

===== 落盘契约(照 lhb.py 模式) =====
- raw kind = "margin",按 code + 采集日分区(走 store 层),payload = 该票两融日记录列表
  (按 date 倒序);**幂等 + 前向增量并集**:同票同 date 去重合并,重跑不产重复。
- **优雅降级**:限流/非 200/空/结构漂移/某交易所缺失一律跳过,不中断整批。

⚠️ 非投资建议;历史披露数据仅供研究。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, timedelta

import pandas as pd

from tools.store import repo as store

logger = logging.getLogger("collectors.margin")

_SOURCE = "akshare"
_KIND = "margin"

# 门控/窗口默认值(env 可覆盖;不改 settings.py,保持文件归属边界)
MARGIN_STALE_DAYS = float(os.getenv("MARGIN_STALE_DAYS", "1"))       # 缓存≤1天视为新鲜
MARGIN_LOOKBACK_DAYS = int(os.getenv("MARGIN_LOOKBACK_DAYS", "7"))   # 未给区间时默认回看自然日
_FETCH_SLEEP = float(os.getenv("MARGIN_FETCH_SLEEP", "0.4"))         # 逐交易所/逐日请求间隔


def _to_float(v):
    """宽松转 float;失败/空 → None。"""
    try:
        if v is None or v == "" or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _norm_date(d: str) -> str:
    """'YYYYMMDD' / 'YYYY-MM-DD' → 'YYYY-MM-DD'。"""
    s = str(d).strip().replace("-", "").replace("/", "")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return str(d)[:10]


def _norm_record(code: str, trade_date: str, rz_buy, rz_bal, rq_vol=None,
                 market: str = "") -> dict | None:
    """归一单票单日两融记录。无 code/融资买入额+融资余额均缺 → None(无效)。

    字段:date(交易日 T,盘后披露)/融资买入额/融资余额/融券余量/market/visible_after_close。
    """
    code = str(code).strip()
    if not code or not code.isdigit():
        return None
    buy = _to_float(rz_buy)
    bal = _to_float(rz_bal)
    if buy is None and bal is None:
        return None
    return {
        "code": code,
        "date": _norm_date(trade_date),
        "融资买入额": buy,
        "融资余额": bal,
        "融券余量": _to_float(rq_vol),
        "market": market,
        "visible_after_close": True,   # 盘后披露 → T+1 才可用(防未来函数标记)
    }


# ————————————————————————————————————————————————
# 单交易所拉取 → 归一记录列表(不落盘,便于回测/mock)
# ————————————————————————————————————————————————
def _fetch_sse(trade_date: str) -> list[dict]:
    """沪市两融明细。列:信用交易日期/标的证券代码/融资余额/融资买入额/…"""
    import akshare as ak
    d = trade_date.replace("-", "")
    df = ak.stock_margin_detail_sse(date=d)
    if df is None or df.empty:
        return []
    out = []
    for r in df.to_dict("records"):
        rec = _norm_record(r.get("标的证券代码"), r.get("信用交易日期", d),
                           r.get("融资买入额"), r.get("融资余额"),
                           r.get("融券余量"), market="SSE")
        if rec:
            out.append(rec)
    return out


def _fetch_szse(trade_date: str) -> list[dict]:
    """深市两融明细。列:证券代码/融资买入额/融资余额/融券余量…(无日期列 → 用入参 date)。"""
    import akshare as ak
    d = trade_date.replace("-", "")
    df = ak.stock_margin_detail_szse(date=d)
    if df is None or df.empty:
        return []
    out = []
    for r in df.to_dict("records"):
        rec = _norm_record(r.get("证券代码"), d, r.get("融资买入额"),
                           r.get("融资余额"), r.get("融券余量"), market="SZSE")
        if rec:
            out.append(rec)
    return out


def _fetch_bse(trade_date: str) -> list[dict]:
    """北市两融明细。列同深市(无日期列 → 用入参 date)。"""
    import akshare as ak
    d = trade_date.replace("-", "")
    df = ak.stock_margin_detail_bse(date=d)
    if df is None or df.empty:
        return []
    out = []
    for r in df.to_dict("records"):
        rec = _norm_record(r.get("证券代码"), d, r.get("融资买入额"),
                           r.get("融资余额"), r.get("融券余量"), market="BSE")
        if rec:
            out.append(rec)
    return out


_EXCHANGES = (("SSE", _fetch_sse), ("SZSE", _fetch_szse), ("BSE", _fetch_bse))


def fetch_detail_by_date(trade_date: str) -> pd.DataFrame:
    """拉某交易日全市场(沪+深+北)两融明细,归一为 DataFrame(不落盘)。

    每个交易所独立 try:单所限流/空/结构漂移 → 跳过该所,不影响其余(优雅降级)。
    空(全部失败或非交易日)→ 空 DataFrame(不抛)。
    """
    rows: list[dict] = []
    for name, fn in _EXCHANGES:
        try:
            recs = fn(trade_date)
            rows.extend(recs)
            logger.info("两融 %s %s:%d 只", name, trade_date, len(recs))
        except Exception as exc:                       # noqa: BLE001
            logger.warning("两融 %s %s 拉取失败(降级跳过): %s", name, trade_date,
                           str(exc)[:120])
        time.sleep(_FETCH_SLEEP)
    return pd.DataFrame(rows)


def _default_window() -> tuple[str, str]:
    """未显式给区间时:[今天 − MARGIN_LOOKBACK_DAYS, 今天](YYYYMMDD)。"""
    end = date.today()
    start = end - timedelta(days=MARGIN_LOOKBACK_DAYS)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _daterange_days(start: str, end: str) -> list[str]:
    """闭区间内所有自然日 'YYYY-MM-DD'(升序)。非交易日拉回空,由降级吸收。"""
    s = pd.Timestamp(_norm_date(start))
    e = pd.Timestamp(_norm_date(end))
    if e < s:
        return []
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(s, e, freq="D")]


def _merge_incremental(new_records: list[dict], prev_records: list[dict]) -> list[dict]:
    """前向增量并集:新旧记录按 date 去重合并(新覆盖旧),按 date 倒序。

    保留历史日序列(无幸存者偏差),幂等去重(重跑同日不产重复)。
    """
    seen: set = set()
    merged: list[dict] = []
    for rec in list(new_records) + list(prev_records):
        d = rec.get("date", "")
        if d in seen:
            continue
        seen.add(d)
        merged.append(rec)
    merged.sort(key=lambda x: x.get("date", ""), reverse=True)
    return merged


def _prev_snapshot(code: str) -> list[dict]:
    """读该票最近一次 margin 快照(任意分区);无则 []。用于增量并集累积。"""
    try:
        prev = store.get_raw(_KIND, code)
        return prev if isinstance(prev, list) else []
    except FileNotFoundError:
        return []


def fetch_margin(start: str | None = None, end: str | None = None,
                 codes: list[str] | None = None) -> dict[str, list[dict]]:
    """批量采集两融明细并**按票落盘**(前向增量、幂等)。

    数据源天然按**日**返回全市场 → 逐交易日拉取 → 分发到各票分区累积。
    参数:
      start/end  日期区间(缺省 = 今天回看 MARGIN_LOOKBACK_DAYS 天)。
      codes      仅落这些票(白名单);None = 落区间内出现的所有票。
    落盘:store.put_raw("margin", code, [日记录...], meta=...)。返回 {code: [日记录...]}。
    单票写入失败 → log 跳过,不中断整批。
    """
    from tools.config import settings
    settings.ensure_dirs()

    s0, e0 = _default_window()
    days = _daterange_days(start or s0, end or e0)
    wl = set(codes) if codes else None

    # 逐日拉全市场,按 code 累积区间内所有日记录
    by_code: dict[str, list[dict]] = {}
    for d in days:
        df = fetch_detail_by_date(d)
        if df.empty:                                   # 非交易日/全所降级
            continue
        for rec in df.to_dict("records"):
            code = str(rec.get("code") or "")
            if not code or (wl is not None and code not in wl):
                continue
            by_code.setdefault(code, []).append(rec)

    out: dict[str, list[dict]] = {}
    failed: list[str] = []
    for code, recs in by_code.items():
        try:
            merged = _merge_incremental(recs, _prev_snapshot(code))
            store.put_raw(_KIND, code, merged,
                          meta={"source": _SOURCE, "kind_detail": "两融明细"})
            out[code] = merged
        except Exception as exc:                        # noqa: BLE001
            failed.append(code)
            logger.error("两融 %s 落盘失败: %s", code, str(exc)[:120])
    if failed:
        logger.warning("两融落盘失败(%d): %s", len(failed), failed[:10])
    logger.info("两融采集完成:%d 票(区间 %s)", len(out),
                (days[:1] + days[-1:]) if days else [])
    return out


# —— load / summarize(供分析层 as-of 读取)——
def load_margin(code: str) -> list[dict]:
    """从本地缓存读单票两融日序列(按 date 倒序)。缓存缺失抛 FileNotFoundError。"""
    recs = store.get_raw(_KIND, code)
    return recs if isinstance(recs, list) else []


def summarize_asof(records: list[dict], as_of: str | None) -> dict | None:
    """取 ≤ as_of 的**最新**一日两融记录(防未来函数)。无满足记录 → None。

    as_of 缺省(None)→ 取全序列最新一日(生产当日用)。
    """
    if not records:
        return None
    asof_key = _norm_date(as_of) if as_of else None
    best = None
    for r in records:
        d = str(r.get("date") or "")
        if not d:
            continue
        if asof_key is not None and d > asof_key:      # 未来交易日,剔(防未来函数)
            continue
        if best is None or d > str(best.get("date") or ""):
            best = r
    return dict(best) if best else None
