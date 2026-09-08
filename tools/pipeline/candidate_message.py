"""候选池消息面三段式:纯数据初筛 → 消息面精选 → 评价回灌打分(项目根本特色:把「消息面」
前移进每日选股流水的固定节点,并**真正回灌进用得上它的策略、参与候选集重排**)。

权威设计:docs/计划/2026-09-08_消息面三段式回灌打分_重设计.md(已用户批准)。本节点**取代**
旧「独立确认层」(commit 06e181b)的定位——旧版消息面只做旁注、不改排序;本版把消息面分
**回灌到「用消息面的策略」→ 在候选集内重排 → 得完整分**。

三段式(与设计逐段对应)
------------------
阶段1 · 纯数据初筛(全A,无消息面):全A 多策略/合议在全A票上跑完出榜(run_screen_all 里,
    本节点之前);本节点只**读**各策略落盘 view,`build_candidate_pool` 取「各策略各 top-K(默认8,
    范围5–10)∪,硬上限 ≤ 策略数×10」组候选集。此阶段完全不触网、不跑 LLM。
阶段2 · 消息面精选(仅候选集):对候选集(几十只,有界)采新闻 + news_ai + 三层情绪 + 事件
    (+资金流补缺),跑 LLM。**只候选集、绝不全A、绝不触发全量财报采集**。`enrich_fn` 预留分批
    接口(`batch_size`),agent 侧可并行提速。
阶段3 · 评价 → 分数 → 回灌重排(核心新逻辑):
    - 对每只候选召「消息面消费者专家」(情绪三层/事件驱动;资金流是数据面因子,不走此通道)合议 → 得**消息面方向 + 强度**;
    - **可解释线性映射**(方向符号 × 强度 × 斜率,再 clamp;config「消息面回灌」,可 kill-switch)
      → **消息面分**;
    - **数据面综合分** = 候选集内用**富集后 record 重算合议**(base 专家组 = 默认专家组 **剔除
      消息面专家**;方案乙防 double-count——情绪三层/事件驱动只走回灌通道、不在 base 再算一次,
      资金流是数据面因子仍留 base);
    - **完整分** = 数据面综合分 + 回灌权重 × 消息面分 → 候选集**内**据此**重排**(所见即所得,无重复计数)。
    - **关键:只重排候选集,绝不写回 record['council']、绝不改全A 5000 的主排序**(测试锁死)。

留存(硬要求)
------------------
- view「候选池消息面确认」:候选集重排后的完整分榜 + 富集报告(节点台账)。
- view「消息面评分」(设计 `__view__:消息面评分`):候选每票的 方向/强度/分/理由 + 完整分,供
  尾盘/午盘逐票分析、「用消息面打断点」的策略复用、远端展示(不重复采、不重复跑 LLM)。
- 按票 code_view「消息面确认」:个股页卡片。

有界护栏(严禁把成本扩到全A)
------------------
- 候选集 = 各策略 top-K(≤策略数×10)∪ 自选池,去重;规模数十只。
- 采集/富集/LLM **只对候选集**;绝不全A采新闻、绝不触发全量财报采集、绝不起 detached 长进程。
- 全程 `_safe` 隔离——任一步失败降级不中止每日闭环。防未来函数由被复用采集层各自 as-of 锚定保证。

依赖方向:pipeline 编排层。读 store 视图 + 复用 collectors/analysis 采集与合议,**不 import
tools.run**(富集/组装步以惰性 import + 可注入桩形式调用)。⚠️ 非投资建议。
"""
from __future__ import annotations

import logging

from tools.analysis import council
from tools.config import stock_pool
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store

logger = logging.getLogger("pipeline.candidate_message")

# 各策略落盘的 view 名(镜像 tools.run._screener_view 的 value;新增策略时两处同步)。
STRATEGY_VIEWS: list[str] = [
    "放量后缩量回踩", "动量组合", "半导体多因子", "最大范围选股", "量价放量",
    "最强选股", "反转低换手组合", "指标条件化状态排序", "扣非质量",
]
COUNCIL_VIEW = "策略0合议"  # 策略0(合议)也是一路「策略」,同 top-K 口径并入候选。
# 「消息面消费者」——声明"用消息面"的专家(情绪三层/事件驱动;资金流是数据面因子已移出消息面通道,
# 仍留默认专家组走 base;可扩,改 config「消息面回灌.消费者专家」)。前向评测证实旧含资金流会重复计数放大噪声。
MSG_EXPERTS: list[str] = ["情绪三层", "事件驱动"]

