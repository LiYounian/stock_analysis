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
from tools.collectors import policy as pol
from tools.collectors import ugc as ug
from tools.config import settings, stock_pool
from tools.llm import client as lc
from tools.llm import prompts

logger = logging.getLogger("analysis.event")

LAYERS = ("政策", "公司行为", "舆情")
_ANALYSIS_DIR = settings.PROJECT_ROOT / "data" / "analysis"
_SENT_DIR = _ANALYSIS_DIR / "sentiment"
_POLICY_SENT = _ANALYSIS_DIR / "sentiment_policy.json"   # 政策打分(全局 list)
_DIR_SIGN = {"利好": 1, "利空": -1, "中性": 0}
_REL_W = {"直接": 1.0, "间接": 0.5, "无关": 0.0}

# —— 三层加权口径(见 analyze_stock docstring)——
# 权重按信源可靠性:新闻(事实) > 政策(方向) > 舆情(散户噪声)。
# 缺某层(无缓存/LLM 降级)则丢该层并对存在层重归一,天然向后兼容。
UGC_SAMPLE_N = 20                       # UGC 单次给 LLM 判整体情绪的取帖上限
_LAYER_W = {"新闻": 0.5, "政策": 0.3, "舆情": 0.2}


# ---------- LLM 缓存 ----------
def _cache_key(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _cached_extract(client, text: str, instruction: str, schema: dict | None = None) -> dict:
    """按 (指令+文本) hash 缓存 LLM 抽取结果。schema 缺省用新闻抽取 schema(向后兼容)。"""
    schema = schema or prompts.NEWS_EXTRACT_SCHEMA
    settings.LLM_CACHE.mkdir(parents=True, exist_ok=True)
    p = settings.LLM_CACHE / f"{_cache_key(instruction + '||' + text)}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    r = client.extract(text, schema, instruction=instruction)
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


# ---------- 政策层:逐条 LLM 打分(舆情三层之「政策」)----------
def score_policy(client=None, date: str | None = None) -> list[dict]:
    """读政策缓存 → 逐条 LLM 判方向/强度(经 `_cached_extract` 同款缓存)→ 落
    data/analysis/sentiment_policy.json(全局 list,供各票命中其行业时取用)。

    每条 = 原政策字段 + {影响方向, 影响强度, 受影响行业};单条 LLM 失败标 error 不中断。
    政策缓存缺失(未 fetch_policy)则返回空 list 并落空文件(降级不崩,约法第5条)。
    """
    client = client or lc.get_client()
    try:
        items = pol.load_policy(date)
    except FileNotFoundError as e:
        logger.warning("政策缓存缺失,跳过打分(先 collect):%s", e)
        items = []
    scored: list[dict] = []
    for it in items:
        text = f"标题:{it.get('title','')}\n摘要:{it.get('summary','')}"
        instr = prompts.policy_score_instruction(it.get("industries") or None)
        try:
            r = _cached_extract(client, text, instr, prompts.POLICY_SCORE_SCHEMA)
        except Exception as e:
            r = {"error": str(e)[:80]}
        scored.append({**it, **r, "层": "政策"})
    _ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    _POLICY_SENT.write_text(
        json.dumps(scored, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("政策打分完成:%d 条 → %s", len(scored), _POLICY_SENT)
    return scored


def load_policy_scores() -> list[dict]:
    """读政策打分缓存;缺失返回空 list(降级,不抛)。"""
    if not _POLICY_SENT.exists():
        return []
    return json.loads(_POLICY_SENT.read_text(encoding="utf-8"))


def _stock_policy_items(code: str) -> list[dict]:
    """命中本票所属行业的政策打分条目(按 industries 或 LLM 受影响行业)。"""
    s = stock_pool.get(code)
    if not s:
        return []
    out = []
    for it in load_policy_scores():
        hit = s.sector in (it.get("industries") or []) or \
            s.sector in (it.get("受影响行业") or [])
        if hit:
            out.append(it)
    return out


def _policy_layer_net(items: list[dict]) -> tuple[float, int]:
    """政策条目 → 政策层净情绪 [-1,1] + 样本数。方向×强度/5 后取均值。"""
    total, n = 0.0, 0
    for it in items:
        if "影响方向" not in it or "error" in it:
            continue
        sign = _DIR_SIGN.get(it.get("影响方向"), 0)
        try:
            strength = float(it.get("影响强度") or 1)
        except (TypeError, ValueError):
            strength = 1.0
        total += sign * strength / 5.0
        n += 1
    return (round(total / n, 3), n) if n else (0.0, 0)


# ---------- 舆情层:一批股吧帖 → LLM 判整体情绪 ----------
def ugc_sentiment(code: str, client=None, n: int = UGC_SAMPLE_N) -> dict:
    """读 UGC 缓存 → 取前 n 帖批量给 LLM 判整体情绪(经缓存)。

    返回 {净情绪(-1~1), 多空, 样本数, 依据}。无 UGC 缓存 / LLM 失败 → 降级为
    中性 + degraded 标记(不抛,约法第5条)。
    """
    try:
        posts = ug.load_ugc(code)[:n]
    except FileNotFoundError as e:
        logger.warning("%s UGC 缓存缺失,舆情层降级:%s", code, e)
        return {"净情绪": 0.0, "多空": "中性", "样本数": 0, "degraded": "no_ugc_cache"}
    if not posts:
        return {"净情绪": 0.0, "多空": "中性", "样本数": 0, "degraded": "empty_ugc"}

    s = stock_pool.get(code)
    name = s.name if s else code
    text = "\n".join(f"{i + 1}. {p.get('text', '')}" for i, p in enumerate(posts))
    instr = prompts.ugc_sentiment_instruction(name, code)
    client = client or lc.get_client()
    try:
        r = _cached_extract(client, text, instr, prompts.UGC_SENTIMENT_SCHEMA)
    except Exception as e:
        logger.warning("%s UGC 情感 LLM 失败,降级:%s", code, str(e)[:80])
        return {"净情绪": 0.0, "多空": "中性", "样本数": len(posts), "degraded": str(e)[:80]}
    try:
        net = max(-1.0, min(1.0, float(r.get("净情绪") or 0.0)))
    except (TypeError, ValueError):
        net = 0.0
    return {"净情绪": round(net, 3), "多空": r.get("多空", "中性"),
            "样本数": len(posts), "依据": r.get("依据", "")}


# ---------- 三层加权 ----------
def _weighted_net(layers: dict[str, tuple[float, int]]) -> float:
    """三层加权净情绪。layers: {层名: (净情绪, 样本数)}。仅对有样本的层加权并重归一。"""
    num, den = 0.0, 0.0
    for name, (net, cnt) in layers.items():
        if cnt > 0:
            w = _LAYER_W.get(name, 0.0)
            num += w * net
            den += w
    return round(num / den, 3) if den else 0.0


# ---------- 编排 + 落盘 ----------
def analyze_stock(code: str, client=None, limit: int | None = None) -> dict:
    """单票情绪分析全流程,覆盖 新闻+舆情+政策 三层,落 sentiment/{code}.json。

    净情绪口径:三层加权(新闻0.5 / 政策0.3 / 舆情0.2,按信源可靠性),
    缺某层(无 UGC/政策缓存或 LLM 降级)则丢该层对存在层重归一——
    只有新闻时退化为纯新闻净情绪,**向后兼容**旧行为。
    顶层 sentiment 仍保留新闻聚合的 利好数/利空数/样本数(旧消费方兼容),
    三层明细见 sentiment["三层"]。
    """
    news_events = classify_events(extract_news_events(code, client, limit))
    news_agg = aggregate_sentiment(news_events)

    # 舆情层(降级不崩)
    ugc = ugc_sentiment(code, client)
    # 政策层:命中本票行业的政策条目(需先 score_policy;无则空,降级)
    pol_items = _stock_policy_items(code)
    pol_net, pol_n = _policy_layer_net(pol_items)

    layers = {
        "新闻": (news_agg["净情绪分"], news_agg["样本数"]),
        "舆情": (float(ugc.get("净情绪") or 0.0), int(ugc.get("样本数") or 0)),
        "政策": (pol_net, pol_n),
    }
    total = _weighted_net(layers)

    # 政策条目并入 events(标注层=政策),使 events 覆盖三层来源
    pol_events = [{
        "事件类型": "政策", "层": "政策",
        "影响方向": it.get("影响方向"), "影响强度": it.get("影响强度"),
        "受影响行业": it.get("受影响行业"), "标题": it.get("title"),
        "source": it.get("source"), "url": it.get("url"),
        "time": it.get("date"), "keyword": it.get("keyword"),
        **({"error": it["error"]} if "error" in it else {}),
    } for it in pol_items]
    events = news_events + pol_events

    sentiment = {
        **news_agg,                       # 兼容旧字段(利好数/利空数/样本数 为新闻口径)
        "净情绪分": total,                # 三层加权后总分
        "口径": "三层加权 新闻0.5/政策0.3/舆情0.2,缺层重归一",
        "三层": {
            "新闻": {"净情绪": news_agg["净情绪分"], "样本数": news_agg["样本数"],
                     "利好数": news_agg["利好数"], "利空数": news_agg["利空数"]},
            "舆情": ugc,
            "政策": {"净情绪": pol_net, "样本数": pol_n},
        },
    }
    rec = {"code": code, "sentiment": sentiment, "events": events}
    _SENT_DIR.mkdir(parents=True, exist_ok=True)
    (_SENT_DIR / f"{code}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def load_sentiment(code: str) -> dict:
    p = _SENT_DIR / f"{code}.json"
    if not p.exists():
        raise FileNotFoundError(f"{code} 无情绪分析,请先 analyze_stock: {p}")
    return json.loads(p.read_text(encoding="utf-8"))
