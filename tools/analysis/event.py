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
from concurrent.futures import ThreadPoolExecutor

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

# —— 情绪数据新鲜度三态(与 contracts.record ENUMS["新鲜度"] 对齐)——
FRESH, STALE, NODATA = "新鲜", "陈旧", "无数据"


def _classify_freshness(resolved_date: str | None, locked_date: str | None,
                        max_stale_days: int, mode: str) -> tuple[str, str | None]:
    """由 (实际读到的分区日 resolved, 锁定日 locked) 判某层新鲜度 + 采集日期。

    返回 (新鲜度, 采集日期);采集日期 = resolved_date(实际读到那份 raw 的分区日),
    「无数据」时采集日期为 None。规则(见计划 §4.4):
      - resolved 缺失 → 无数据。
      - 无锁定日(未 set_active_date 且未显式传 date)→ 无法判回退,按现状视为新鲜。
      - resolved == locked → 新鲜(当日锁定日采到)。
      - resolved < locked(发生回退):
          A1 严格锁定 → 无数据(绝不用旧数据);
          A2 可识别回退 → 窗口内(staleness_rank ≤ max)标陈旧,超窗标无数据。
    """
    from tools.store import repo as store
    if resolved_date is None:
        return NODATA, None
    if locked_date is None or resolved_date == locked_date:
        return FRESH, resolved_date
    if resolved_date > locked_date:        # 理论不应发生(pinned ≤ locked),保守当新鲜
        return FRESH, resolved_date
    # resolved < locked:回退发生
    if mode == "A1":
        return NODATA, None
    rank = store.raw_staleness_rank(resolved_date, locked_date)
    if rank > max_stale_days:
        return NODATA, None                # 超窗:不使用旧数据
    return STALE, resolved_date


def _resolve_layer(kind: str, code: str, locked_date: str | None,
                   max_stale_days: int, mode: str, legacy_loader):
    """date-pin 解析某层 raw + 判新鲜度。返回 (items, 新鲜度, 采集日期)。

    优先经 store.get_raw_resolved 解析(可判回退/超窗);store 无该 raw(或 mock/兼容环境)
    时回退 legacy_loader(code)——此路径无法从 store 判新鲜度,按锁定日尽力标新鲜。
    「无数据」/超窗 → items=[](不喂旧数据下游),新鲜度=无数据。
    """
    from tools.store import repo as store
    try:
        payload, resolved, _ = store.get_raw_resolved(kind, code, date=locked_date or "latest")
    except FileNotFoundError:
        payload, resolved = None, None
    except Exception as e:                 # store 异常不阻断情绪主流程,降级到 legacy
        logger.warning("%s %s get_raw_resolved 异常,回退 legacy loader:%s", code, kind, str(e)[:80])
        payload, resolved = None, None
    if resolved is not None:
        fresh, asof = _classify_freshness(resolved, locked_date, max_stale_days, mode)
        if fresh == NODATA:
            return [], NODATA, None
        return (payload if payload is not None else []), fresh, asof
    # store 无该 raw:回退 legacy(含被测试 monkeypatch 的 load_news/load_ugc)
    try:
        items = legacy_loader(code)
    except FileNotFoundError:
        return [], NODATA, None
    if not items:
        return [], NODATA, None
    return items, FRESH, locked_date       # 无法判定 → 保守按锁定日新鲜(兼容路径)


def _aggregate_freshness(layer_freshness: list[str]) -> str:
    """顶层新鲜度 = 三层「最坏优先」聚合(见计划 §4.3(3)):
    任一层陈旧 → 陈旧;全部无数据 → 无数据;否则新鲜。"""
    if any(f == STALE for f in layer_freshness):
        return STALE
    if layer_freshness and all(f == NODATA for f in layer_freshness):
        return NODATA
    return FRESH


def _aggregate_asof(layer_asofs: list[str | None]) -> str | None:
    """顶层采集日期 = 三层中**最旧**的一层日期(最保守,代表整块最陈旧成分);全 None → None。"""
    present = [d for d in layer_asofs if d]
    return min(present) if present else None