_CFG = THRESHOLDS.get("消息面回灌", {}) or {}


def _cfg() -> dict:
    """读「消息面回灌」config(单一真源;测试可 monkeypatch THRESHOLDS 后自取)。"""
    return THRESHOLDS.get("消息面回灌", {}) or {}


# ————————————————————————————————————————————————
# 小工具(本地实现,不 import tools.run 以免循环依赖)
# ————————————————————————————————————————————————
def _dedup(seq: list[str]) -> list[str]:
    """去重保序。"""
    s: set[str] = set()
    return [c for c in seq if not (c in s or s.add(c))]


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


def resolve_base_experts(msg_experts: list[str], cfg: dict | None = None) -> list[str]:
    """算「数据面综合分」重算合议用的 base 专家组(所见即所得,方案乙防 double-count)。

    方案乙(根治重复计数):开关「base剔除消息面专家」开(默认 True)→ base 专家组 =
    默认专家组 − 消息面专家(情绪三层/事件驱动只走回灌通道,不在 base 里再算一次;**资金流是
    数据面因子,保留 base**)。开关关 → 退回完整默认专家组(方案甲行为,可逆回退)。
    与 rescore_pool 里的 consumer_experts 单一真源一致(此处集中计算,避免口径漂移)。
    """
    c = cfg if cfg is not None else _cfg()
    default = list(THRESHOLDS["合议"]["默认专家组"])
    if c.get("base剔除消息面专家", True):
        excl = set(msg_experts or [])
        return [e for e in default if e not in excl]
    return default


def _picks_from_view(view: dict | None) -> list[str]:
    """从单个 screener view 抽出选出票 code(兼容 入选清单/top/排行 三种落法)。

    与 tools.run._picks_from_view 同口径(此处本地实现避免循环 import)。view 为 None/字段缺失
    → 返回空(优雅降级)。
    """
    if not isinstance(view, dict):
        return []
    items = view.get("入选清单") or view.get("top")
    if items:
        return [x["code"] for x in items if isinstance(x, dict) and x.get("code")]
    rank = view.get("排行")
    if isinstance(rank, dict):
        codes: list[str] = []
        for lst in rank.values():
            if not isinstance(lst, list):
                continue
            for x in lst:
                code = x.get("code") if isinstance(x, dict) else x
                if isinstance(code, str) and code:
                    codes.append(code)
        return _dedup(codes)
    return []


def _get_view(name: str, as_of: str | None):
    """读 view,缺失(未跑该策略/无当日产物)→ None(降级,不中止)。"""
    try:
        return store.get_view(name, date=as_of or "latest")
    except FileNotFoundError:
        return None


def _resolve_top_k(top_k: int | None) -> int:
    """把 top-K clamp 到 config「top_k范围」(默认 [5,10]);None → 取 config「top_k」默认。"""
    c = _cfg()
    if top_k is None:
        top_k = int(c.get("top_k", 8))
    rng = c.get("top_k范围", [5, 10]) or [5, 10]
    lo, hi = int(rng[0]), int(rng[1])
    return int(_clamp(top_k, lo, hi))


