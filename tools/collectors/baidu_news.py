"""百度个股新闻情绪采集(前向增量,每日盘后快照)。

研究结论见 `docs/策略/新闻源可得性探查_百度等.md`:百度公开 JSON 接口
`finance.pae.baidu.com/vapi/sentimentlist` 免登录、curl_cffi 直取,带**真实发布时间戳
+ 百度自带利好/利空标签(benefitType)**,聚合同花顺/东财/证券之星等多家 → 覆盖优于
现有「东财∪新浪∪财联社」,且自带情绪极性(可省一次 LLM 或做交叉校验)。

本采集器**只做采集 + 落盘**(前向消息面样本累积),不改现有 event.py 净情绪分逻辑、
不动 strategy.json。是否接入情绪专家由统筹后定;这里只额外提供一个 `benefit_to_sentiment`
适配函数备用(把 benefitType 映射成情绪方向),不在采集链路里调用。

设计要点:
- **真实发布时间**:每条落 `publish_ts`(unix 秒)+ `publish_time`(北京时区字符串),
  供 as-of 无未来函数切片(绝不用采集日冒充发布日)。
- **落盘 raw kind = "baidu_news"**,按 code + 采集日分区(走 store 层);payload 为
  按发布时间倒序的新闻列表。**幂等**:同一票的条目按 `news_id` 去重;与既有最近快照
  并集累积(前向增量,重跑不产生重复)。
- **新鲜度门控**:缓存 ≤ BAIDU_NEWS_STALE_DAYS 天视为新鲜 → 当日跳过重拉(对齐
  consensus / industry_history 的 skip-if-cached),避免同日多次跑重复打接口。
- **反爬容错**:限流/非 200/空数据/结构漂移一律优雅降级(单票 log 跳过,不中断整批)。
  港股无此接口 → 落空降级。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.baidu_news")

_SOURCE = "baidu"
_API = "https://finance.pae.baidu.com/vapi/sentimentlist"
_CST = timezone(timedelta(hours=8))   # 北京时区(发布时间戳按东八区解码)

# benefitType(百度自带情绪标签)→ 中文标签。研究报告假设 1/−1/0 = 利好/利空/中性
# (按分布推断,未逐条人工核;生产接入前应抽样校验)。字符串/数字都容忍。
_BENEFIT_LABEL = {1: "利好", -1: "利空", 0: "中性"}


def _to_int(v):
    """宽松转 int(容忍字符串/None/浮点/空串);失败返回 None。"""
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def benefit_label(benefit_type) -> str:
    """benefitType → 中文情绪标签(利好/利空/中性);无法识别 → 中性(保守)。"""
    return _BENEFIT_LABEL.get(_to_int(benefit_type), "中性")


def benefit_to_sentiment(benefit_type) -> float:
    """**备用适配器(本轮不接入)**:把 benefitType 映射成情绪方向 [-1, +1]。

    1=利好→+1.0 / −1=利空→−1.0 / 0 或未知=中性→0.0。供未来情绪专家/净情绪分交叉
    校验用;**当前不在采集链路调用,也不改 event.py 净情绪口径**。
    """
    b = _to_int(benefit_type)
    if b == 1:
        return 1.0
    if b == -1:
        return -1.0
    return 0.0


def _ts_to_bjt(ts) -> tuple[int | None, str]:
    """unix 秒 → (int 时间戳, 北京时区 'YYYY-MM-DD HH:MM:SS' 字符串)。无法解析→(None,'')。"""
    t = _to_int(ts)
    if t is None or t <= 0:
        return None, ""
    try:
        return t, datetime.fromtimestamp(t, _CST).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return None, ""


def _extract_list(payload) -> list[dict]:
    """从百度返回的嵌套结构里防御式取出 sentimentListInfo 列表。

    正常路径:Result[0].TplData.aiSentimentXcxListInfo.sentimentListInfo[]。
    结构漂移(字段改名/层级变动)→ 尽力兜底,取不到返回 [](降级,不抛)。
    """
    if not isinstance(payload, dict):
        return []
    result = payload.get("Result")
    # Result 可能是 list(取第0个)或直接 dict
    nodes = result if isinstance(result, list) else [result]
    for node in nodes:
        if not isinstance(node, dict):
            continue
        tpl = node.get("TplData")
        if not isinstance(tpl, dict):
            continue
        info = tpl.get("aiSentimentXcxListInfo") or tpl.get("sentimentInfo") or {}
        if isinstance(info, dict):
            lst = info.get("sentimentListInfo")
            if isinstance(lst, list) and lst:
                return lst
    return []


def _parse(items: list[dict]) -> list[dict]:
    """百度原始条目 → 归一新闻条目,按发布时间倒序。

    字段:{title, source, publish_time(北京时区字符串), publish_ts(unix秒),
    benefit_type(int/None), benefit_label(利好/利空/中性), abstract, url, news_id}。
    无发布时间戳或无标题的条目丢弃(保证 as-of 切片有可靠发布时间)。
    """
    out: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title", "")).strip()
        ts, tstr = _ts_to_bjt(it.get("publishTime"))
        if not title or ts is None:
            continue
        bt = _to_int(it.get("benefitType"))
        out.append({
            "title": title,
            "source": str(it.get("provider", "")).strip(),
            "publish_time": tstr,
            "publish_ts": ts,
            "benefit_type": bt,
            "benefit_label": benefit_label(bt),
            "abstract": str(it.get("abstract", "")).strip()[:2000],
            "url": str(it.get("originUrl") or it.get("api") or "").strip(),
            "news_id": str(it.get("news_id", "")).strip(),
        })
    out.sort(key=lambda x: x["publish_ts"], reverse=True)
    return out


def _fetch_raw(code: str, rn: int) -> list[dict]:
    """curl_cffi 伪装 chrome 拉百度 sentimentlist,返回原始条目列表(抽出便于 mock)。

    非 200 / JSON 解析失败 → 抛异常(交上层降级)。空列表正常返回 []。
    """
    from curl_cffi import requests as creq

    params = {
        "market": "ab", "code": code, "query": code, "financeType": "stock",
        "benefitType": "", "pn": 0, "rn": rn, "is_fallback": 0, "finClientType": "pc",
    }
    r = creq.get(_API, params=params, impersonate="chrome",
                 timeout=float(os.getenv("FETCH_TIMEOUT", "10")))
    r.raise_for_status()
    return _extract_list(r.json())


def _merge_incremental(new_items: list[dict], prev_items: list[dict]) -> list[dict]:
    """前向增量并集:新旧快照按 news_id 去重合并(无 news_id 退化用 url|title+ts)。

    先放新条目(供更新),再补旧快照独有条目 → 保留被平台后期下架的旧新闻,
    长期累积成无幸存者偏差的干净前向样本。按发布时间倒序。幂等:同 id 只留一份。
    """
    def _key(it: dict):
        nid = (it.get("news_id") or "").strip()
        if nid:
            return ("id", nid)
        url = (it.get("url") or "").strip()
        return ("u", url) if url else ("t", it.get("title", ""), it.get("publish_ts"))

    seen: set = set()
    merged: list[dict] = []
    for it in list(new_items) + list(prev_items):
        k = _key(it)
        if k in seen:
            continue
        seen.add(k)
        merged.append(it)
    merged.sort(key=lambda x: x.get("publish_ts") or 0, reverse=True)
    return merged


def _prev_snapshot(code: str) -> list[dict]:
    """读该票最近一次快照(任意日期分区);无则 []。用于增量并集累积。"""
    try:
        prev = store.get_raw("baidu_news", code)
        return prev if isinstance(prev, list) else []
    except FileNotFoundError:
        return []


def stale_codes(codes: list[str], max_days: float | None = None) -> list[str]:
    """返回需要重拉的票(缓存陈旧/无缓存)。供编排层 skip-if-cached 过滤用。

    对齐 consensus/industry 的门控:缓存 ≤ max_days 天视为新鲜 → 不在返回列表。
    """
    md = settings.BAIDU_NEWS_STALE_DAYS if max_days is None else max_days
    return [c for c in codes if store.is_stale("baidu_news", c, md)]


def fetch_one(code: str, rn: int | None = None) -> list[dict]:
    """拉单票百度新闻并解析(不落盘,不做门控)。失败抛异常(交上层)。港股返回 []。"""
    from tools.config import stock_pool
    if stock_pool.is_hk(code):
        return []
    rn = settings.BAIDU_NEWS_RN if rn is None else rn
    return _parse(_fetch_raw(code, rn))


def fetch_baidu_news(codes: list[str], rn: int | None = None,
                     skip_fresh: bool = True,
                     max_days: float | None = None) -> dict[str, list[dict]]:
    """批量采集百度个股新闻并落盘(前向增量、幂等、带新鲜度门控)。

    参数:
      rn         单票拉取条数(默认 settings.BAIDU_NEWS_RN)。
      skip_fresh 新鲜度门控开关(默认 True):缓存 ≤ max_days 天的票直接跳过重拉,
                 沿用既有快照(省网络、防限流);无缓存/无元数据一律视为陈旧会采。
      max_days   门控阈值(默认 settings.BAIDU_NEWS_STALE_DAYS)。

    落盘:store.put_raw("baidu_news", code, [条目...], meta={"source":"baidu", ...})。
    单票失败/空/被反爬 → log 跳过,不中断整批;港股整体落空降级。
    返回 {code: [条目...]}(含被跳过的票,回落其既有快照;真失败的票不入返回值)。
    """
    from tools.config import stock_pool

    settings.ensure_dirs()
    rn = settings.BAIDU_NEWS_RN if rn is None else rn
    md = settings.BAIDU_NEWS_STALE_DAYS if max_days is None else max_days

    out: dict[str, list[dict]] = {}
    failed: list[str] = []
    n = len(codes)
    for i, code in enumerate(codes, 1):
        # 新鲜度门控:已有新鲜快照 → 跳过重拉(沿用既有)
        if skip_fresh and not store.is_stale("baidu_news", code, md):
            try:
                out[code] = store.get_raw("baidu_news", code)
            except FileNotFoundError:
                out[code] = []
            logger.info("[%d/%d] 百度新闻 %s:缓存新鲜,跳过", i, n, code)
            continue

        logger.info("[%d/%d] 百度新闻 %s 采集...", i, n, code)
        if stock_pool.is_hk(code):
            store.put_raw("baidu_news", code, [], meta={"source": "none(hk)"})
            out[code] = []
            continue
        try:
            fresh = _parse(_fetch_raw(code, rn))
            # 前向增量并集:与最近快照按 news_id 去重合并,累积不丢旧条(幂等)
            merged = _merge_incremental(fresh, _prev_snapshot(code))
            store.put_raw("baidu_news", code, merged,
                          meta={"source": _SOURCE, "new_pulled": len(fresh),
                                "total": len(merged), "rn": rn})
            out[code] = merged
            logger.info("百度新闻 %s:新拉 %d 条,累积 %d 条", code, len(fresh), len(merged))
        except Exception as e:
            failed.append(code)
            logger.warning("百度新闻 %s 失败(降级跳过): %s", code, e)
        time.sleep(settings.FETCH_SLEEP_SEC)
    if failed:
        logger.warning("百度新闻拉取失败(%d): %s", len(failed), failed)
    return out


def load_baidu_news(code: str, date: str | None = None) -> list[dict]:
    """读单票百度新闻快照。缺失抛 FileNotFoundError。

    date:None → 最新分区(向后兼容);显式日期即锁读该日分区。
    """
    return store.get_raw("baidu_news", code, date=date or "latest")


def news_asof(code: str, as_of: str, date: str | None = None) -> list[dict]:
    """as-of 无未来函数切片:只返回**发布时间 ≤ as_of** 的条目(按发布时间倒序)。

    as_of 支持 'YYYY-MM-DD'(视为当日 23:59:59)或 'YYYY-MM-DD HH:MM:SS'。
    用**真实发布时间(publish_ts)**过滤,绝不用采集日冒充 → 回测切片无前视偏差。
    date:锁读哪个采集分区快照(默认最新);缺快照抛 FileNotFoundError。
    """
    s = as_of.strip()
    if len(s) <= 10:
        s = s[:10] + " 23:59:59"
    try:
        cutoff = int(datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
                     .replace(tzinfo=_CST).timestamp())
    except ValueError:
        cutoff = None
    items = load_baidu_news(code, date=date)
    if cutoff is None:
        return items
    return [it for it in items if (it.get("publish_ts") or 0) <= cutoff]