# ---------- LLM 缓存 ----------
def _cache_key(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _pmap(fn, items: list, workers: int) -> list:
    """有界线程池按输入顺序回填结果(index 对齐,防并发乱序)。

    - I/O 型(逐条 LLM 抽取),用线程池即可;workers<=1 或单条时退化为串行。
    - fn 须自行做失败隔离(内部 try/except 返回 {"error":...}),本函数不吞异常语义,
      只负责并发调度 + 顺序回填(下游可能按序处理)。
    - 缓存幂等:并发下多线程首次读同一缺失键最多重复调用一次、写同名文件幂等,可接受
      (不加锁,避免把并行锁没了)。
    """
    n = len(items)
    if n == 0:
        return []
    if workers <= 1 or n == 1:
        return [fn(i, items[i]) for i in range(n)]
    results: list = [None] * n
    with ThreadPoolExecutor(max_workers=min(workers, n)) as pool:
        futs = {pool.submit(fn, i, it): i for i, it in enumerate(items)}
        for fut in futs:
            results[futs[fut]] = fut.result()   # 按 index 回填,保持输入顺序
    return results


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
def extract_news_events(code: str, client=None, limit: int | None = None,
                        items: list[dict] | None = None) -> list[dict]:
    """对单票新闻逐条 LLM 抽取(有界线程池并行,I/O 型)。失败的条目标 error(不中断)。

    limit:只截断送 LLM 抽取的条数,取最近 limit 条(load_news 已按 time 倒序);
    原始新闻的落盘/展示不受此影响(见 news_ai / store)。
    items:可传入已由上层 date-pin 解析好的新闻列表(analyze_stock 走此路,避免二次读+统一新鲜度口径);
    缺省 None → 自读 nw.load_news(code)(向后兼容既有调用方/测试)。
    并发下按输入顺序回填结果(index 对齐),下游可按序处理。
    """
    s = stock_pool.get(code)
    name = s.name if s else code
    items = nw.load_news(code) if items is None else items
    if limit:
        items = items[:limit]           # 取最近 limit 条(倒序在前)
    client = client or lc.get_client()
    instr = prompts.news_extract_instruction(name, code)

    def _one(_i: int, n: dict) -> dict:
        text = f"标题:{n['title']}\n正文:{n['content']}"
        try:
            ev = _cached_extract(client, text, instr)
        except Exception as e:
            ev = {"error": str(e)[:80]}
        return {**ev, "time": n.get("time"), "source": n.get("source"),
                "url": n.get("url"), "标题": n.get("title")}

    return _pmap(_one, items, settings.LLM_EXTRACT_WORKERS)


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
    def _one(_i: int, it: dict) -> dict:
        text = f"标题:{it.get('title','')}\n摘要:{it.get('summary','')}"
        instr = prompts.policy_score_instruction(it.get("industries") or None)
        try:
            r = _cached_extract(client, text, instr, prompts.POLICY_SCORE_SCHEMA)
        except Exception as e:
            r = {"error": str(e)[:80]}
        return {**it, **r, "层": "政策"}

    scored: list[dict] = _pmap(_one, items, settings.LLM_EXTRACT_WORKERS)
    from tools.store import repo as store
    p = store.put_view("sentiment_policy", scored)     # 按日期视图
    logger.info("政策打分完成:%d 条 → %s", len(scored), p)
    return scored


def load_policy_scores() -> list[dict]:
    """读政策打分缓存(最新日期);缺失返回空 list(降级,不抛)。"""
    from tools.store import repo as store
    try:
        return store.get_view("sentiment_policy")
    except FileNotFoundError:
        return []


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


def _policy_freshness(locked_date: str | None, max_stale_days: int,
                      mode: str) -> tuple[str, str | None]:
    """政策层新鲜度(见计划 §4.4 政策特例)。

    政策按 policy_{锁定日} 聚合、文件名内嵌日期,不做「读旧文件冒充今天」的回退——
    缺锁定日政策文件即视为回退/无当日政策:
      - 锁定日政策 raw 存在 → 新鲜(即便命中本票行业条数=0,仍是「有当日政策文件」);
      - 否则 date-pin 找 ≤锁定日的最近政策 raw:A2 窗口内标陈旧、超窗无数据;A1 无数据;
      - store 无任何政策 raw(mock/兼容)但有政策打分视图 → 尽力按锁定日标新鲜;全无 → 无数据。
    返回 (新鲜度, 采集日期)。
    """
    from tools.store import repo as store
    if locked_date is not None:
        if store.raw_exists("policy", f"policy_{locked_date}", locked_date):
            return FRESH, locked_date
        # 找 ≤锁定日的最近政策 raw(文件名内嵌日期,逐日回看)
        resolved = None
        for d in store.list_dates("raw")[::-1]:      # 降序
            if d <= locked_date and store.raw_exists("policy", f"policy_{d}", d):
                resolved = d
                break
        if resolved is not None:
            return _classify_freshness(resolved, locked_date, max_stale_days, mode)
    # store 无政策 raw:回退看是否有政策打分视图(mock/兼容路径)
    return (FRESH, locked_date) if load_policy_scores() else (NODATA, None)


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
def ugc_sentiment(code: str, client=None, n: int = UGC_SAMPLE_N,
                  posts: list[dict] | None = None) -> dict:
    """读 UGC 缓存 → 取前 n 帖批量给 LLM 判整体情绪(经缓存)。

    返回 {净情绪(-1~1), 多空, 样本数, 依据}。无 UGC 缓存 / LLM 失败 → 降级为
    中性 + degraded 标记(不抛,约法第5条)。
    posts:可传入已由上层 date-pin 解析好的帖子列表(analyze_stock 走此路);
    缺省 None → 自读 ug.load_ugc(code)(向后兼容既有调用方/测试)。
    """
    if posts is None:
        try:
            posts = ug.load_ugc(code)[:n]
        except FileNotFoundError as e:
            logger.warning("%s UGC 缓存缺失,舆情层降级:%s", code, e)
            return {"净情绪": 0.0, "多空": "中性", "样本数": 0, "degraded": "no_ugc_cache"}
    else:
        posts = posts[:n]
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
def analyze_stock(code: str, client=None, limit: int | None = None,
                  date: str | None = None, max_stale_days: int | None = None) -> dict:
    """单票情绪分析全流程,覆盖 新闻+舆情+政策 三层,落 sentiment/{code}.json。

    净情绪口径:三层加权(新闻0.5 / 政策0.3 / 舆情0.2,按信源可靠性),
    缺某层(无 UGC/政策缓存或 LLM 降级)则丢该层对存在层重归一——
    只有新闻时退化为纯新闻净情绪,**向后兼容**旧行为。
    顶层 sentiment 仍保留新闻聚合的 利好数/利空数/样本数(旧消费方兼容),
    三层明细见 sentiment["三层"]。

    date-pin 与新鲜度(附加字段,不动上述打分口径):
      - date 缺省取 store.active_date()(编排入口已 set_active_date)→ 当日跑锁当日;
        回测复算传历史日;三层各自 date-pin 读取 ≤锁定日的最近 raw。
      - 每层写 采集日期/新鲜度(新鲜/陈旧/无数据);顶层写 新鲜度(最坏优先聚合)/
        采集日期(最旧层)/锁定日期(active_date,诊断回退)。
      - 回退策略由 settings.SENTIMENT_FRESHNESS_MODE 决定(A2 默认可识别回退 / A1 严格锁定),
        窗口 settings.SENTIMENT_MAX_STALE_DAYS(可被 max_stale_days 覆盖)。

    limit 缺省用 settings.NEWS_EXTRACT_MAX(只截断送 LLM 抽取的最近条数,原文全量保留)。
    """
    if limit is None:
        limit = settings.NEWS_EXTRACT_MAX
    if max_stale_days is None:
        max_stale_days = getattr(settings, "SENTIMENT_MAX_STALE_DAYS", 3)
    mode = getattr(settings, "SENTIMENT_FRESHNESS_MODE", "A2")
    from tools.store import repo as store
    locked = date or store.active_date()               # 本次锁定的交易日(诊断/回退判定用)

    # 新闻层:date-pin 解析 → 抽取 → 聚合
    news_items, news_fresh, news_asof = _resolve_layer(
        "news", code, locked, max_stale_days, mode, nw.load_news)
    news_events = classify_events(extract_news_events(code, client, limit, items=news_items))
    news_agg = aggregate_sentiment(news_events)

    # 舆情层(降级不崩):date-pin 解析 → LLM 判情绪
    ugc_items, ugc_fresh, ugc_asof = _resolve_layer(
        "ugc", code, locked, max_stale_days, mode, ug.load_ugc)
    ugc = ugc_sentiment(code, client, posts=ugc_items)

    # 政策层:命中本票行业的政策条目(需先 score_policy;无则空,降级)。消费口径不变。
    pol_items = _stock_policy_items(code)
    pol_net, pol_n = _policy_layer_net(pol_items)
    pol_fresh, pol_asof = _policy_freshness(locked, max_stale_days, mode)

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

    # 顶层新鲜度聚合(最坏优先)+ 采集日期(最旧层)
    top_fresh = _aggregate_freshness([news_fresh, ugc_fresh, pol_fresh])
    top_asof = _aggregate_asof([news_asof, ugc_asof, pol_asof])

    sentiment = {
        **news_agg,                       # 兼容旧字段(利好数/利空数/样本数 为新闻口径)
        "净情绪分": total,                # 三层加权后总分
        "口径": "三层加权 新闻0.5/政策0.3/舆情0.2,缺层重归一",
        # —— 新增(附加、可选):顶层聚合新鲜度 ——
        "采集日期": top_asof,
        "新鲜度": top_fresh,
        "锁定日期": locked,
        "三层": {
            "新闻": {"净情绪": news_agg["净情绪分"], "样本数": news_agg["样本数"],
                     "利好数": news_agg["利好数"], "利空数": news_agg["利空数"],
                     "采集日期": news_asof, "新鲜度": news_fresh},
            "舆情": {**ugc, "采集日期": ugc_asof, "新鲜度": ugc_fresh},
            "政策": {"净情绪": pol_net, "样本数": pol_n,
                     "采集日期": pol_asof, "新鲜度": pol_fresh},
        },
    }
    rec = {"code": code, "sentiment": sentiment, "events": events}
    store.put_code_view("sentiment", code, rec)        # 按日期/按票视图
    return rec


def load_sentiment(code: str) -> dict:
    """读单票情绪分析(最新日期)。缺失抛 FileNotFoundError。"""
    from tools.store import repo as store
    return store.get_code_view("sentiment", code)
