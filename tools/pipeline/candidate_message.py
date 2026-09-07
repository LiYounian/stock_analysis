"""候选池消息面富集节点(项目根本特色:把「消息面」前移进每日选股流水的固定节点)。

背景 / 要解决的硬伤
------------------
全A 多策略选股(run_screen_all)在全A票上跑完出榜单,但最贵的新闻/LLM 情绪历史上只对很小的
子集采过;合议里 **情绪三层 / 事件驱动 / 资金流** 三位「消息面专家」因单票 record 缺 sentiment/
events/fundflow 数据而**集体弃权**,消息面根本没进选股。本节点把「候选池消息面富集」做成每日
流水的**固定节点**:基础策略/合议选出候选后,自动对候选池(~40–50 只,有界)采新闻 + 跑
news_ai + 三层情绪(+事件/资金流),让消息面专家对候选池**发声**。

独立确认层(先不动全A主排序,风险控制)
------------------
本节点产出的是一个**独立确认层**:只对候选池召 `情绪三层 / 事件驱动 / 资金流` 三位消息面专家
做一次「二次合议」,给每只候选一个**消息面方向 + 理由**标注,落 view「候选池消息面确认」+ 按票
code_view「消息面确认」。**不改写** record['council'](全A 合议主排序)、不回灌打分——把「消息面
是否确认基础策略的方向」作为**附加信息**呈现给选股。回灌全A主排序留给下一步(需专门评测)。

有界护栏(严禁把成本扩到全A)
------------------
- 候选池 = 合议 Top-N(默认30)∪ 各策略视图各前 K(默认5)∪ 自选池,去重;规模 ~40–50。
- 采集/富集**只对候选池**这几十只;绝不全A采新闻、绝不触发全量财报采集。
- 幂等 / skip-if-cached / 优雅降级 / 防未来函数由被复用的采集层各自保证;本节点不放宽口径。
- 全程 `_safe` 隔离——任一步失败降级不中止每日闭环。

依赖方向:pipeline 编排层。读 store 视图 + 复用 collectors/analysis 采集与合议,**不 import
tools.run**(避免与 run.py 循环依赖;富集/组装步以惰性 import + 可注入桩形式调用)。
"""
from __future__ import annotations

import logging
import os

from tools.analysis import council
from tools.config import stock_pool
from tools.store import repo as store

logger = logging.getLogger("pipeline.candidate_message")

# 各策略落盘的 view 名(镜像 tools.run._screener_view 的 value;新增策略时两处同步)。
STRATEGY_VIEWS: list[str] = [
    "放量后缩量回踩", "动量组合", "半导体多因子", "最大范围选股", "量价放量",
    "最强选股", "反转低换手组合", "指标条件化状态排序", "扣非质量",
]
COUNCIL_VIEW = "策略0合议"
# 独立确认层只召这三位「消息面/信息面」专家(其余基础因子专家不参与——本层只回答
# 「消息面是否确认」,不重算技术/多因子)。
MSG_EXPERTS: list[str] = ["情绪三层", "事件驱动", "资金流"]


# ————————————————————————————————————————————————
# 小工具(本地实现,不 import tools.run 以免循环依赖)
# ————————————————————————————————————————————————
def _dedup(seq: list[str]) -> list[str]:
    """去重保序。"""
    s: set[str] = set()
    return [c for c in seq if not (c in s or s.add(c))]


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


