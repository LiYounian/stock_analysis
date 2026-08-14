"""每票结构化 JSON 组装(程序/DB/Web 可消费的数据层产出)。

把散落在各缓存里的技术/基本面/公告/资金流,汇成一条清晰 schema 的记录,
落 data/analysis/{code}.json。区分当前快照 / 派生信号 / 时间序列指针(不塞大数组)。
schema 见 docs/数据结构说明.md。
"""
from __future__ import annotations

import json
import logging

from tools.analysis import predict as pr
from tools.analysis import technical as ta
from tools.analysis import valuation
from tools.collectors import announcement as an
from tools.collectors import fundamental as fd
from tools.collectors import market
from tools.config import settings, stock_pool

logger = logging.getLogger("analysis.serialize")

_OUT_DIR = settings.PROJECT_ROOT / "data" / "analysis"

_CODE_NAME: dict | None = None


def _code_name(code: str) -> str | None:
    """全A 代码→名称(config/code_name.json,模块级只加载一次)。缺失/损坏 → None。
    自选池外的票(screenall 选出票)没有 stock_pool 名,靠这里补名,避免 meta.name 落成代码。"""
    global _CODE_NAME
    if _CODE_NAME is None:
        try:
            _CODE_NAME = json.loads(
                (settings.PROJECT_ROOT / "config" / "code_name.json").read_text("utf-8"))
            if not isinstance(_CODE_NAME, dict):
                _CODE_NAME = {}
        except Exception:
            _CODE_NAME = {}
    return _CODE_NAME.get(code)
SCHEMA_VERSION = "1.0"


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def build_record(code: str, as_of: str) -> dict:
    """组装单票结构化记录。缺失的数据块降级为 None / 空,不抛错。"""
    s = stock_pool.get(code)
    kdf = _safe(lambda: market.load_kline_recent(code))          # 加载一次,tech/predict 复用
    tech = _safe(lambda: ta.compute(kdf), {}) if kdf is not None else {}
    fund = _safe(lambda: fd.load_fundamental(code), {}) or {}
    anns = _safe(lambda: an.load_announcements(code), []) or []

    # 资金流摘要(P3.1 已加;不存在则 None)
    flow = None
    try:
        from tools.collectors import fundflow as ff
        flow = _safe(lambda: ff.summarize(ff.load_fundflow(code)))
    except Exception:
        flow = None

    has_tech = "signal" in tech
    snapshot = None
    signals = None
    if has_tech:
        snapshot = {
            "close": tech["last"]["close"], "pct_chg": tech["last"]["pct_chg"],
            "ma": tech["ma"], "macd": tech["macd"], "kdj": tech["kdj"],
            "rsi": tech["rsi"], "bias20": tech["bias"]["bias20"],
            "vol_ratio": tech["vol"]["量比"], "vol_state": tech["vol"]["状态"],
        }
        signals = {"trend": tech["signal"], "reversal": tech["reversal"], "ob_os": tech["ob_os"]}

    # 情绪面(P2-C,LLM;需先 run.py sentiment 生成,否则 None)
    sentiment = None
    try:
        from tools.analysis import event
        srec = _safe(lambda: event.load_sentiment(code))
        if srec:
            sentiment = {**srec.get("sentiment", {}),
                         "events": [e for e in srec.get("events", [])
                                    if e.get("与本股关系") in ("直接", "间接")][:8]}
    except Exception:
        sentiment = None

    # 预测/推荐(P3.2):止盈止损%/情景/买卖倾向。需 tech + kline。
    prediction = None
    if has_tech and kdf is not None:
        prediction = _safe(lambda: pr.predict(kdf, tech, flow, sentiment=sentiment))

    valuation_block = None
    if fund:
        sw = valuation.pe_switch(fund)
        valuation_block = {
            "pe_ttm": fund.get("PE_TTM"), "pb": fund.get("PB"),
            "mktcap_yi": fund.get("总市值"), "报告期": fund.get("报告期"), **sw,
        }
    fundamental_block = {k: fund.get(k) for k in
                         ("营收", "净利", "营收增速", "净利增速", "ROE", "毛利率", "净利率", "负债率",
                          "每股股利")} if fund else None

    events = [{"date": a.get("date"), "type": a.get("type"),
               "impact": a.get("impact"), "title": a.get("title")} for a in anns[:20]]

    rec = {
        "schema_version": SCHEMA_VERSION,
        "meta": {"code": code, "name": s.name if s else (_code_name(code) or code),
                 "sector": s.sector if s else None, "industry": s.industry if s else None,
                 "as_of": as_of},
        "snapshot": snapshot,
        "valuation": valuation_block,
        "fundamental": fundamental_block,
        "signals": signals,
        "prediction": prediction,
        "sentiment": sentiment,
        "fundflow": flow,
        "events": events,
        "timeseries_refs": {
            "kline": f"data/raw/kline/{code}.parquet",
            "fundflow": f"data/raw/fundflow/{code}.parquet",
            "announcements": f"data/raw/announcement/{code}.json",
        },
        "provenance": {"tech": bool(has_tech), "fundamental": bool(fund),
                       "announcements": len(anns), "fundflow": bool(flow)},
    }

    # 多策略合议(F5·D7):后端预算各专家信封 + 默认组合议结果 + config(tau/权重),随记录落库。
    # 默认组专家均 record-shaped(读上面已装好的 signals/fundflow/sentiment),故 kline 可选。
    # 展示层只读本块;前端勾选按落库 config 权重重合成(不触发后端重算)。
    from tools.analysis import council
    rec["council"] = _safe(lambda: council.build_council_block(rec, kdf))
    return rec


