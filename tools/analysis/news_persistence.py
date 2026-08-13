"""消息持续性分类(结构性 vs 短暂)· LLM 分类器。

承接 docs/计划/消息持续性分类_预注册与执行.md §1。

命题:现有净情绪分只判**极性**(利好/利空),不判**持续性**。真正决定"能不能拿住"的是——
这条**根源消息**(公告/政策)带来的是**结构性持续增长**,还是**短暂催化/蹭概念**?
- 结构性持续:长期订单/在手订单饱满、产能扩张/新产线投产、政策长期扶持(非一次性补贴)、
  主营业务实质改善、连续多期高增、护城河/份额提升。
- 短暂事件:单季度业绩波动、非经常性损益/一次性扭亏、蹭概念/热点跟风、纯资金/龙虎榜异动、
  传闻/未落地重组。

单条消息一次 LLM 调用;结果按 (指令+文本) hash 缓存(复用 event._cached_extract),关思考、可并行。
只对**根源消息**(公司公告/政策文本)分类,不判舆情噪声。

⚠️ 非投资建议。分类只依据消息文本本身(及调用方在文本里给出的、披露日之前的历史),
不做外部推断——避免把"事后才知道的兑现结果"回灌进分类(无未来函数)。
"""
from __future__ import annotations

import logging

from tools.analysis import event as _ev  # 复用 _pmap / _cached_extract 缓存与并发
from tools.config import settings
from tools.llm import client as lc

logger = logging.getLogger("analysis.news_persistence")

# —— 输出契约(schema)——
PERSISTENCE_SCHEMA = {
    "持续性": "结构性持续 / 短暂事件 / 中性 之一",
    "方向": "利好 / 利空 / 中性 之一(对该公司/行业基本面而言)",
    "印证强度": "强 / 中 / 弱 之一(公告+政策+基本面是否互相印证;仅单一来源且无佐证=弱)",
    "依据": "一句话中文说明判定持续性的依据(只据消息文本,禁编造;拿不准留空)",
}

_PERSIST_VALS = {"结构性持续", "短暂事件", "中性"}
_DIR_VALS = {"利好", "利空", "中性"}
_STRENGTH_VALS = {"强", "中", "弱"}


def persistence_instruction() -> str:
    """持续性分类指令。描述性判据(不塞具体 case),强制 JSON,禁编造。"""
    return (
        "你是金融消息持续性研判助手。下面是一条**根源消息**(公司公告 / 政策文本,可能附该主体的历史信息)。"
        "请判断这条消息反映的基本面变化是**结构性持续**还是**短暂事件**。要求:\n"
        "- 【持续性】只有三种取值:\n"
        "  · '结构性持续':长期/在手订单饱满、产能扩张或新产线投产、政策**长期**扶持(非一次性补贴)、"
        "主营业务实质改善、**连续多期高增长**、护城河或市场份额提升——这类变化市场难以瞬间完全定价、可能慢兑现。\n"
        "  · '短暂事件':单季度业绩波动、非经常性损益/一次性扭亏或补贴、蹭概念或热点跟风、纯资金面/龙虎榜异动、"
        "传闻或未落地的重组——这类多为一次性催化,易见光死。\n"
        "  · '中性':信息不足以判断,或与持续性无关。\n"
        "- 【方向】这条消息对该公司/行业基本面的方向(利好/利空/中性)。\n"
        "- 【印证强度】是否有多来源互相印证(公告+政策+基本面数据一致=强;单一来源但有具体数据支撑=中;"
        "仅传闻/单一来源且无佐证=弱)。\n"
        "- 【依据】一句话说明你判定持续性的依据。\n"
        "- 只依据消息文本(含随附的历史信息),**禁止编造或引入外部/事后信息**;拿不准时'持续性'填'中性'。\n"
        "只输出一个 JSON,不要任何解释/多余文字。")


def _normalize(r: dict) -> dict:
    """把 LLM 输出规整到受限取值集;越界值降级为'中性'/'弱',保留原始依据。"""
    if not isinstance(r, dict) or "error" in r:
        return {"持续性": None, "方向": None, "印证强度": None,
                "依据": (r or {}).get("error", "") if isinstance(r, dict) else "",
                "error": (r or {}).get("error") if isinstance(r, dict) else "非字典输出"}
    persist = r.get("持续性")
    direction = r.get("方向")
    strength = r.get("印证强度")
    return {
        "持续性": persist if persist in _PERSIST_VALS else "中性",
        "方向": direction if direction in _DIR_VALS else "中性",
        "印证强度": strength if strength in _STRENGTH_VALS else "弱",
        "依据": str(r.get("依据") or "")[:200],
    }


def classify(text: str, client=None) -> dict:
    """对单条根源消息分类。返回规整后的 {持续性, 方向, 印证强度, 依据}。

    LLM 失败 → 返回 {..None.., error}(降级不抛,约法第5条)。结果按文本 hash 缓存。
    """
    if not text or not text.strip():
        return {"持续性": None, "方向": None, "印证强度": None, "依据": "", "error": "空文本"}
    client = client or lc.get_client()
    instr = persistence_instruction()
    try:
        raw = _ev._cached_extract(client, text, instr, PERSISTENCE_SCHEMA)
    except Exception as e:  # noqa: BLE001
        logger.warning("持续性分类 LLM 失败,降级: %s", str(e)[:80])
        return {"持续性": None, "方向": None, "印证强度": None, "依据": "", "error": str(e)[:80]}
    return _normalize(raw)


def classify_batch(texts: list[str], client=None, workers: int | None = None) -> list[dict]:
    """批量分类(有界线程池并行,按输入顺序回填)。单条失败标 error 不中断整批。"""
    client = client or lc.get_client()
    workers = settings.LLM_EXTRACT_WORKERS if workers is None else workers

    def _one(_i: int, t: str) -> dict:
        return classify(t, client)

    return _ev._pmap(_one, texts, workers)