# ————————————————————————————————————————————————
# 阶段1 · 候选池抽取:各策略各 top-K ∪,硬上限 ≤ 策略数×10(纯数据,不触网)
# ————————————————————————————————————————————————
def build_candidate_pool(as_of: str | None = None, *, top_k: int | None = None,
                         include_watch: bool = True,
                         strategy_views: list[str] | None = None,
                         hard_cap: int | None = None) -> dict:
    """算候选池(设计阶段1):各策略(含合议策略0)各取 top-K ∪,去重保序,硬上限 ≤ 策略数×10。

    与旧口径(合议Top30 ∪ 各策略前5)的差异:改为**各策略统一 top-K**(默认8,clamp 到 [5,10]),
    并加**硬上限 = 策略数 × config「候选硬上限倍数」(默认10)**——控 LLM 预算不失控。
    自选池(∪)不受硬上限约束(运营/复盘关注票必进),但计入返回台账。

    Args:
        as_of: 目标日期(None → store latest)。
        top_k: 各策略取前 K(None → config 默认8;越界 clamp 到 [5,10])。
        include_watch: 是否并入自选池(stock_pool.get_codes)。
        strategy_views: 参与的策略 view 名(默认 [合议] + STRATEGY_VIEWS)。
        hard_cap: 策略侧候选硬上限(None → 策略数 × config「候选硬上限倍数」)。

    Returns:
        {"as_of","pool":[code...],"provenance":{code:[来源...]},"counts":{来源:命中数},
         "top_k","策略数","硬上限","上限命中":bool}。缺某 view → 该来源贡献 0(降级,不报错)。
    """
    k = _resolve_top_k(top_k)
    strategies = [COUNCIL_VIEW] + list(strategy_views if strategy_views is not None
                                       else STRATEGY_VIEWS)
    cap_mult = int(_cfg().get("候选硬上限倍数", 10))
    cap = hard_cap if hard_cap is not None else len(strategies) * cap_mult

    provenance: dict[str, list[str]] = {}
    counts: dict[str, int] = {}

    def _add(code: str, source: str) -> None:
        provenance.setdefault(code, [])
        if source not in provenance[code]:
            provenance[code].append(source)

    ordered: list[str] = []
    for vname in strategies:
        v = _get_view(vname, as_of)
        picks = _picks_from_view(v)[:k] if v else []
        counts[vname] = len(picks)
        for c in picks:
            ordered.append(c)
            _add(c, vname)

    # 策略侧去重 + 硬上限(控 LLM 预算;超出按并入序截断,合议/靠前策略优先保留)。
    strat_pool = _dedup(ordered)
    上限命中 = len(strat_pool) > cap
    if 上限命中:
        # 截断前把被砍票的 provenance 清掉(它们不再进候选,别留脏来源)。
        kept = set(strat_pool[:cap])
        for c in strat_pool[cap:]:
            provenance.pop(c, None)
        strat_pool = strat_pool[:cap]
        _ = kept

    # 自选池(∪,永远进候选;不受硬上限约束——运营关注票不因未入策略榜/超限而缺消息面)。
    pool = list(strat_pool)
    if include_watch:
        watch = [c for c in (stock_pool.get_codes() or []) if c]
        counts["自选池"] = len(watch)
        for c in watch:
            if c not in provenance:
                pool.append(c)
            _add(c, "自选池")
        pool = _dedup(pool)

    return {"as_of": as_of, "pool": pool, "provenance": provenance, "counts": counts,
            "top_k": k, "策略数": len(strategies), "硬上限": cap, "上限命中": 上限命中}


# ————————————————————————————————————————————————
# 阶段2 · 消息面精选(有界:只对候选池采 新闻 → news_ai → 三层情绪 → 事件 [→资金流])
# ————————————————————————————————————————————————
def _default_enrich(pool: list[str], as_of: str, *, no_llm: bool = False,
                    ensure_fundflow: bool = True, batch_size: int | None = None) -> dict:
    """默认富集实现(惰性 import tools.run 的采集步;只对候选池,有界)。

    步骤(全 `_safe` 隔离,任一失败降级不中止):
      新闻(collect_message)→ 三层情绪+news_ai(run_sentiment;内部 LLM 未配置则自动跳过)
      → 事件精数值(run_events)→ 资金流补缺(可选,供资金流专家发声)。
    **不含**财报三大表/年报采集(那是全A主流水职责;本节点严守消息面边界、不触发财报采集)。
    no_llm=True:跳过 run_sentiment(情绪三层专家将弃权),仅采新闻/事件/资金流数值面。

    分批(batch_size):>0 时把候选切成若干批**顺序**跑(压缩单批规模、便于观察/限流);agent 侧
    可把各批**并行**调本函数(批间无共享状态)——这里给出串行安全实现 + 并行接口注释,不在进程内
    起线程(守「不起 detached 长进程」纪律)。None/≤0 → 一次跑完。
    """
    from tools import run  # 惰性 import,避免模块顶层循环依赖

    if batch_size is None:
        batch_size = int(_cfg().get("批大小", 0) or 0)

    def _one_batch(batch: list[str]) -> dict:
        run._safe("候选池新闻采集", lambda: run.collect_message(batch))
        if no_llm:
            logger.info("候选池消息面精选:no_llm=True,跳过三层情绪(情绪专家将弃权)")
        else:
            run._safe("候选池三层情绪+news_ai", lambda: run.run_sentiment(batch))
        run._safe("候选池事件精数值", lambda: run.run_events(batch, as_of))
        nd = 0
        if ensure_fundflow:
            from tools.collectors import fundflow as ff
            need = [c for c in batch if not run._load_ok(ff.load_fundflow, c)]
            if need:
                run._safe("候选池资金流补缺", lambda: ff.fetch_fundflow(need))
            nd = len(need)
        return {"fundflow_need": nd}

    done: dict = {"news": True, "sentiment": not no_llm, "events": True,
                  "fundflow_need": 0, "批数": 1, "批大小": batch_size}
    if batch_size and batch_size > 0 and len(pool) > batch_size:
        batches = [pool[i:i + batch_size] for i in range(0, len(pool), batch_size)]
        done["批数"] = len(batches)
        for b in batches:
            r = _one_batch(b)
            done["fundflow_need"] += r["fundflow_need"]
    else:
        r = _one_batch(pool)
        done["fundflow_need"] += r["fundflow_need"]
    return done


