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
SCHEMA_VERSION = "1.0"


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def build_record(code: str, as_of: str) -> dict:
    """组装单票结构化记录。缺失的数据块降级为 None / 空,不抛错。"""
    s = stock_pool.get(code)
    kdf = _safe(lambda: market.load_kline(code))          # 加载一次,tech/predict 复用
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
                         ("营收", "净利", "营收增速", "净利增速", "ROE", "毛利率", "净利率", "负债率")} if fund else None

    events = [{"date": a.get("date"), "type": a.get("type"),
               "impact": a.get("impact"), "title": a.get("title")} for a in anns[:20]]

    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {"code": code, "name": s.name if s else code,
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


def serialize_all(as_of: str | None = None) -> dict[str, str]:
    """对全池组装并落盘 data/analysis/{code}.json。落盘前过 contracts 校验(§9.2)。返回 {code: path}。"""
    import pandas as pd

    from tools.contracts import record as contracts
    as_of = as_of or pd.Timestamp.today().strftime("%Y-%m-%d")
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out, invalid = {}, 0
    for code in stock_pool.get_codes():
        rec = build_record(code, as_of)
        errs = contracts.validate_record(rec)     # 契约优先:产出即校验,漂移当场暴露
        if errs:
            invalid += 1
            logger.warning("契约校验 %s:%d 处问题 %s", code, len(errs), errs[:3])
        p = _OUT_DIR / f"{code}.json"
        p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        out[code] = str(p)
    logger.info("结构化 JSON 落盘 %d 只(契约不合规 %d)→ %s", len(out), invalid, _OUT_DIR)
    return out


def load_record(code: str) -> dict:
    p = _OUT_DIR / f"{code}.json"
    if not p.exists():
        raise FileNotFoundError(f"{code} 无结构化记录,请先 serialize_all: {p}")
    return json.loads(p.read_text(encoding="utf-8"))
