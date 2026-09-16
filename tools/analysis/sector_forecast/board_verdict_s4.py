"""S4 · 结构化研判(每日·DeepSeek 思考模式关)——程序化流水线第四阶段。

设计契约:docs/计划/2026-09-16_消息板块选股_程序化流水线_设计.md §1 S4。
**采集与研判解耦**:读 S3 原始消息过程文件(data/sector_news/raw/<date>/<板块>.json),
逐板块喂 **DeepSeek-v4-pro(get_client() 默认·thinking 关)**,沿用现
`news_catalyst.board_verdict_instruction` + `BOARD_VERDICT_SCHEMA`(rubric 文字分级·不打分),
产板块整体评价 + 分级关键事件 → catalyst_<date>.json(全板块)。

模型选定(统筹 A/B·2026-09-16):DeepSeek-v4-pro·思考关。依据:分歧样本上 DeepSeek 正确判
「分歧」而 qwen3.8-max 误判「利好」(假利好风险);思考开关输出近一致→关(无收益不多花)。

并行:板块间相互独立 → **有界线程池并发**(默认 DEFAULT_WORKERS),按网关并发上限分批,
总时长≈单板块×批数。去重增量(analyzed/<sw>.json 各板块独立文件,线程安全)。
⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sector_forecast.board_verdict_s4")

# 网关并发上限(env 可覆盖):板块并行调用 DeepSeek 的有界工作线程数。保守默认 4,防打爆网关。
DEFAULT_WORKERS = int(os.getenv("SECTOR_S4_WORKERS", "4"))

# ════════════════════ S4 前置 · 长文 LLM 压缩(新闻加深配套)════════════════════
# S3 只截断不压(保持无 LLM);长文的关键信息可能被 500 字截断稀释。此处 S4 前置对**长文**
# 逐条 LLM 压成要点(时间/来源/关键数字/对本股影响·≤~160字),可缓存。压缩腾出 verdict 的
# 6000 字预算塞更多**不同事件**(凝练防过载)。压缩失败保留原截断文本、不崩。
COMPRESS_TRIGGER = 220                     # 正文超此长度才压(短文不折腾 LLM)
COMPRESS_MAX = 160                         # 压缩要点上限
_COMPRESS_SCHEMA = {"要点": f"≤{COMPRESS_MAX}字要点:保留时间/涉及主体/关键数字金额比例/对该公司影响方向;不编造不评论不复述标题"}


def _compress_instruction() -> str:
    return (f"把下面新闻正文压缩成不超过 {COMPRESS_MAX} 字的要点。必须保留:时间、涉及主体、"
            "关键数字/金额/比例、对该公司的影响方向。不要编造、不要加评论、不要复述标题。")


def compress_long_items(items: list[dict], client, *, trigger: int = COMPRESS_TRIGGER) -> int:
    """S4 前置:对长文(text_truncated 或超 trigger)逐条 LLM 压成要点(可缓存)。返回压缩条数。"""
    from tools.analysis import event
    n = 0
    for it in items:
        if it.get("kind") != "个股新闻":
            continue
        text = it.get("text", "") or ""
        if not it.get("text_truncated") and len(text) < trigger:
            continue
        if len(text) < trigger:
            continue
        try:
            r = event._cached_extract(client, text[:2000], _compress_instruction(), _COMPRESS_SCHEMA)
        except Exception:
            continue                       # 压缩失败:保留原截断文本
        pt = (r or {}).get("要点")
        if pt:
            it["text"] = pt[:COMPRESS_MAX + 20]
            it["compressed"] = True
            n += 1
    return n


def board_desc(entry: dict, sw: str) -> str:
    """金字塔④·板块消息面研判 → 塔尖可读的一段**凝练描述**(additive)。

    口径一致由 rubric_map **档标签**(利好/利空/中性·强/中/弱)保证——标签的标准定义由塔尖
    侧 **glossary 全票共享一次**(rubric_map.render_*),此处**只用标签、不内联定义**(防过载·
    §4.1 凝练防过载:板块描述全票共享一次,不逐票重复长定义)。
    塔尖选股读这段描述(不是 raw 全量)。格式初版·待统筹塔尖接口定稿后对齐(结构稳定,只调排布)。
    """
    面 = entry.get("消息标签") or "中性"
    强弱 = entry.get("强弱") or ""
    parts = [f"消息面：{面}·{强弱}".rstrip("·")]
    if entry.get("理由"):
        parts.append(f"研判：{entry['理由']}")
    if entry.get("持续性"):
        parts.append(f"持续性：{entry['持续性']}")
    # 关键催化:优先影响大的,取前 2(凝练防过载)
    evs = entry.get("关键事件") or []
    big = [e for e in evs if e.get("影响程度") == "大"]
    top = (big or evs)[:2]
    if top:
        cat = "；".join(
            f"{e.get('时间','')}·{(e.get('事件') or '')[:36]}"
            f"({e.get('方向','')}/影响{e.get('影响程度','')}/{e.get('来源','')})"
            for e in top)
        parts.append(f"关键催化：{cat}")
    # 资金流(主力净买/卖·凝练)
    flows = entry.get("资金流") or []
    net_in = [f for f in flows if f.get("方向") == "净买入"]
    if net_in:
        top_f = max(net_in, key=lambda f: f.get("累计净买亿", 0))
        parts.append(f"资金流：主力净买领先 {top_f.get('name') or top_f.get('code')}"
                     f"(+{top_f.get('累计净买亿')}亿)")
    if entry.get("时效"):
        parts.append(f"时效：{entry['时效']}")
    if entry.get("可靠性综述"):
        parts.append(f"可靠性：{entry['可靠性综述']}")
    return "｜".join(parts)


def _one_verdict(date: str, sw: str, raw: dict, client, *, compress: bool = True) -> dict:
    """单板块:用 S3 raw 的 items 跑 board_news_verdict(沿用 rubric 文字分级),组装输出。

    compress=True:先对长文 LLM 压缩(S4 前置·可缓存),再喂 verdict(腾预算塞更多事件)。
    """
    from tools.analysis.sector_forecast import news_catalyst as NC
    leads = raw.get("角色", [])
    items = raw.get("items", [])
    if compress:
        try:
            compress_long_items(items, client)
        except Exception as e:
            logger.warning("S4 长文压缩跳过 %s: %s", sw, str(e)[:80])
    v = NC.board_news_verdict(date, sw, leads, client=client, items=items)
    def _role(r):
        return [{"code": d["code"], "name": d.get("name", "")} for d in leads if d.get("role") == r]
    entry = {
        "消息标签": v.get("消息面", "中性"), "强弱": v.get("强弱"),
        "关键事件": v.get("关键事件", []), "持续性": v.get("持续性"),
        "时效": v.get("时效"), "可靠性综述": v.get("可靠性综述"), "理由": v.get("理由"),
        "n条": v.get("n条", 0), "新增": v.get("新增", 0), "时间跨度": v.get("时间跨度"),
        "龙头": _role("龙头"), "中军": _role("中军"), "主力": _role("主力"),
        "资金流": raw.get("资金流", []),
    }
    entry["描述"] = board_desc(entry, sw)      # 金字塔④凝练描述(additive·供塔尖选股读)
    return entry


def _boards_with_raw(date: str, *, out_root: Optional[str] = None) -> list[str]:
    """当日 S3 raw 目录里有哪些板块(第一个存在的目录为准)。"""
    from tools.config import settings
    from tools.backtest.iet_probe.data import _MAIN
    bases = [out_root] if out_root else [settings.PROJECT_ROOT, _MAIN]
    for base in bases:
        d = Path(base) / "data" / "sector_news" / "raw" / date
        if d.exists():
            return sorted(p.stem for p in d.glob("*.json") if not p.name.endswith(".tmp"))
    return []


def board_catalyst_from_raw(date: str, *, boards: Optional[list[str]] = None, client=None,
                            workers: int = DEFAULT_WORKERS, compress: bool = True,
                            out_root: Optional[str] = None) -> dict[str, dict]:
    """读 S3 raw → 全板块并行 DeepSeek 研判 → {板块: 研判dict}。并发有界(网关分批)。

    compress=True:verdict 前对长文 LLM 压缩(S4 前置·可缓存·腾预算);测试可关。
    """
    from tools.analysis.sector_forecast import board_news_collect as BC
    from tools.llm import client as lc
    boards = boards or _boards_with_raw(date, out_root=out_root)
    tasks = []
    for sw in boards:
        raw = BC.load_board_raw(date, sw, out_root=out_root)
        if raw:
            tasks.append((sw, raw))
    if not tasks:
        logger.warning("S4 无 S3 raw(先跑 S3 采集),不研判:%s", date)
        return {}
    client = client or lc.get_client()          # 共享 client(DeepSeek-v4-pro 默认·thinking关)
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(_one_verdict, date, sw, raw, client, compress=compress): sw
                for sw, raw in tasks}
        for fut in as_completed(futs):
            sw = futs[fut]
            try:
                results[sw] = fut.result()
            except Exception as e:               # 单板块失败隔离,不拖垮整批
                logger.warning("S4 板块 %s 研判失败(跳过): %s", sw, str(e)[:120])
    logger.info("S4 研判完成:%d/%d 板块(并发%d)", len(results), len(tasks), workers)
    return results


def write_catalyst(date: str, cat: dict[str, dict], *, out_root: Optional[str] = None) -> Path:
    """落 catalyst_<date>.json(全板块·沿用现格式 + rubric),扩「消息驱动」块进 sector_focus。"""
    from tools.analysis.sector_forecast import news_catalyst as NC
    from tools.config import settings
    root = Path(out_root) if out_root else settings.PROJECT_ROOT / "data" / "sector_news"
    root.mkdir(parents=True, exist_ok=True)
    利好 = [sw for sw, c in cat.items() if c.get("消息标签") == "利好"]
    payload = {
        "date": date, "version": NC.CATALYST_VERSION, "rubric": NC.RUBRIC,
        "board_verdict_schema": NC.BOARD_VERDICT_SCHEMA,
        "model": "deepseek-v4-pro(thinking off·统筹A/B择优)",
        "板块研判": cat, "利好板块": 利好,
        "口径": "S3采集(解耦)→S4 DeepSeek并行研判(rubric文字分级·去重增量);全板块",
        "免责": "测试环境研究模拟,非投资建议。",
    }
    out = root / f"catalyst_{date}.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(out)
    logger.info("S4 catalyst → %s(全板块%d·利好%d:%s)", out, len(cat), len(利好), "、".join(利好))
    return out