# ————————————————————————————————————————————————
# ① 候选池抽取(可复用函数:供本节点 + 未来其它「只对候选池」的贵活复用)
# ————————————————————————————————————————————————
def build_candidate_pool(as_of: str | None = None, *, council_top_n: int = 30,
                         per_view_top_k: int = 5, include_watch: bool = True,
                         strategy_views: list[str] | None = None) -> dict:
    """算候选池:合议 Top-N ∪ 各策略视图各前 K ∪ 自选池,去重保序(规模 ~40–50)。

    Args:
        as_of: 目标日期(None → store latest)。
        council_top_n: 合议(策略0)榜取前 N。
        per_view_top_k: 各策略视图取前 K。
        include_watch: 是否并入自选池(stock_pool.get_codes)。
        strategy_views: 参与的策略 view 名(默认 STRATEGY_VIEWS)。

    Returns:
        {"as_of", "pool":[code...], "provenance":{code:[来源...]}, "counts":{来源:命中数},
         "council_top_n","per_view_top_k"}。缺某 view → 该来源贡献 0(降级,不报错)。
    """
    views = strategy_views if strategy_views is not None else STRATEGY_VIEWS
    provenance: dict[str, list[str]] = {}
    counts: dict[str, int] = {}

    def _add(code: str, source: str) -> None:
        provenance.setdefault(code, [])
        if source not in provenance[code]:
            provenance[code].append(source)

    ordered: list[str] = []

    # 合议 Top-N(项目主排序权威源;放最前,候选池以合议为骨架)
    cview = _get_view(COUNCIL_VIEW, as_of)
    council_codes = _picks_from_view(cview)[:council_top_n] if cview else []
    counts[COUNCIL_VIEW] = len(council_codes)
    for c in council_codes:
        ordered.append(c)
        _add(c, COUNCIL_VIEW)

    # 各策略视图各前 K
    for vname in views:
        v = _get_view(vname, as_of)
        picks = _picks_from_view(v)[:per_view_top_k] if v else []
        counts[vname] = len(picks)
        for c in picks:
            ordered.append(c)
            _add(c, vname)

    # 自选池(∪,永远进候选;运营/复盘关注票不因未入策略榜而缺消息面)
    if include_watch:
        watch = [c for c in (stock_pool.get_codes() or []) if c]
        counts["自选池"] = len(watch)
        for c in watch:
            ordered.append(c)
            _add(c, "自选池")

    pool = _dedup(ordered)
    return {"as_of": as_of, "pool": pool, "provenance": provenance, "counts": counts,
            "council_top_n": council_top_n, "per_view_top_k": per_view_top_k}


# ————————————————————————————————————————————————
# ② 候选池消息面富集(有界:只对候选池采 新闻 → news_ai → 三层情绪 → 事件 [→资金流])
# ————————————————————————————————————————————————
def _default_enrich(pool: list[str], as_of: str, *, no_llm: bool = False,
                    ensure_fundflow: bool = True) -> dict:
    """默认富集实现(惰性 import tools.run 的采集步;只对候选池,有界)。

    步骤(全 `_safe` 隔离,任一失败降级不中止):
      新闻(collect_message)→ 三层情绪+news_ai(run_sentiment;内部 LLM 未配置则自动跳过)
      → 事件精数值(run_events)→ 资金流补缺(可选,供资金流专家发声)。
    **不含**财报三大表/年报采集(那是全A主流水的职责;本节点严守消息面边界、不触发财报采集)。
    no_llm=True:跳过 run_sentiment(情绪三层专家将弃权),仅采新闻/事件/资金流数值面。
    """
    from tools import run  # 惰性 import,避免模块顶层循环依赖

    done: dict = {"news": True, "sentiment": not no_llm, "events": True}
    run._safe("候选池新闻采集", lambda: run.collect_message(pool))
    if no_llm:
        logger.info("候选池消息面富集:no_llm=True,跳过三层情绪(情绪专家将弃权)")
    else:
        run._safe("候选池三层情绪+news_ai", lambda: run.run_sentiment(pool))
    run._safe("候选池事件精数值", lambda: run.run_events(pool, as_of))
    if ensure_fundflow:
        # 只补缺 fundflow 的票(供资金流专家发声);skip-if-cached，绝不全A。
        from tools.collectors import fundflow as ff
        need = [c for c in pool if not run._load_ok(ff.load_fundflow, c)]
        if need:
            run._safe("候选池资金流补缺", lambda: ff.fetch_fundflow(need))
        done["fundflow_need"] = len(need)
    return done


