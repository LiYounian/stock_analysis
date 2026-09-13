"""逐票研判的**事实原料装配**(headless 生成器的输入层)。

设计:docs/计划/2026-09-13_headless逐票研判生成器与双跑框架_P2实现计划.md §1/§2。

从 `data/analysis/<date>/` 汇聚单票事实(record / news_ai / sentiment / market_forecast β),
裁剪成紧凑、可喂 prompt 的事实块。**只做取数 + 防未来裁剪 + 摘要**,不做研判、不调 LLM。

**防未来函数**(硬红线):
  - record.meta.as_of 晚于 pick_date → 标记 future_leak(生成器据此拒跑该票或降级);
  - news_ai / sentiment.events 里 time 晚于 pick_date 的条目一律剔除;
  - 只选 ≤ pick_date 的经验版本(在 experience_recall,不在此)。

客观字段(名称/价/涨跌/命中策略)最终由 write_picks 从 record 回填,此处仅为研判提供上下文。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


def _analysis_root(data_root: Path | None) -> Path:
    if data_root is not None:
        return Path(data_root)
    from tools.config import settings
    return settings.PROJECT_ROOT / "data" / "analysis"


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _date_of(ts: str) -> str:
    """从形如 '2026-09-11 14:30' / '2026-09-11T..' / '2026-09-11' 的时间串取日期部分。"""
    if not ts or not isinstance(ts, str):
        return ""
    return ts[:10]


@dataclass
class StockFacts:
    code: str
    pick_date: str
    record: dict | None = None
    news: list = field(default_factory=list)          # 已按 pick_date 防未来过滤
    sentiment: dict | None = None
    market_forecast: dict | None = None
    future_leak: bool = False                          # record.as_of 晚于 pick_date
    notes: list[str] = field(default_factory=list)     # 装配告警(缺件/剔除条数等)

    @property
    def name(self) -> str:
        return ((self.record or {}).get("meta") or {}).get("name") or self.code

    @property
    def industry(self) -> str | None:
        return ((self.record or {}).get("meta") or {}).get("industry")


# ————————————————————————————————————————————————————————————————
# 取数 + 防未来
# ————————————————————————————————————————————————————————————————
def load_record(code: str, pick_date: str, data_root: Path | None = None) -> dict | None:
    return _read_json(_analysis_root(data_root) / pick_date / f"{code}.json")


def load_news(code: str, pick_date: str, data_root: Path | None = None) -> tuple[list, int]:
    """读 news_ai/<code>.json(list),剔除 time 晚于 pick_date 的条目(防未来)。返回 (保留, 剔除数)。"""
    raw = _read_json(_analysis_root(data_root) / pick_date / "news_ai" / f"{code}.json")
    if not isinstance(raw, list):
        return [], 0
    kept, dropped = [], 0
    for it in raw:
        t = _date_of((it or {}).get("time", "")) if isinstance(it, dict) else ""
        if t and t > pick_date:
            dropped += 1
            continue
        kept.append(it)
    return kept, dropped


def load_sentiment(code: str, pick_date: str, data_root: Path | None = None) -> dict | None:
    """读 sentiment/<code>.json;剔除 events 里 time 晚于 pick_date 的条目(防未来)。"""
    raw = _read_json(_analysis_root(data_root) / pick_date / "sentiment" / f"{code}.json")
    if not isinstance(raw, dict):
        return None
    evs = raw.get("events")
    if isinstance(evs, list):
        raw = dict(raw)
        raw["events"] = [e for e in evs
                         if not (_date_of((e or {}).get("time", "")) > pick_date)]
    return raw


def load_market_forecast(pick_date: str, data_root: Path | None = None) -> dict | None:
    return _read_json(_analysis_root(data_root) / pick_date / "market_forecast.json")


def assemble(code: str, pick_date: str, data_root: Path | None = None,
             *, news_limit: int = 12) -> StockFacts:
    """装配单票事实(含防未来裁剪)。"""
    rec = load_record(code, pick_date, data_root)
    facts = StockFacts(code=code, pick_date=pick_date, record=rec)

    if rec is None:
        facts.notes.append("record 缺失")
    else:
        as_of = ((rec.get("meta") or {}).get("as_of")) or ""
        if as_of and as_of > pick_date:
            facts.future_leak = True
            facts.notes.append(f"record.as_of({as_of}) 晚于 pick_date({pick_date})——防未来违规")

    news, dropped = load_news(code, pick_date, data_root)
    if dropped:
        facts.notes.append(f"news_ai 剔除 {dropped} 条晚于 pick_date 的未来条目")
    facts.news = news[:news_limit]

    facts.sentiment = load_sentiment(code, pick_date, data_root)
    facts.market_forecast = load_market_forecast(pick_date, data_root)
    return facts


# ————————————————————————————————————————————————————————————————
# 定性分类(SOP 步骤0;供经验检索,best-effort 启发式)
# ————————————————————————————————————————————————————————————————
def infer_qualitative(record: dict | None) -> str | None:
    """据 record 信号粗判票的定性形态(游资情绪连板/高位妖股超买/超卖反弹/趋势成长)。

    启发式、可为 None(拿不准就不给,经验检索仍会带常驻纪律 + 行业/策略召回)。
    """
    if not record:
        return None
    snap = record.get("snapshot") or {}
    sig = record.get("signals") or {}
    ob = (sig.get("ob_os") or {}).get("verdict") or ""
    rev = sig.get("reversal") or {}
    pct = snap.get("pct_chg")
    bias = snap.get("bias20")
    vol_state = snap.get("vol_state") or ""

    # 涨停 / 连板 / 情绪:pct_chg 近涨停 + 放量
    if isinstance(pct, (int, float)) and pct >= 9.5:
        return "游资情绪连板"
    # 高位超买:超买判定 + 高 bias + 放量
    if "超买" in ob or (isinstance(bias, (int, float)) and bias >= 8 and "放量" in vol_state):
        return "高位妖股超买"
    # 超卖反弹:超卖 / 反包 / 低位金叉
    if "超卖" in ob or rev.get("放量反包") or rev.get("低位金叉") or rev.get("超跌"):
        return "超卖反弹"
    # 趋势:均线多头 + 趋势评级好
    trend = (sig.get("trend") or {}).get("评级") or ""
    if trend in ("强", "较强", "多头"):
        return "趋势成长"
    return None


# ————————————————————————————————————————————————————————————————
# 事实块渲染(喂 client.extract 的 text)
# ————————————————————————————————————————————————————————————————
def _pick(d: dict | None, keys) -> dict:
    """从 dict 取子集(缺键跳过),控体量。"""
    if not isinstance(d, dict):
        return {}
    return {k: d[k] for k in keys if k in d}


def render_facts_text(facts: StockFacts, *, news_summary_limit: int = 10) -> str:
    """把 StockFacts 渲染成紧凑事实文本(中文标签 + 关键子字段),供 LLM 研判。

    只保留研判必需的salient字段,避免整个 record(含大量口径/新鲜度元数据)撑爆 token。
    """
    rec = facts.record or {}
    lines: list[str] = [f"标的:{facts.name}({facts.code}) 选股日:{facts.pick_date}"]
    if facts.future_leak:
        lines.append("⚠️ 防未来告警:record.as_of 晚于选股日,以下数据可能含未来信息,研判须保守。")

    meta = rec.get("meta") or {}
    lines.append(f"行业:{meta.get('industry')} 板块:{meta.get('sector')} record.as_of:{meta.get('as_of')}")

    snap = _pick(rec.get("snapshot"), ["close", "pct_chg", "ma", "macd", "kdj", "rsi",
                                       "bias20", "vol_ratio", "vol_state"])
    if snap:
        lines.append(f"【技术快照】{json.dumps(snap, ensure_ascii=False)}")

    val = _pick(rec.get("valuation"), ["pe_ttm", "pb", "mktcap_yi", "报告期", "pe_valid"])
    if val:
        lines.append(f"【估值】{json.dumps(val, ensure_ascii=False)}")

    fund = _pick(rec.get("fundamental"), ["营收增速", "净利增速", "ROE", "毛利率", "净利率", "负债率"])
    if fund:
        lines.append(f"【基本面】{json.dumps(fund, ensure_ascii=False)}")

    fin = _pick(rec.get("financial"), ["quality_score", "评级", "five_dims", "flags", "is_forecast"])
    if fin:
        lines.append(f"【财报质地】{json.dumps(fin, ensure_ascii=False)}")

    sig = rec.get("signals") or {}
    lines.append(f"【信号】trend={_pick(sig.get('trend'), ['评级', '得分'])} "
                 f"reversal={_pick(sig.get('reversal'), ['超跌', '放量反包', '低位金叉', '底背离', '拐点标签'])} "
                 f"ob_os={_pick(sig.get('ob_os'), ['verdict', 'resonance'])}")

    pred = _pick(rec.get("prediction"), ["atr_pct", "支撑位", "压力位", "结构位", "持有期建议"])
    if pred:
        lines.append(f"【价位/持有期建议】{json.dumps(pred, ensure_ascii=False)[:900]}")

    ff = _pick(rec.get("fundflow"), ["今日主力净流入", "今日主力净占比", "近5日主力合计",
                                     "主力连续净流入天数", "口径日期"])
    if ff:
        lines.append(f"【资金流(注意口径日期可能陈旧)】{json.dumps(ff, ensure_ascii=False)}")

    chip = _pick(rec.get("chip"), ["获利比例", "平均成本", "集中度90", "换手缺失日"])
    if chip:
        lines.append(f"【筹码】{json.dumps(chip, ensure_ascii=False)}")

    tick = _pick(rec.get("tick"), ["主买占比", "主卖占比", "净主动买量", "大单笔数"])
    if tick:
        lines.append(f"【逐笔】{json.dumps(tick, ensure_ascii=False)}")

    holder = _pick(rec.get("holder"), ["户数环比", "连续减少期数"])
    if holder:
        lines.append(f"【股东户数】{json.dumps(holder, ensure_ascii=False)}")

    fincg = rec.get("financing") or {}
    if isinstance(fincg, dict) and fincg.get("固定一问") is not None:
        lines.append(f"【供给面固定一问(转债/定增/解禁)】{json.dumps(fincg.get('固定一问'), ensure_ascii=False)}")

    lhb = _pick(rec.get("lhb_veto"), ["triggered", "reason", "direction", "net_buy_ratio"])
    if lhb:
        lines.append(f"【龙虎榜否决】{json.dumps(lhb, ensure_ascii=False)}")

    council = rec.get("council") or {}
    if isinstance(council, dict):
        lines.append(f"【系统合议(加工分,须存疑对账)】{json.dumps(_pick(council.get('default'), ['综合方向', '综合分', '是否冲突', '冲突说明']), ensure_ascii=False)}")

    # 情绪(加工分,须用原始消息校验)
    sent = facts.sentiment or {}
    sblk = sent.get("sentiment") if isinstance(sent, dict) else None
    if sblk:
        lines.append(f"【情绪(加工分)】{json.dumps(_pick(sblk, ['利好数', '利空数', '样本数', '净情绪分', '质量', '覆盖率']), ensure_ascii=False)}")

    # 原始消息面(防未来已过滤;只给标题/来源/时间 + ai 摘要,控体量)
    if facts.news:
        lines.append("【原始消息面(公告>媒体>研报>舆情;交叉验证用)】")
        for it in facts.news[:news_summary_limit]:
            if not isinstance(it, dict):
                continue
            title = (it.get("title") or "")[:60]
            src = it.get("source") or ""
            t = it.get("time") or ""
            ai = it.get("ai")
            ai_s = ""
            if isinstance(ai, dict):
                ai_s = json.dumps(_pick(ai, ["影响方向", "影响强度", "与本股关系", "摘要"]), ensure_ascii=False)
            lines.append(f"  - [{t} {src}] {title} {ai_s}")

    # 大盘 β 背景
    mf = facts.market_forecast or {}
    if isinstance(mf, dict):
        lines.append(f"【大盘预测 β 背景】{json.dumps(_pick(mf, ['选股用β基准', '分歧标记', 'breadth_snapshot']), ensure_ascii=False)[:700]}")

    return "\n".join(lines)
