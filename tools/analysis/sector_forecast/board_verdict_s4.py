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


def _one_verdict(date: str, sw: str, raw: dict, client) -> dict:
    """单板块:用 S3 raw 的 items 跑 board_news_verdict(沿用 rubric 文字分级),组装输出。"""
    from tools.analysis.sector_forecast import news_catalyst as NC
    leads = raw.get("角色", [])
    v = NC.board_news_verdict(date, sw, leads, client=client, items=raw.get("items", []))
    def _role(r):
        return [{"code": d["code"], "name": d.get("name", "")} for d in leads if d.get("role") == r]
    return {
        "消息标签": v.get("消息面", "中性"), "强弱": v.get("强弱"),
        "关键事件": v.get("关键事件", []), "持续性": v.get("持续性"),
        "时效": v.get("时效"), "可靠性综述": v.get("可靠性综述"), "理由": v.get("理由"),
        "n条": v.get("n条", 0), "新增": v.get("新增", 0), "时间跨度": v.get("时间跨度"),
        "龙头": _role("龙头"), "中军": _role("中军"), "主力": _role("主力"),
        "资金流": raw.get("资金流", []),
    }


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
                            workers: int = DEFAULT_WORKERS,
                            out_root: Optional[str] = None) -> dict[str, dict]:
    """读 S3 raw → 全板块并行 DeepSeek 研判 → {板块: 研判dict}。并发有界(网关分批)。"""
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
        futs = {ex.submit(_one_verdict, date, sw, raw, client): sw for sw, raw in tasks}
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
