"""新闻情绪分析:L1 关键信息提取(LLM) + 三层归类(代码) + L2 情感聚合(代码)。

单条新闻仅一次 LLM 调用(抽取);归类/聚合走代码,省成本。
LLM 结果按输入 hash 缓存(data/raw/llm_cache),可重跑、改下游免重复烧钱。
过程结果落 data/analysis/sentiment/{code}.json(可追溯)。
契约见 docs/计划/P2C_新闻情绪LLM.md。
"""
from __future__ import annotations

import hashlib
import json
import logging

from tools.collectors import news as nw
from tools.config import settings, stock_pool
from tools.llm import client as lc
from tools.llm import prompts

logger = logging.getLogger("analysis.event")

LAYERS = ("政策", "公司行为", "舆情")
_SENT_DIR = settings.PROJECT_ROOT / "data" / "analysis" / "sentiment"
_DIR_SIGN = {"利好": 1, "利空": -1, "中性": 0}
_REL_W = {"直接": 1.0, "间接": 0.5, "无关": 0.0}


# ---------- LLM 缓存 ----------
def _cache_key(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _cached_extract(client, text: str, instruction: str) -> dict:
    """按 (指令+文本) hash 缓存 LLM 抽取结果。"""
    settings.LLM_CACHE.mkdir(parents=True, exist_ok=True)
    p = settings.LLM_CACHE / f"{_cache_key(instruction + '||' + text)}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    r = client.extract(text, prompts.NEWS_EXTRACT_SCHEMA, instruction=instruction)
    p.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    return r


# ---------- L1 抽取 ----------
def extract_news_events(code: str, client=None, limit: int | None = None) -> list[dict]:
    """对单票新闻逐条 LLM 抽取。失败的条目标 error(不中断)。"""
    s = stock_pool.get(code)
    name = s.name if s else code
    items = nw.load_news(code)
    if limit:
        items = items[:limit]
    client = client or lc.get_client()
    instr = prompts.news_extract_instruction(name, code)
    events = []
    for n in items:
        text = f"标题:{n['title']}\n正文:{n['content']}"
        try:
            ev = _cached_extract(client, text, instr)
        except Exception as e:
            ev = {"error": str(e)[:80]}
        events.append({**ev, "time": n.get("time"), "source": n.get("source"),
                       "url": n.get("url"), "标题": n.get("title")})
    return events


# ---------- 三层归类(代码)----------
def classify_events(events: list[dict]) -> list[dict]:
    """按事件类型映射情绪三层(政策/公司行为/舆情)。"""
    for e in events:
        if "事件类型" in e:
            e["层"] = prompts.type_to_layer(e["事件类型"])
    return events


# ---------- L2 情感聚合(代码)----------
def aggregate_sentiment(events: list[dict]) -> dict:
    """把单票多条事件聚合成情绪分。无关条目按关系权重 0 剔除。"""
    total, bull, bear, n = 0.0, 0, 0, 0
    for e in events:
        if "影响方向" not in e or "error" in e:
            continue
        rel = _REL_W.get(e.get("与本股关系"), 0.5)
        if rel == 0:
            continue
        sign = _DIR_SIGN.get(e.get("影响方向"), 0)
        try:
            strength = float(e.get("影响强度") or 1)
        except (TypeError, ValueError):
            strength = 1.0
        total += sign * strength * rel / 5.0
        n += 1
        if sign > 0:
            bull += 1
        elif sign < 0:
            bear += 1
    return {"净情绪分": round(total / n, 3) if n else 0.0,
            "利好数": bull, "利空数": bear, "样本数": n}


# ---------- 编排 + 落盘 ----------
def analyze_stock(code: str, client=None, limit: int | None = None) -> dict:
    """单票情绪分析全流程,过程结果落 data/analysis/sentiment/{code}.json。"""
    events = classify_events(extract_news_events(code, client, limit))
    rec = {"code": code, "sentiment": aggregate_sentiment(events), "events": events}
    _SENT_DIR.mkdir(parents=True, exist_ok=True)
    (_SENT_DIR / f"{code}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def load_sentiment(code: str) -> dict:
    p = _SENT_DIR / f"{code}.json"
    if not p.exists():
        raise FileNotFoundError(f"{code} 无情绪分析,请先 analyze_stock: {p}")
    return json.loads(p.read_text(encoding="utf-8"))
