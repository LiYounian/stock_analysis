"""#23 深采分层门控 · 边缘候选切片(排序型 screener 共用)。

背景(docs/计划/2026-09-04_深采分层门控_设计.md):screenall 的深采票池原是二值——
`code ∈ 选出并集∪自选` → 全套深采,否则当日 0 文件。**循环盲区**:未选中 → 数值面缺 →
合议专家弃权多 → 软收缩降权 → 更不被选中。方案 A 移植 two_stage 三层门控:新增「边缘候选」层
(紧挨入选之外的票)拿 6 类**数值面**深采(fundflow/chip/holder_num/block_trade/tick/consensus),
让其数值面专家不再因无数据弃权、拿到公允合议分浮出。新闻/LLM 情绪坚决不扩(最贵),仍限选出集。

排序型策略(council/momentum/半导体/反转)本就把全候选打分排序后截 TopN,全排名池已算好、
只是截断丢弃。本模块把「入选之外前 K 只」这一切片口径抽成单一真源,各 screener 复用后在 view 里
落 `边缘候选`(仅 code 列表,轻量),供 run_screen_all 汇总成有界的 analysis_set。

成本硬约束:数值面串行 0.5s/请求、单类源全A≈46min,故边缘集**必须设上界**——每策略取前
SCREENALL_EDGE_TOPK 只(本模块封顶),全局再由 run.py 按 SCREENALL_EDGE_MAX 二次封顶。
"""
from __future__ import annotations


def edge_slice(ranked_codes: list[str], selected: set[str],
               k: int | None = None) -> list[str]:
    """从排序型策略的**全排名 code 序列**里取入选之外的前 k 只作边缘候选。

    Args:
        ranked_codes: 该策略全候选按分降序的 code 序列(含入选;剔除票不应在内)。
        selected: 已入选票 code 集合(从边缘集中排除)。
        k: 每策略边缘上限;None → 读 settings.SCREENALL_EDGE_TOPK。k<=0 → 关闭(空)。

    Returns:
        入选之外、按排名靠前的前 k 只 code(保序去重)。取"入选之外前 k"而非"排名 K+1..K+k",
        对入选非严格前缀(如否决沉底、软降级)的排序也稳健——凡不在 selected 里、排名靠前的即边缘。
    """
    if k is None:
        from tools.config import settings
        k = int(getattr(settings, "SCREENALL_EDGE_TOPK", 0) or 0)
    if k <= 0:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for c in ranked_codes:
        if not isinstance(c, str) or not c or c in selected or c in seen:
            continue
        out.append(c)
        seen.add(c)
        if len(out) >= k:
            break
    return out
