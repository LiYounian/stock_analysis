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
from tools.contracts.record import ENUMS as _ENUMS

logger = logging.getLogger("analysis.serialize")

_OUT_DIR = settings.PROJECT_ROOT / "data" / "analysis"

# ————————————————————————————————————————————————
# 口径日期与新鲜度:「会过期的块必须自证它是哪天的口径」
# ————————————————————————————————————————————————
# 为什么存在这一层(2026-09-03 实证的两个高危静默失真,别在重写时删掉):
#   ① fundflow 块无任何日期字段。源采集失败时 store.get_raw 黑盒回退到旧分区,13 天前的
#      缓存被原样写成「今日主力净流入」——实测某票如此产出的方向与当日真实值**符号相反**,
#      直接误导决策,而下游从记录里**看不出**这是旧数据。
#   ② valuation/fundamental 的 报告期 可能整体滞后一个报告期(半年报已披露却仍是一季报),
#      PE_TTM/净利率量级全变;而 provenance 只记 {fundamental: true} 布尔值,不记口径日期
#      → 滞后完全静默。
# 结论(本层不变的三条):**保留数据 + 标明它是哪天的 + 标明是陈旧**。
#   不静默沿用(那是①的病);也不一律清空(清空会丢掉「有旧数据可参考」这个信息)。
#
# 三态**不另造**:直接取 contracts.record.ENUMS["新鲜度"] —— 它是全项目单一真源,
# tools/analysis/event.py 的 FRESH/STALE/NODATA 也声明与它对齐(见 event.py:37)。
FRESH, STALE, NODATA = _ENUMS["新鲜度"]

VINTAGE_DATE = "口径日期"        # 该块数据实际来自哪一天(序列块=最后一根 bar 日;分区块=命中的分区日)
FRESHNESS = "新鲜度"             # ∈ FRESH/STALE/NODATA
VINTAGE_NOTE = "口径提示"        # 仅在需要提醒时出现的人读/LLM 可读说明

# provenance 里除数据外的**元信息**键:判「该维实际有无数据」时必须排除,
# 否则「只剩源不可得标记的空块」会被 bool() 误报成 True(= 明确撒谎说有数据)。
_META_KEYS = frozenset({
    VINTAGE_DATE, FRESHNESS, VINTAGE_NOTE,
    "as_of", "采集日期", "锁定日期", "源状态", "降级", "剔除", "源不可得", "口径说明", "口径注解",
})

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


# ---------- 口径日期 / 新鲜度 / 诚实的 provenance ----------
def _d10(x) -> str | None:
    """任意日期表示 → 'YYYY-MM-DD';不可解析 → None。"""
    if x is None:
        return None
    s = str(x)[:10]
    return s if len(s) == 10 and s[4] == "-" and s[7] == "-" else None


def _digits(x) -> str:
    """报告期归一成纯数字串('2026-06-30'/'20260630' → '20260630');供跨源可比。"""
    return "".join(ch for ch in str(x or "") if ch.isdigit())


def _last_bar_date(df) -> str | None:
    """序列型缓存的口径日期 = **最后一根 bar 的日期**(比分区日更准:它就是这个数是哪天的)。

    这是问题①的正解——`今日主力净流入` 取的是 df 最后一行,那这个「今日」到底是哪天,
    只有最后一根 bar 的 date 说得清;分区日只说明「哪天采的」,采失败回退旧分区时两者都会骗人,
    但最后一根 bar 的日期**永远诚实**(它是数据自带的)。
    """
    try:
        if df is None or len(df) == 0 or "date" not in getattr(df, "columns", ()):
            return None
        return _d10(df["date"].iloc[-1])
    except Exception:
        return None


def _raw_vintage(kind: str, code: str, date: str | None) -> str | None:
    """该票该 raw 实际命中的分区日(= 该源的口径日期)。判不出 → None。

    复用 store 已有的 date-pin 解析(同 tools/run.py:_resolved_before),**不另造新鲜度判据**。
    ⚠️ `date` 必须与真正取载荷的那个 loader 的日期语义**一致**,否则盖的戳会撒谎:
       loader 走 `get_raw(..., "latest")`(全局最新、可能晚于 as_of)时这里也必须传 "latest"。
    """
    from tools.store import repo as store
    try:
        _payload, resolved, _fetched = store.get_raw_resolved(kind, code, date=date)
    except Exception:
        return None                       # 无该 raw / 未走 store(被 mock)/ 解析失败 → 判不出
    return _d10(resolved)