def _default_serialize(pool: list[str], as_of: str) -> None:
    """把候选池富集后的数据组装进 record(供确认层召集专家读取)。惰性 import。"""
    from tools.analysis import serialize
    serialize.serialize_all(as_of=as_of, codes=pool)


# ————————————————————————————————————————————————
# ③ 独立确认层:只对候选池召消息面专家,产「消息面方向 + 理由」标注(不动全A主排序)
# ————————————————————————————————————————————————
def _confirm_one(code: str, record: dict, provenance: dict) -> dict:
    """对一只候选召 MSG_EXPERTS 二次合议,产消息面确认标注。

    只用 council.convene(不落 record['council'],不改全A主排序);消息面专家全弃权 → 标 `全弃权`。
    """
    res = council.convene(MSG_EXPERTS, record)
    归因 = res.get("归因", [])
    发声 = [a["专家"] for a in 归因 if not a.get("弃权") and a.get("置信度", 0) > 0]
    弃权 = [a["专家"] for a in 归因 if a.get("弃权") or a.get("置信度", 0) <= 0]
    依据 = [f"{a['专家']}:{'·'.join(a.get('依据') or []) or a['方向']}" for a in 归因
            if not a.get("弃权") and a.get("置信度", 0) > 0]
    name = ((record or {}).get("meta") or {}).get("name") or code
    return {
        "code": code,
        "name": name,
        "消息面方向": res.get("综合方向", "中性"),
        "消息面分": res.get("综合分", 0.0),
        "发声专家": 发声,
        "弃权专家": 弃权,
        "全弃权": not 发声,
        "是否冲突": res.get("是否冲突", False),
        "依据": 依据,
        "候选来源": provenance.get(code, []),
    }


def confirm_pool(pool: list[str], provenance: dict, *,
                 load_record=None) -> list[dict]:
    """对候选池逐票产消息面确认标注;缺 record 的票跳过。可注入 load_record 便于测试。"""
    if load_record is None:
        from tools.analysis import serialize
        load_record = serialize.load_record
    out: list[dict] = []
    for code in pool:
        try:
            rec = load_record(code)
        except FileNotFoundError:
            continue
        try:
            out.append(_confirm_one(code, rec, provenance))
        except Exception as e:  # noqa: BLE001
            logger.warning("候选池消息面确认 %s 失败(降级跳过):%s", code, str(e)[:120])
    # 排序:先有发声、消息面看多在前,便于选股一眼看「消息面确认了哪些」。
    _dir_rank = {"看多": 2, "中性": 1, "看空": 0}
    out.sort(key=lambda x: (not x["全弃权"], _dir_rank.get(x["消息面方向"], 1),
                            x["消息面分"]), reverse=True)
    return out