def _default_serialize(pool: list[str], as_of: str) -> None:
    """把候选池富集后的数据组装进 record(供阶段3重算合议读取)。惰性 import。"""
    from tools.analysis import serialize
    serialize.serialize_all(as_of=as_of, codes=pool)


# ————————————————————————————————————————————————
# 阶段3 · 评价 → 分数(可解释线性映射)→ 回灌重排
# ————————————————————————————————————————————————
def msg_score_from_evaluation(方向: str, 强度: float, *, cfg: dict | None = None) -> float:
    """LLM/消息面评价(方向 + 强度)→ 消息面分(**可解释线性映射**,防过拟合)。

    映射:消息面分 = 方向符号 × clip(强度, 0, 1) × 斜率,再 clamp 到 ±分数上限。
      · 方向符号:看多/看涨=+1,看空=−1,中性/未知=0(→ 分为0,不影响排序)。
      · kill-switch:config「消息面回灌.启用」=False → 恒返回 0.0(回灌 no-op,完整分退回纯数据基线)。
    刻意只做线性映射、不做复杂拟合——可解释、可审计、不过拟合(设计§待定2)。
    """
    c = cfg if cfg is not None else _cfg()
    if not c.get("启用", True):
        return 0.0
    sign = {"看多": 1.0, "看涨": 1.0, "看空": -1.0}.get(方向, 0.0)
    slope = float(c.get("方向强度斜率", 1.0))
    cap = float(c.get("分数上限", 1.0))
    raw = sign * _clamp(float(强度 or 0.0), 0.0, 1.0) * slope
    return round(_clamp(raw, -cap, cap), 4)


def _score_one(code: str, record: dict, provenance: dict, *,
               msg_experts: list[str], consumer_experts: list[str],
               reflow_weight: float, cfg: dict) -> dict:
    """对一只候选:算 消息面评价 → 消息面分 → 数据面综合分 → 完整分(不落 record['council'])。

    - 消息面评价:council.convene(msg_experts, record) → 综合方向 + 综合分(强度=|综合分|,∈[0,1])。
    - 消息面分:msg_score_from_evaluation(方向, 强度)(线性映射 + kill-switch)。
    - 数据面综合分:council.convene(base 专家组=默认专家组**剔除消息面专家**, record).综合分——
      候选集内用**富集后 record 重算合议**;消息面专家(情绪三层/事件驱动)只走回灌通道、不在 base
      重复计数(方案乙防 double-count);资金流等数据面因子仍在 base。
    - 完整分 = 数据面综合分 + 回灌权重 × 消息面分。
    仅内存计算,**从不写 record['council']**(全A 主排序不受影响的根本保证)。
    """
    msg = council.convene(list(msg_experts), record)
    msg_dir = msg.get("综合方向", "中性")
    强度 = min(abs(float(msg.get("综合分", 0.0) or 0.0)), 1.0)
    消息面分 = msg_score_from_evaluation(msg_dir, 强度, cfg=cfg)

    归因 = msg.get("归因", [])
    发声 = [a["专家"] for a in 归因 if not a.get("弃权") and a.get("置信度", 0) > 0]
    弃权 = [a["专家"] for a in 归因 if a.get("弃权") or a.get("置信度", 0) <= 0]
    依据 = [f"{a['专家']}:{'·'.join(a.get('依据') or []) or a['方向']}" for a in 归因
            if not a.get("弃权") and a.get("置信度", 0) > 0]

    # 数据面综合分:候选集内重算合议(base 专家组=默认组剔除消息面专家);消息面专家不在此、只走回灌。
    full = council.convene(list(consumer_experts), record)
    数据面综合分 = float(full.get("综合分", 0.0) or 0.0)
    完整分 = round(数据面综合分 + reflow_weight * 消息面分, 4)

    name = ((record or {}).get("meta") or {}).get("name") or code
    return {
        "code": code, "name": name,
        "消息面方向": msg_dir,
        "消息面强度": round(强度, 4),
        "消息面分": 消息面分,
        "数据面综合分": round(数据面综合分, 4),
        "完整分": 完整分,
        "发声专家": 发声, "弃权专家": 弃权, "全弃权": not 发声,
        "是否冲突": bool(msg.get("是否冲突", False)),
        "理由": 依据,
        "候选来源": provenance.get(code, []),
    }