def serialize_all(as_of: str | None = None, codes: list[str] | None = None) -> dict[str, str]:
    """对给定票池(缺省全池)组装并经 store 按日期落盘。落盘前过 contracts 校验(§9.2)。

    落盘走 store.put_record(rec, date=as_of):记录进 data/analysis/<as_of>/{code}.json。
    返回 {code: path}。
    """
    import pandas as pd

    from tools.contracts import record as contracts
    from tools.store import repo as store
    as_of = as_of or pd.Timestamp.today().strftime("%Y-%m-%d")
    codes = codes or stock_pool.get_codes()
    out, invalid = {}, 0
    for code in codes:
        rec = build_record(code, as_of)
        errs = contracts.validate_record(rec)     # 契约优先:产出即校验,漂移当场暴露
        if errs:
            invalid += 1
            logger.warning("契约校验 %s:%d 处问题 %s", code, len(errs), errs[:3])
        out[code] = store.put_record(rec, date=as_of)
    logger.info("结构化 JSON 落盘 %d 只(契约不合规 %d,日期 %s)", len(out), invalid, as_of)
    return out


def load_record(code: str, date: str | None = "latest") -> dict:
    """读单票中心记录(缺省最新日期)。缺失抛 FileNotFoundError。"""
    from tools.store import repo as store
    return store.get_record(code, date=date)


def reattach_council(codes: list[str], as_of: str) -> int:
    """(编排用)横截面/事件数据就绪**之后**,重算 council 块并回写各记录。

    为什么二次附着:build_record 里首次附 council 时,多因子 code_view(横截面,需全池)
    与事件驱动精数值尚未产出 → 那两个专家会弃权。编排在 factor.precompute + 事件采集之后
    调本函数,council 重算即纳入全部专家(不再弃权)。council 块仍是唯一权威合成产物。

    只用 store 公开 API + council(调用,不改)。缺记录的票跳过。返回回写只数。
    """
    from tools.analysis import council
    from tools.collectors import market
    from tools.store import repo as store

    n = 0
    for code in codes:
        try:
            rec = store.get_record(code, date=as_of)
        except FileNotFoundError:
            continue
        kdf = _safe(lambda: market.load_kline_recent(code))
        rec["council"] = _safe(lambda: council.build_council_block(rec, kdf))
        store.put_record(rec, date=as_of)
        n += 1
    return n
