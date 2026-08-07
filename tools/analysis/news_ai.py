"""统一「新闻 + AI 分析」视图(一次生产,处处复用)。

把原始新闻(data/raw/<日期>/news/{code}.json)与 event.py 已有的逐条 LLM 抽取
(方向/强度/关系/摘要/原因,已带内容 hash 缓存,不额外烧钱)按索引对齐合并,
落成按票视图 data/analysis/<日期>/news_ai/{code}.json,一条一新闻:
    {title, time, source, url, content, ai:{方向, 强度, 与本股关系, 评论, 原因}}
消费端(web/data_access)只认这一份,/news 列 / 个股页新闻块 / 详情页 共用同一 reader。

复用点:LLM 抽取复用 event.extract_news_events(含 _cached_extract 缓存);
存取复用 store 的 code_view;按日期复用现有分区。
降级:LLM 未配置 / 抽取抛错 / 某条缺字段 → 该条 ai 降级中性,不崩(约法第5条)。
设计见 docs/计划/新闻AI评论与交互改进.md。
"""
from __future__ import annotations

import logging

from tools.analysis import event
from tools.store import repo as store

logger = logging.getLogger("analysis.news_ai")


def _degraded_ai() -> dict:
    """LLM 未配置 / 抽取失败 / 无有效字段时的降级 ai 块(中性、不崩)。"""
    return {"方向": "中性", "强度": 0, "与本股关系": "", "评论": "", "原因": ""}


def _to_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _to_ai(ev: dict | None) -> dict:
    """把单条 event 抽取结果映射成统一 ai 块。缺字段/标 error → 降级中性。

    评论=复用抽取「摘要」(空则「暂无」);原因=复用抽取「原因」(老缓存无该字段降级空串)。
    """
    if not ev or "error" in ev or "影响方向" not in ev:
        return _degraded_ai()
    comment = (ev.get("摘要") or "").strip() or "暂无"
    return {
        "方向": ev.get("影响方向") or "中性",
        "强度": _to_int(ev.get("影响强度"), 0),
        "与本股关系": ev.get("与本股关系") or "",
        "评论": comment,
        "原因": (ev.get("原因") or "").strip(),
    }


def _assemble(n: dict, ai: dict) -> dict:
    """原始新闻字段 + ai 块合成统一条目。"""
    return {
        "title": n.get("title", ""),
        "time": n.get("time", ""),
        "source": n.get("source", ""),
        "url": n.get("url", ""),
        "content": n.get("content", ""),
        "ai": ai,
    }


def enrich_news(code: str, date: str | None = None, client=None) -> list[dict]:
    """读某票原始新闻 + 复用 event 逐条抽取,按索引对齐合并成统一「新闻+AI」列表。

    date 缺省 latest(与编排同日,复用 sentiment 阶段已建的 LLM 缓存)。
    无原始新闻 → 返回 [];LLM 未配置 / 抽取抛错 → 全部 ai 降级中性(不崩)。
    """
    try:
        items = store.get_raw("news", code, date=date or "latest")
    except FileNotFoundError:
        return []
    if not isinstance(items, list) or not items:
        return []

    try:
        events = event.extract_news_events(code, client=client, limit=len(items))
    except Exception as e:                       # LLM 未配置 / 网络等 → 全条降级
        logger.warning("%s 新闻抽取失败,ai 全部降级中性:%s", code, str(e)[:80])
        events = []

    out = []
    for i, n in enumerate(items):
        ev = events[i] if i < len(events) else None
        out.append(_assemble(n, _to_ai(ev)))
    return out


def write_news_ai(codes: list[str], date: str | None = None, client=None) -> int:
    """批量生产「新闻+AI」按票视图并落盘。返回落盘票数(无新闻的票跳过)。"""
    n = 0
    for code in codes:
        items = enrich_news(code, date=date, client=client)
        if items:
            store.put_code_view("news_ai", code, items, date=date)
            n += 1
    logger.info("新闻 AI 视图落盘 %d 只(store 按日期)", n)
    return n