def rescore_pool(pool: list[str], provenance: dict, *, load_record=None,
                 msg_experts: list[str] | None = None,
                 consumer_experts: list[str] | None = None,
                 cfg: dict | None = None) -> list[dict]:
    """阶段3 主体:对候选池逐票算完整分,**候选集内按完整分重排**。缺 record 的票跳过。

    可注入 load_record / msg_experts / consumer_experts / cfg 便于测试。**不写 record['council']**。
    """
    if load_record is None:
        from tools.analysis import serialize
        load_record = serialize.load_record
    c = cfg if cfg is not None else _cfg()
    msg_experts = msg_experts or c.get("消费者专家") or MSG_EXPERTS
    # base(数据面综合分)专家组 = 默认专家组 − 消息面专家(方案乙:消息面专家只走回灌通道,不在
    # base 里重复计数;资金流是数据面因子仍留 base)。开关关 → 退回完整默认专家组(方案甲行为)。
    if consumer_experts is None:
        consumer_experts = resolve_base_experts(msg_experts, c)
    reflow_weight = float(c.get("回灌权重", 0.5))

    out: list[dict] = []
    for code in pool:
        try:
            rec = load_record(code)
        except FileNotFoundError:
            continue
        try:
            out.append(_score_one(code, rec, provenance, msg_experts=msg_experts,
                                   consumer_experts=consumer_experts,
                                   reflow_weight=reflow_weight, cfg=c))
        except Exception as e:  # noqa: BLE001
            logger.warning("候选池消息面回灌打分 %s 失败(降级跳过):%s", code, str(e)[:120])
    # 候选集内重排:完整分降序(同分时消息面看多在前,便于选股一眼看回灌后的次序)。
    _dir_rank = {"看多": 2, "看涨": 2, "中性": 1, "看空": 0}
    out.sort(key=lambda x: (x["完整分"], _dir_rank.get(x["消息面方向"], 1),
                            x["消息面分"]), reverse=True)
    for i, x in enumerate(out, 1):
        x["候选排名"] = i
    return out