def _classify(vintage: str | None, as_of: str, *, what: str = "本块数据") -> tuple[str, str | None]:
    """(口径日期, as_of) → (新鲜度, 口径提示)。

    规则(与 event._classify_freshness / run._dim_status 同一套语义,不新增状态):
      - 口径日期 == as_of        → 新鲜;
      - 口径日期 <  as_of        → **陈旧**(数据保留,但显式标明它是哪天的);
      - 口径日期 >  as_of        → 保守当新鲜(同 event.py),但**显式提示疑未来函数**;
      - 口径日期判不出(None)    → 保守当新鲜 + 提示判不出(同 event._resolve_layer 的兼容路径)。
        注意「判不出」≠「无数据」:NODATA 只用于该维**确实没有可用数据**(见 _provenance_dim)。
    """
    a = _d10(as_of)
    if vintage is None:
        return FRESH, f"{what}口径日期判不出(未走 store 日期分区)"
    if a is None or vintage == a:
        return FRESH, None
    if vintage > a:
        return FRESH, f"{what}口径日期 {vintage} 晚于 as_of {a}(疑未来函数,下游谨慎使用)"
    return STALE, f"{what}为 {vintage} 口径(as_of={a}),此后的变化未反映"


def _stamp(block, vintage: str | None, as_of: str, *,
           what: str = "本块数据", extra_note: str | None = None):
    """给块盖 口径日期 + 新鲜度 (+ 口径提示)。**原地改并返回同一对象**。

    block 非 dict(None / 列表 / 标量)→ 原样返回,不盖戳(此时口径信息只进 provenance.口径)。
    **陈旧时绝不清空数据**:清空等于丢掉「有旧数据可参考」;正确做法是数据留着 + 说清是哪天的。
    """
    if not isinstance(block, dict):
        return block
    fresh, note = _classify(vintage, as_of, what=what)
    if extra_note:                       # 例:报告期整体滞后一个报告期(比"晚几天"严重得多)
        fresh, note = STALE, ";".join(x for x in (extra_note, note) if x)
    block[VINTAGE_DATE] = vintage
    block[FRESHNESS] = fresh
    if note:
        block[VINTAGE_NOTE] = note
    return block


def _has_data(block) -> bool:
    """该维**是否真的拿到了可用数据**(provenance 的诚实判据,替代裸 `bool(块)`)。

    为什么不能用 `bool(块)`:块里只要有任何键就为真——包括「只剩 `源不可得`/`降级` 标记的空块」。
    那种块 `bool()` 为 True,provenance 就会**明确撒谎说有数据**。故:dict 必须至少有一个
    **非元信息键**且其值非空;list/其他按非空判。元信息键清单见 _META_KEYS。
    """
    if block is None or block is False:
        return False
    if isinstance(block, dict):
        return any(k not in _META_KEYS and v is not None and v != [] and v != {}
                   for k, v in block.items())
    if isinstance(block, (list, tuple, str)):
        return len(block) > 0
    return bool(block)


def _provenance_dim(block, vintage: str | None, as_of: str) -> dict:
    """单维 provenance 口径条目 {口径日期, 新鲜度}。

    该维**无可用数据** → 新鲜度 = 无数据(与「有旧数据但陈旧」严格区分:前者什么都没有、
    后者有值只是旧的,下游的处置完全不同)。
    """
    if not _has_data(block):
        return {VINTAGE_DATE: None, FRESHNESS: NODATA}
    fresh, _note = _classify(vintage, as_of)
    return {VINTAGE_DATE: vintage, FRESHNESS: fresh}