# ————————————————————————————————————————————————
# 节点主入口:算候选池 → 富集 → 组装 → 确认层 → 落 view
# ————————————————————————————————————————————————
def run_candidate_message_enrich(as_of: str | None = None, *, council_top_n: int = 30,
                                 per_view_top_k: int = 5, no_llm: bool = False,
                                 enrich_fn=None, serialize_fn=None,
                                 load_record=None) -> dict:
    """候选池消息面富集节点主入口(每日流水固定节点;有界、不改全A主排序)。

    enrich_fn/serialize_fn/load_record 可注入(测试桩);默认走 _default_enrich/_default_serialize/
    serialize.load_record。返回节点报告 dict(供台账留痕 + 调用方核验)。
    """
    if as_of is None:
        as_of = store.active_date() or store._today()
    store.set_active_date(as_of)
    enrich_fn = enrich_fn or _default_enrich
    serialize_fn = serialize_fn or _default_serialize

    built = build_candidate_pool(as_of, council_top_n=council_top_n,
                                 per_view_top_k=per_view_top_k)
    pool = built["pool"]
    logger.info("候选池消息面富集节点开始(日期 %s):候选池 %d 只(合议Top%d∪各策略前%d∪自选;来源计数 %s)",
                as_of, len(pool), council_top_n, per_view_top_k, built["counts"])
    if not pool:
        logger.warning("候选池为空(无策略/合议 view?),节点降级空跑")
        empty = {"as_of": as_of, "节点": "候选池消息面富集·独立确认层", "候选池规模": 0,
                 "确认层专家": MSG_EXPERTS, "确认": [], "统计": {}, "来源计数": built["counts"],
                 "口径": "候选池为空(缺策略/合议 view),降级"}
        store.put_view("候选池消息面确认", empty)
        return empty

    # ② 富集(有界,只对候选池)
    enrich_report = enrich_fn(pool, as_of, no_llm=no_llm)
    # ③ 组装 record(让消息面专家有 sentiment/events/fundflow 可读)
    serialize_fn(pool, as_of)
    # ④ 独立确认层
    confirmations = confirm_pool(pool, built["provenance"], load_record=load_record)

    stat = {"看多": 0, "看空": 0, "中性": 0, "全弃权": 0}
    for x in confirmations:
        if x["全弃权"]:
            stat["全弃权"] += 1
        stat[x["消息面方向"]] = stat.get(x["消息面方向"], 0) + 1
    发声数 = sum(1 for x in confirmations if not x["全弃权"])

    view = {
        "as_of": as_of,
        "节点": "候选池消息面富集·独立确认层",
        "候选池规模": len(pool),
        "确认层专家": MSG_EXPERTS,
        "口径": ("只对候选池(合议Top-N∪各策略前K∪自选,有界)采新闻+news_ai+三层情绪+事件+资金流,"
                 "再召消息面专家二次合议产『消息面方向+理由』;**不改写 record['council'] 全A主排序**,"
                 "作为独立确认层附加信息。纯数据·非投资建议。"),
        "统计": {**stat, "有发声": 发声数},
        "来源计数": built["counts"],
        "富集报告": enrich_report,
        "确认": confirmations,
        "防未来函数": "复用采集/合议层各自 as_of 锚定,本节点不放宽口径",
    }
    p = store.put_view("候选池消息面确认", view, date=as_of)
    # 按票 code_view「消息面确认」(供个股页卡片)
    for x in confirmations:
        try:
            store.put_code_view("消息面确认", x["code"], x, date=as_of)
        except Exception:  # noqa: BLE001
            pass
    logger.info("候选池消息面富集节点完成 → %s;候选 %d,消息面确认发声 %d(看多%d/看空%d/中性%d/全弃权%d)",
                p, len(pool), 发声数, stat["看多"], stat["看空"], stat["中性"], stat["全弃权"])
    return view


def _main(argv: list[str] | None = None) -> int:
    """CLI:python -m tools.pipeline.candidate_message [--date YYYY-MM-DD] [--no-llm]
    [--council-top-n N] [--view-top-k K]。"""
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    as_of = None
    no_llm = "--no-llm" in argv
    council_top_n = 30
    per_view_top_k = 5
    if "--date" in argv:
        i = argv.index("--date")
        if i + 1 < len(argv):
            as_of = argv[i + 1]
    if "--council-top-n" in argv:
        i = argv.index("--council-top-n")
        if i + 1 < len(argv) and argv[i + 1].isdigit():
            council_top_n = int(argv[i + 1])
    if "--view-top-k" in argv:
        i = argv.index("--view-top-k")
        if i + 1 < len(argv) and argv[i + 1].isdigit():
            per_view_top_k = int(argv[i + 1])
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    rep = run_candidate_message_enrich(as_of, council_top_n=council_top_n,
                                       per_view_top_k=per_view_top_k, no_llm=no_llm)
    logger.info("完成:候选 %d,统计 %s", rep.get("候选池规模", 0), rep.get("统计"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