# ————————————————————————————————————————————————
# 节点主入口:算候选池 → 富集 → 组装 → 回灌打分重排 → 落 view(留存)
# ————————————————————————————————————————————————
def run_candidate_message_enrich(as_of: str | None = None, *, top_k: int | None = None,
                                 no_llm: bool = False, enrich_fn=None,
                                 serialize_fn=None, load_record=None) -> dict:
    """候选池消息面三段式节点主入口(每日流水固定节点;有界、**不改全A主排序**)。

    enrich_fn/serialize_fn/load_record 可注入(测试桩);默认走 _default_enrich/_default_serialize/
    serialize.load_record。返回节点报告 dict(供台账留痕 + 调用方核验)。
    """
    if as_of is None:
        as_of = store.active_date() or store._today()
    store.set_active_date(as_of)
    enrich_fn = enrich_fn or _default_enrich
    serialize_fn = serialize_fn or _default_serialize

    built = build_candidate_pool(as_of, top_k=top_k)
    pool = built["pool"]
    logger.info("消息面三段式节点开始(日期 %s):候选池 %d 只(各策略top%d∪自选,策略数%d,硬上限%d,上限命中%s;来源计数 %s)",
                as_of, len(pool), built["top_k"], built["策略数"], built["硬上限"],
                built["上限命中"], built["counts"])
    if not pool:
        logger.warning("候选池为空(无策略/合议 view?),节点降级空跑")
        empty = {"as_of": as_of, "节点": "候选池消息面三段式·回灌打分", "候选池规模": 0,
                 "消息面消费者专家": MSG_EXPERTS, "重排": [], "统计": {}, "来源计数": built["counts"],
                 "口径": "候选池为空(缺策略/合议 view),降级"}
        store.put_view("候选池消息面确认", empty)
        store.put_view("消息面评分", empty)
        return empty

    # 阶段2 · 消息面精选(有界,只对候选池)
    enrich_report = enrich_fn(pool, as_of, no_llm=no_llm)
    # 组装 record(让消息面专家有 sentiment/events/fundflow 可读 → 阶段3 发声)
    serialize_fn(pool, as_of)
    # 阶段3 · 评价 → 分数 → 回灌重排(候选集内)
    scored = rescore_pool(pool, built["provenance"], load_record=load_record)

    stat = {"看多": 0, "看空": 0, "中性": 0, "全弃权": 0}
    for x in scored:
        if x["全弃权"]:
            stat["全弃权"] += 1
        stat[x["消息面方向"]] = stat.get(x["消息面方向"], 0) + 1
    发声数 = sum(1 for x in scored if not x["全弃权"])
    c = _cfg()

    view = {
        "as_of": as_of,
        "节点": "候选池消息面三段式·回灌打分",
        "候选池规模": len(pool),
        "消息面消费者专家": c.get("消费者专家") or MSG_EXPERTS,
        "口径": ("阶段1各策略top-K(≤策略数×10)∪自选组候选(纯数据);阶段2只对候选采新闻+news_ai+"
                 "三层情绪+事件+资金流(有界,绝不全A);阶段3把消息面评价可解释线性映射成分,回灌到"
                 "候选集重算合议→**候选集内按完整分重排**。**不写 record['council']、不改全A主排序**。"
                 "纯数据·非投资建议。"),
        "回灌参数": {"启用": c.get("启用", True), "回灌权重": c.get("回灌权重", 0.5),
                     "方向强度斜率": c.get("方向强度斜率", 1.0), "分数上限": c.get("分数上限", 1.0),
                     "top_k": built["top_k"], "硬上限": built["硬上限"]},
        "统计": {**stat, "有发声": 发声数},
        "来源计数": built["counts"],
        "上限命中": built["上限命中"],
        "富集报告": enrich_report,
        "重排": scored,
        "防未来函数": "复用采集/合议层各自 as_of 锚定,本节点不放宽口径",
    }
    p = store.put_view("候选池消息面确认", view, date=as_of)
    # 留存视图「消息面评分」(设计 __view__:消息面评分):候选每票 方向/强度/分/理由 + 完整分,供复用/展示。
    scoring_view = {
        "as_of": as_of,
        "视图": "消息面评分",
        "说明": ("候选集每票的消息面评价(方向/强度)+ 线性映射消息面分 + 数据面综合分 + 回灌后完整分,"
                 "按完整分重排。供尾盘/午盘逐票分析、断点策略复用、远端展示(不重复采、不重复跑 LLM)。"),
        "候选池规模": len(pool),
        "回灌参数": view["回灌参数"],
        "评分": [{"code": x["code"], "name": x["name"], "候选排名": x["候选排名"],
                  "消息面方向": x["消息面方向"], "消息面强度": x["消息面强度"],
                  "消息面分": x["消息面分"], "数据面综合分": x["数据面综合分"],
                  "完整分": x["完整分"], "理由": x["理由"], "候选来源": x["候选来源"],
                  "全弃权": x["全弃权"]} for x in scored],
    }
    store.put_view("消息面评分", scoring_view, date=as_of)
    # 按票 code_view「消息面确认」(供个股页卡片)
    for x in scored:
        try:
            store.put_code_view("消息面确认", x["code"], x, date=as_of)
        except Exception:  # noqa: BLE001
            pass
    logger.info("消息面三段式节点完成 → %s;候选 %d,回灌重排发声 %d(看多%d/看空%d/中性%d/全弃权%d)",
                p, len(pool), 发声数, stat["看多"], stat["看空"], stat["中性"], stat["全弃权"])
    return view


def _main(argv: list[str] | None = None) -> int:
    """CLI:python -m tools.pipeline.candidate_message [--date YYYY-MM-DD] [--no-llm]
    [--top-k K]。"""
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    as_of = None
    no_llm = "--no-llm" in argv
    top_k = None
    if "--date" in argv:
        i = argv.index("--date")
        if i + 1 < len(argv):
            as_of = argv[i + 1]
    if "--top-k" in argv:
        i = argv.index("--top-k")
        if i + 1 < len(argv) and argv[i + 1].isdigit():
            top_k = int(argv[i + 1])
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    rep = run_candidate_message_enrich(as_of, top_k=top_k, no_llm=no_llm)
    logger.info("完成:候选 %d,统计 %s", rep.get("候选池规模", 0), rep.get("统计"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