def _period_lag_note(block_period, disclosed_period) -> str | None:
    """基本面/估值的 报告期 是否整体滞后于**已披露**最新报告期(问题②)。

    `financial` 块由 analysis.financial 按**披露日锚定**产出(PIT 正确),是可信的参照系;
    `fundamental` 缓存的 报告期 若比它旧,说明整块基本面/估值滞后一个报告期——影响远大于
    「晚几天的价格」:实测某票由一季报换成半年报后 PE_TTM +144%、净利率由正翻负。
    """
    a, b = _digits(block_period), _digits(disclosed_period)
    if len(a) == 8 and len(b) == 8 and b > a:
        return f"报告期 {a} 落后于已披露最新报告期 {b}(基本面/估值滞后整个报告期)"
    return None


def _industry_asof(code: str, as_of: str) -> str | None:
    """as_of「当时」所属行业(证监会口径,collectors.industry_history)。

    去回测前视偏差:历史归因/回测按「当时」而非「现在」的行业取数。
    无历史记录/该时点前无生效记录 → None(调用方回退现状,不静默失真)。
    """
    try:
        from tools.collectors import industry_history as ih
        return _safe(lambda: ih.industry_at(code, as_of))
    except Exception:
        return None


def build_record(code: str, as_of: str) -> dict:
    """组装单票结构化记录。缺失的数据块降级为 None / 空,不抛错。"""
    s = stock_pool.get(code)
    kdf = _safe(lambda: market.load_kline_recent(code))          # 加载一次,tech/predict 复用
    tech = _safe(lambda: ta.compute(kdf), {}) if kdf is not None else {}
    fund = _safe(lambda: fd.load_fundamental(code), {}) or {}
    anns = _safe(lambda: an.load_announcements(code), []) or []

    # 资金流摘要(P3.1 已加;不存在则 None)
    # 口径日期取**最后一根 bar 的日期**而非分区日:`今日主力净流入` 取的就是最后一行,
    # 源采集失败时 store 会黑盒回退到旧分区(实测回退过 13 天),只有 bar 自带的日期不会骗人。
    flow, flow_vintage = None, None
    try:
        from tools.collectors import fundflow as ff
        ffdf = _safe(lambda: ff.load_fundflow(code))
        # 空 df 不再喂给 summarize:它会返回一份「全 None + 连续天数 0」的空壳,
        # 那种壳过去让 provenance.fundflow 报 True(撒谎说有资金流)。无数据就诚实为 None。
        if ffdf is not None and len(ffdf) > 0:
            flow = _safe(lambda: ff.summarize(ffdf))
            flow_vintage = _last_bar_date(ffdf)
    except Exception:
        flow, flow_vintage = None, None

    # —— 借鉴 a-stock-data 新增采集的摘要块(缺采集→None,多因子该维降级)——
    # 三块统一按 as_of point-in-time 取数,去历史重建(回填 panel/多因子回测)前视偏差:
    #   · chip:纯本地推演,**天然可按 as_of 重算** → 只用 ≤as_of 的 K线 bar(真 point-in-time)。
    #   · consensus/holder:**点数据、源无历史快照** → 只能 date-pin 到 ≤as_of 的最近采集分区
    #     (get_raw_resolved,绝不返回未来分区);无 ≤as_of 分区则缺失降级(不注入今值)。
    #     ⚠ 锁死:这两块历史重建只保证「无未来函数」,**不保证重构任意 as_of 当天的原值**
    #     (分区颗粒度受实际采集频率限制)。详见各 load_* docstring。
    # 筹码分布(本地推演):获利比例/平均成本/成本区间/集中度90
    chip_block = None
    try:
        from tools.collectors import chip
        chip_block = _safe(lambda: chip.load_chip(code, as_of))
    except Exception:
        chip_block = None
    # 一致预期(前瞻):预期EPS当年/次年、预期增速、覆盖机构数
    consensus_block = None
    try:
        from tools.collectors import consensus
        consensus_block = _safe(lambda: consensus.load_consensus(code, as_of))
    except Exception:
        consensus_block = None
    # 股东户数趋势(主力吸筹):最新户数/户数环比/连续减少期数
    holder_block = None
    try:
        from tools.collectors import smart_money as sm
        holder_block = _safe(lambda: sm.summarize_holder(sm.load_holder_num(code, as_of)))
    except Exception:
        holder_block = None
    # 盘口微观结构(逐笔;collectors.tdx_l2 盘后 run ticks 归档;缺则 None 降级,前端卡片不显示)
    # date-pin 到 ≤as_of 分区(无未来函数);只读 meta 里的轻量摘要,不重载大 parquet。
    tick_block = None
    try:
        from tools.collectors import tdx_l2
        tick_block = _safe(lambda: tdx_l2.load_summary(code, date=as_of))
    except Exception:
        tick_block = None

    # 存量融资与解禁(D · 固定一问):有无存续可转债 / 在推进的定增 / 临近限售解禁。
    # 需 run.py financing 先采集(低频,30天缓存);缺采集 → None(优雅降级,不阻断)。
    # 防未来函数:load 走 ≤as_of 分区 + 逐条「披露日 ≤ as_of」闸门(见 collectors.equity_financing)。
    # 总股本由 总市值(亿)/ 现价 反算,供转债潜在摊薄%;缺任一 → 摊薄字段 None(不猜)。
    financing_block = None
    try:
        from tools.collectors import equity_financing as efin
        _cap_yi = (fund or {}).get("总市值")
        _close = (tech.get("last") or {}).get("close") if tech else None
        _shares = (float(_cap_yi) * 1e8 / float(_close)
                   if (_cap_yi and _close) else None)
        financing_block = _safe(lambda: efin.build_financing_block(
            code, as_of=as_of, 总股本=_shares))
    except Exception:
        financing_block = None

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
    # 基本面/估值的口径日期 = `fundamental` raw 实际命中的分区日。
    # 探针日期必须传 "latest" —— 因为载荷来自 fd.load_fundamental(code),它内部就是
    # get_raw(..., "latest")(全局最新,**可能晚于 as_of**)。若这里传 as_of,探针会解析到
    # ≤as_of 的分区、和真正读到的那份不是同一天 → 盖的戳就成了假话。
    # 顺带:探针一旦报出「口径日期 > as_of」,就是在如实暴露该 loader 没有 date-pin 的未来函数风险。
    fund_vintage = _raw_vintage("fundamental", code, "latest") if fund else None

    # 财报质地块(P1):披露日锚定的最新已披露报告期轻量摘要(analysis.financial)。
    # 缺财报采集 → None(优雅降级,不阻断);行业传入供金融业红旗特判。
    from tools.analysis.financial import analyzer as fr_analyzer
    financial_block = _safe(lambda: fr_analyzer.build_financial_block(
        code, as_of=as_of, industry=(s.industry if s else None)))

    # summary 一并带上:事件专家减持性质区分(协议转让给战投 vs 二级抛售)靠公告标题/摘要文本
    # 兜底,标题外的"引入战投/业务协同/集中竞价"等语义常落在摘要里(采集缺 summary 时为 None,不影响)。
    events = [{"date": a.get("date"), "type": a.get("type"),
               "impact": a.get("impact"), "title": a.get("title"),
               "summary": a.get("summary")} for a in anns[:20]]

    # 龙虎榜「入选否决」as-of 裁决(WI-6 Phase 3 · 风控微结构轴):挂进 record 供 web 只读取用
    # (展示层不 import 分析器,§9.3 依赖方向)。缺快照/未采集/未触发 → None(优雅降级)。
    # 防未来函数:走 lhb_veto → lhb_asof(list_date < as_of 严格,盘后披露当天不可用)。
    from tools.analysis import risk_veto as _rv
    lhb_veto_block = _safe(lambda: _rv.lhb_verdict_asof(code, as_of))

    # ———— 给「会过期」的块盖口径日期 + 新鲜度(见文件头 §口径日期与新鲜度)————
    # 各块口径日期从哪来(选择依据:哪个日期能真正代表「这个数是哪天的」):
    #   · snapshot/chip:最后一根 K线 bar 日(chip 是由同一段 K线本地推演的纯派生量);
    #   · fundflow:资金流序列最后一根 bar 日(见上文);
    #   · valuation/fundamental:`fundamental` raw 命中的分区日 + 报告期滞后交叉核对;
    #   · consensus/holder/tick:各自 raw 在 ≤as_of 命中的分区日(这三块的 loader 本就 date-pin
    #     到 as_of,只是把 resolved 丢掉了 → 这里用同一套解析把它捡回来)。
    # 未盖戳的块及理由:
    #   · sentiment:**本来就自证**(采集日期/新鲜度/锁定日期三态,event.py 产出),不重复盖;
    #   · financial:披露日锚定,块内已有 报告期/报告类型/披露日,自证;
    #   · financing:块内已有 as_of/源状态/降级/剔除,自证;
    #   · lhb_veto:走 lhb_asof(list_date < as_of 严格闸门),按构造无法沿用未来/旧值;
    #   · events(公告):每条自带 date,列表无处盖块级戳 → 口径进 provenance.口径;
    #   · signals/prediction/council:纯派生量,口径 = 其输入块的口径,不另立日期(否则两处会打架);
    #   · margin(两融):当前**不进 record**(只有 collectors.margin + analysis.margin_divergence
    #     用),没有产出可被误当「今日」,故本轮不处理;若哪天挂进 record,按 consensus 同款处理。
    price_vintage = _last_bar_date(kdf)
    _stamp(snapshot, price_vintage, as_of, what="价量快照")
    _stamp(flow, flow_vintage, as_of,
           what="资金流(字段名里的「今日」= 口径日期那天,不是 as_of)")
    _stamp(chip_block, price_vintage, as_of, what="筹码(由 ≤as_of 的 K线本地推演)")
    consensus_vintage = _raw_vintage("consensus", code, as_of) if consensus_block else None
    _stamp(consensus_block, consensus_vintage, as_of, what="一致预期")
    holder_vintage = _raw_vintage("holder_num", code, as_of) if holder_block else None
    _stamp(holder_block, holder_vintage, as_of, what="股东户数")
    tick_vintage = _raw_vintage("tick_summary", code, as_of) if tick_block else None
    _stamp(tick_block, tick_vintage, as_of, what="盘口微观结构")
    # 报告期滞后交叉核对:以披露日锚定的 financial 块为参照系(问题②)
    _lag = _period_lag_note((valuation_block or {}).get("报告期"),
                            (financial_block or {}).get("报告期"))
    _stamp(valuation_block, fund_vintage, as_of, what="估值", extra_note=_lag)
    _stamp(fundamental_block, fund_vintage, as_of, what="基本面", extra_note=_lag)
    if valuation_block is not None:
        valuation_block["报告期滞后"] = bool(_lag)

    rec = {
        "schema_version": SCHEMA_VERSION,
        "meta": {"code": code, "name": s.name if s else (_code_name(code) or code),
                 "sector": s.sector if s else None, "industry": s.industry if s else None,
                 # industry_asof:as_of「当时」所属行业(collectors.industry_history);供回测/历史归因
                 # 去前视偏差用。与 industry(人工细分文本)独立,缺历史→None(回退现状,不静默失真)。
                 "industry_asof": _industry_asof(code, as_of),
                 "as_of": as_of},
        "snapshot": snapshot,
        "valuation": valuation_block,
        "fundamental": fundamental_block,
        "financial": financial_block,
        "signals": signals,
        "prediction": prediction,
        "sentiment": sentiment,
        "fundflow": flow,
        "chip": chip_block,             # 筹码分布摘要(多因子「筹码」)
        "consensus": consensus_block,   # 一致预期摘要(多因子「预期」)
        "holder": holder_block,         # 股东户数趋势摘要(多因子「主力」)
        "tick": tick_block,             # 盘口微观结构摘要(逐笔;主买占比/净主动买量/大单)
        "lhb_veto": lhb_veto_block,     # 龙虎榜入选否决 as-of 裁决(风控微结构轴;缺→None)
        "financing": financing_block,   # 存量融资与解禁固定一问(存续可转债/定增/解禁;缺→None)
        "events": events,
        "timeseries_refs": {
            "kline": f"data/raw/kline/{code}.parquet",
            "fundflow": f"data/raw/fundflow/{code}.parquet",
            "announcements": f"data/raw/announcement/{code}.json",
        },
        # provenance:**布尔按「该维实际有无可用数据」判**(不是对块做 bool()——那样只要块里
        # 有任何键、哪怕全是「源不可得」标记,也会报 True = 明确撒谎说有数据,见 _has_data),
        # 并在 `口径` 子字典里给出每个源的口径日期 + 新鲜度三态。
        # 向后兼容是硬要求:既有布尔键**类型不变**(大量代码/测试读 provenance.xxx 的 True/False),
        # 新增信息一律挂在 `口径` 子字典下,不覆盖任何老键。
        "provenance": {"tech": bool(has_tech), "fundamental": _has_data(fundamental_block),
                       "announcements": len(anns), "fundflow": _has_data(flow),
                       "chip": _has_data(chip_block), "consensus": _has_data(consensus_block),
                       "holder": _has_data(holder_block), "tick": _has_data(tick_block),
                       "financing": _has_data(financing_block),
                       "口径": {
                           "tech": _provenance_dim(snapshot, price_vintage, as_of),
                           "fundamental": _provenance_dim(fundamental_block, fund_vintage, as_of),
                           "valuation": _provenance_dim(valuation_block, fund_vintage, as_of),
                           "fundflow": _provenance_dim(flow, flow_vintage, as_of),
                           "chip": _provenance_dim(chip_block, price_vintage, as_of),
                           "consensus": _provenance_dim(consensus_block, consensus_vintage, as_of),
                           "holder": _provenance_dim(holder_block, holder_vintage, as_of),
                           "tick": _provenance_dim(tick_block, tick_vintage, as_of),
                           # 公告是列表(无处盖块级戳)→ 口径只在这里给:命中的采集分区日
                           "announcements": _provenance_dim(
                               anns, _raw_vintage("announcement", code, "latest") if anns else None,
                               as_of),
                           # 下面三块**块内已自证**,这里只做镜像,便于下游在一处读全部口径。
                           # financial/financing 用各自 raw 的分区日(= 采集日),与 consensus/holder
                           # 同一把尺子;块内的 披露日/报告期/as_of 仍是更细的业务口径,原样保留。
                           "financial": _provenance_dim(
                               financial_block,
                               _raw_vintage("financial_report", code, as_of)
                               if financial_block else None, as_of),
                           "financing": _provenance_dim(
                               financing_block,
                               _raw_vintage("equity_financing", code, as_of)
                               if financing_block else None, as_of),
                           # sentiment 有**自己的**新鲜度窗口策略(SENTIMENT_MAX_STALE_DAYS /
                           # FRESHNESS_MODE),这里原样镜像它的结论,绝不用本模块的尺子重判——
                           # 否则一个块会同时挂两个互相矛盾的新鲜度。
                           "sentiment": ({VINTAGE_DATE: _d10(sentiment.get("采集日期")),
                                          FRESHNESS: sentiment.get(FRESHNESS) or FRESH}
                                         if _has_data(sentiment)
                                         else {VINTAGE_DATE: None, FRESHNESS: NODATA}),
                       }},
    }

    # 多策略合议(F5·D7):后端预算各专家信封 + 默认组合议结果 + config(tau/权重),随记录落库。
    # 默认组专家均 record-shaped(读上面已装好的 signals/fundflow/sentiment),故 kline 可选。
    # 展示层只读本块;前端勾选按落库 config 权重重合成(不触发后端重算)。
    from tools.analysis import council
    rec["council"] = _safe(lambda: council.build_council_block(rec, kdf))
    # 同系统对账(弃权置信度标注 §3.4):合议综合方向 vs per-stock 买卖倾向 → 内部分歧标记(只标不改判)。
    # 标注关 / 任一缺失 → None(优雅降级)。附加 rec 键,契约对 null/额外键宽容。
    rec["合议对账"] = _safe(lambda: _council_bias_reconcile(rec))
    return rec


def _council_bias_reconcile(rec: dict) -> dict | None:
    """读 rec 的合议综合方向 + prediction.买卖倾向.结论,产内部分歧标记(纯读取 + 纯函数)。"""
    from tools.analysis import council
    if not bool((council._abstain_cfg()).get("标注启用", False)):
        return None
    方向 = (((rec.get("council") or {}).get("default") or {}).get("综合方向"))
    结论 = (((rec.get("prediction") or {}).get("买卖倾向") or {}).get("结论"))
    if 方向 is None or 结论 is None:
        return None
    return council.reconcile_direction(方向, 结论)


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
