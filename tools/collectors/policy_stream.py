"""L4:东财 7x24 全量快讯流并入 policy 池(去关键词化)。

关键词检索(policy.fetch_policy)之外的第二条政策进料:东财 7x24 全球财经快讯**全量流**,
按游标翻页拉到目标日 00:00,用东财每条自带的预打标(stockList=关联标的/板块码)归到申万一级,
再并入现有 policy 契约(keyword="7x24流")。覆盖关键词检索最难接住的「盘面异动快讯」
(不含政策词、不挂个股),见 docs/计划/2026-09-19_L4实现计划_东财7x24全量流并入policy池.md。

数据源(两个东财主机,分工不同):
  - 快讯流 `np-weblist.eastmoney.com/comm/web/getFastNewsList`(biz=web_724)——普通 requests
    直连即可,**不吃 TLS 指纹墙**。响应 data.fastNewsList[] 每条 {title,summary,showTime,code,
    stockList},data.sortEnd 为游标、data.total≈5000(可回溯约 10 天)。
  - BK 板块码→板块名 `push2delay.eastmoney.com/api/qt/stock/get`——**实时 push2 主机被 JA3 墙挡**
    (RemoteDisconnected,连 curl_cffi 都被拒),改走**延时主机 push2delay + curl_cffi 伪装 chrome**
    (与 fundflow.py 绕墙同一手法)。板块名不随行情变,用延时主机零损失;结果长期缓存。

打标口径:stockList 里 A 股码走现有 board_of(离线 code_industry,已归申万一级)、BK 码走
push2delay 解析板块名→industry_map.to_sw 归申万一级;无 stockList 的走规则兜底
(policy._match_industries→to_sw)。industries 槽统一存**申万一级名**(单一真源 to_sw)。

成本控制(本方案可控的关键):默认**池过滤**(只留碰票池申万一级的条目,因下游 event.score_policy
对 policy 池逐条调 LLM 打分)+ LLM 兜底**默认关闭**(不默认每条过 LLM)。

降级(约法5):取数/解析任一步失败记 logger、返回已拿到部分或空,**绝不 raise**——724 全挂时
policy.fetch_policy 仍走关键词/联播两源 fallback,流水线不断。

as-of(防未来红线):只保留 showTime 日期在目标窗口内的条目,**绝不纳入未来日条目**;原始流
按日归档(policy kind、code=policy724raw_{date}),回测重算打标可复现。
"""
from __future__ import annotations

import json
import logging

from tools.collectors import policy as pol
from tools.collectors._retry import retry_call
from tools.config import settings, stock_pool
from tools.store import repo as store

logger = logging.getLogger("collectors.policy_stream")

_STREAM_URL = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
_BK_URL = "https://push2delay.eastmoney.com/api/qt/stock/get"

_PAGE_SIZE = 200                       # 服务端封顶(传更大只回 200)
_MAX_PAGES = 80                        # 死循环兜底(~200×80=16000 条 > total 5000,足够冷启动回溯)
_SOURCE = "东财7x24"
_KEYWORD = "7x24流"

# BK 板块码→板块名缓存(进程级 + 扁平磁盘 json,跨天复用;BK 码稳定)。
_BK_CACHE: dict[str, str] | None = None


# ————————————————————————————————————————————————
# 取数:游标翻页(np-weblist,不吃墙,普通 requests)
# ————————————————————————————————————————————————
def _fetch_page(sort_end: str = "") -> dict:
    """拉一页快讯流,返回 data 字典({sortEnd,total,fastNewsList,...})。抽出便于测试 mock。"""
    import requests
    params = {
        "client": "web", "biz": "web_724", "fastColumn": "102",
        "sortEnd": sort_end, "pageSize": str(_PAGE_SIZE), "req_trace": "1",
    }
    r = requests.get(_STREAM_URL, params=params,
                     timeout=float(getattr(settings, "FETCH_TIMEOUT", 15) or 15))
    r.raise_for_status()
    return (r.json() or {}).get("data") or {}


def _iter_stream(target_date: str, max_days: int) -> list[dict]:
    """游标翻页拉快讯,收齐 [start_date, target_date] 窗口内的原始条目(未打标)。

    条目按时间倒序(新→旧)。翻页直到:翻过窗口下界 / 空页 / 游标不前进 / 超页数上限。
    **绝不纳入 showTime 日期 > target_date 的未来条目**(防未来红线)。
    返回原始 fastNewsList 条目列表(供归档 + 打标),失败抛(由 collect_stream 兜住)。
    """
    import pandas as pd
    start_date = (pd.Timestamp(target_date) - pd.Timedelta(days=max(max_days, 1) - 1)
                  ).strftime("%Y-%m-%d")
    raw: list[dict] = []
    sort_end = ""
    seen_cursors: set[str] = set()
    for page in range(_MAX_PAGES):
        data = retry_call(_fetch_page, sort_end, label="724快讯流")
        items = data.get("fastNewsList") or []
        if not items:
            break
        page_min_date = "9999-99-99"
        for it in items:
            d = str(it.get("showTime") or "")[:10]
            if not d:
                continue
            page_min_date = min(page_min_date, d)
            if d > target_date:            # 未来条目:剔除(防未来)
                continue
            if d < start_date:             # 窗口下界外:丢弃(继续看本页其余)
                continue
            raw.append(it)
        # 终止:本页最旧一条已早于窗口下界 → 已覆盖全窗
        if page_min_date < start_date:
            break
        nxt = str(data.get("sortEnd") or "")
        if not nxt or nxt in seen_cursors:  # 游标为空/不前进 → 停(防死循环)
            break
        seen_cursors.add(nxt)
        sort_end = nxt
    else:
        logger.warning("724快讯流翻页达上限 %d 页,可能未回溯到 %s(截断,已收 %d 条)",
                       _MAX_PAGES, start_date, len(raw))
    return raw


# ————————————————————————————————————————————————
# BK 板块码 → 板块名(push2delay + curl_cffi,带缓存)
# ————————————————————————————————————————————————
def _bk_cache_path():
    """BK 名缓存文件路径(挂在 store 的 raw 根下,测试 monkeypatch store._RAW_DIR 时一并重定向)。"""
    return store._RAW_DIR / "bk_name_cache.json"


def _load_bk_cache() -> dict[str, str]:
    global _BK_CACHE
    if _BK_CACHE is None:
        p = _bk_cache_path()
        try:
            _BK_CACHE = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (OSError, ValueError):
            _BK_CACHE = {}
    return _BK_CACHE


def _save_bk_cache() -> None:
    try:
        p = _bk_cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / (p.name + ".tmp")
        tmp.write_text(json.dumps(_BK_CACHE or {}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        import os
        os.replace(tmp, p)
    except OSError as e:
        logger.warning("BK 名缓存落盘失败(不影响解析): %s", e)


def _fetch_bk_name(bk_code: str) -> str | None:
    """push2delay + curl_cffi 伪装 chrome 拉 BK 板块名(f58)。抽出便于测试 mock。"""
    from curl_cffi import requests as creq
    params = {"secid": f"90.{bk_code}", "fields": "f57,f58"}
    r = creq.get(_BK_URL, params=params, impersonate="chrome",
                 timeout=float(getattr(settings, "FETCH_TIMEOUT", 10) or 10))
    r.raise_for_status()
    name = ((r.json() or {}).get("data") or {}).get("f58")
    return str(name).strip() or None if name else None


def _resolve_bk(bk_code: str) -> str | None:
    """BK 码 → 板块名;先查缓存,未命中才请求(瞬时错误重试)。失败返回 None、不缓存、不抛。"""
    cache = _load_bk_cache()
    if bk_code in cache:
        return cache[bk_code] or None
    try:
        name = retry_call(_fetch_bk_name, bk_code, label=f"BK解析{bk_code}")
    except Exception as e:                            # noqa: BLE001
        logger.warning("BK 解析失败 %s(跳过该板块码): %s", bk_code, e)
        return None
    if name:
        cache[bk_code] = name
        _save_bk_cache()
    return name


# ————————————————————————————————————————————————
# 打标:stockList / 规则兜底 → 申万一级
# ————————————————————————————————————————————————
def _tag_one(item: dict, use_llm: bool = False) -> list[str]:
    """一条快讯 → 命中申万一级名列表(去重保序)。

    ① stockList:A 股码(0./1. 前缀 6 位数)走 board_of;BK 码(90.BKxxxx)走 push2delay 解析
       →to_sw;美股/港股/基金等其它市场码跳过(board_of 命中与否天然过滤混入的基金码)。
    ② 无 stockList 命中:规则兜底 policy._match_industries(title+summary)→to_sw。
    ③ 仍空且 use_llm:LLM 兜底(默认关闭,见 _llm_industries)。
    """
    from tools.analysis.industry_map import to_sw
    from tools.collectors.board import board_of

    out: list[str] = []

    def _add(sw: str | None) -> None:
        if sw and sw not in out:
            out.append(sw)

    for secid in item.get("stockList") or []:
        s = str(secid)
        if "." not in s:
            continue
        market, raw = s.split(".", 1)
        if market == "90" and raw.startswith("BK"):        # 东财板块
            name = _resolve_bk(raw)
            _add(to_sw(name) if name else None)
        elif market in ("0", "1") and raw.isdigit() and len(raw) == 6:   # A 股个股
            _add(board_of(raw))
        # 其它市场(美股 105/106、港股 116、基金 0.16xxxx/150.x…)不参与打标

    if not out:                                            # ② 规则兜底
        text = f"{item.get('title', '')} {item.get('summary', '')}"
        for concept in pol._match_industries(text):
            _add(to_sw(concept))

    if not out and use_llm:                                # ③ LLM 兜底(默认关闭)
        for sw in _llm_industries(item):
            _add(sw)
    return out


def _llm_industries(item: dict) -> list[str]:
    """LLM 兜底归行业(默认路径不调用;use_llm=True 时对无预打标+规则未命中的条目才走)。

    仅返回申万一级名(过 to_sw 过滤,LLM 幻觉出的非申万名被丢)。任何失败返回 []、不中断。
    刻意保守:本轮不接入编排默认路径,避免全量流每条过 LLM 烧钱(见计划 §4.4)。
    """
    try:
        from tools.analysis import event
        from tools.analysis.industry_map import to_sw
        from tools import prompts
        client = event.lc.get_client()
        text = f"标题:{item.get('title', '')}\n摘要:{item.get('summary', '')}"
        instr = ("判断这条财经快讯主要影响哪些 A 股申万一级行业,只输出行业名列表(如电子/通信/"
                 "电力设备);无法判断输出空列表。")
        schema = getattr(prompts, "POLICY_INDUSTRY_SCHEMA", None)
        r = event._cached_extract(client, text, instr, schema) if schema else {}
        names = r.get("industries") or r.get("受影响行业") or []
        return [sw for sw in (to_sw(str(n)) for n in names) if sw]
    except Exception as e:                                # noqa: BLE001
        logger.warning("LLM 行业兜底失败(跳过): %s", e)
        return []


def _normalize_stream(item: dict, industries: list[str]) -> dict:
    """一条快讯 + 已算好的申万一级 industries → policy 契约(industries 已定,不再文本重打标)。

    ⚠️ industries 槽口径不变量(全库红线,勿假设单一格式):policy 池的 `industries` **混装**
    两种自由文本——关键词检索路径写**概念名**(policy._INDUSTRY_TERMS 的 key,如「半导体」
    「光通信」),本 724 路径写**申万一级名**(如「电子」「通信」)。**任何消费方按 industries 做
    行业匹配,一律先经 `industry_map.to_sw` 归一到申万一级再比对,禁止字面 `x in industries`
    直比**(直比会随任一侧措辞漂移而静默失配——正是词表漂移事故的同一类病)。to_sw 对申万名
    幂等、对概念名映射,过一遍即抹平两种格式差异。测试见 test_policy_stream 的语义锁用例。
    """
    title = str(item.get("title") or "").strip()
    summary = str(item.get("summary") or "").strip()
    show = str(item.get("showTime") or "")
    code = str(item.get("code") or "")
    text = f"{title} {summary}"
    return {
        "date": show[:10],
        "title": title,
        "source": _SOURCE,
        "url": f"https://finance.eastmoney.com/a/{code}.html" if code else "",
        "region": pol._classify_region(text),
        "summary": summary[:200],
        "industries": industries,
        "keyword": _KEYWORD,
    }


# ————————————————————————————————————————————————
# as-of 原始流归档
# ————————————————————————————————————————————————
def archive_raw_stream(date: str, raw_items: list[dict]) -> str:
    """把当日拉到的原始 fastNewsList(未打标、含 stockList)按日归档,供回测重算打标复现。

    复用 policy kind、code=policy724raw_{date}(不动 repo.py 的 kind 白名单)。返回落盘路径。
    """
    return store.put_raw("policy", f"policy724raw_{date}", raw_items,
                         meta={"source": "eastmoney_724"}, date=date)


def _pool_sw() -> set[str]:
    """当前票池的申万一级集合(与 policy.default_keywords 同口径,单一真源 to_sw)。"""
    from tools.analysis.industry_map import to_sw
    return {to_sw(s.sector) for s in stock_pool.get_pool()} - {None}


# ————————————————————————————————————————————————
# 主入口:取数 + 打标 + 过滤 + 归一(不落盘,交 policy.tag_and_dump 合并)
# ————————————————————————————————————————————————
def collect_stream(date: str | None = None, max_days: int = 1,
                   pool_only: bool = True, use_llm: bool = False,
                   archive: bool = True) -> list[dict]:
    """拉当日(或近 max_days 天)7x24 全量流 → 打标 → 过滤 → 归一到 policy 契约列表。

    date:目标交易日(缺省今天);max_days:回溯天数(冷启动可设 10)。
    pool_only:默认只留 industries 碰票池申万一级的条目(bound 下游逐条 LLM 打分成本)。
    use_llm:默认关闭的 LLM 行业兜底(见 _llm_industries)。
    archive:是否 as-of 归档原始流(默认 True)。

    **降级**:取数失败记 logger、返回 [](绝不 raise)——上层 fetch_policy 仍走关键词/联播两源。
    返回契约 dict 列表(industries 已是申万一级、keyword="7x24流");不落盘。
    """
    import pandas as pd
    date = date or pd.Timestamp.today().strftime("%Y-%m-%d")
    try:
        raw = _iter_stream(date, max_days)
    except Exception as e:                            # noqa: BLE001
        logger.error("724 全量流取数失败,降级为空(不中止流水线): %s", e)
        return []

    if archive and raw:
        try:
            archive_raw_stream(date, raw)
        except Exception as e:                        # noqa: BLE001
            logger.warning("724 原始流归档失败(不影响本次入库): %s", e)

    pool = _pool_sw() if pool_only else None
    out: list[dict] = []
    for it in raw:
        inds = _tag_one(it, use_llm=use_llm)
        if not inds:                                  # 无行业命中:同关键词路径 require_industry_hit
            continue
        if pool is not None and not (set(inds) & pool):
            continue
        out.append(_normalize_stream(it, inds))
    logger.info("724 全量流 %s:原始 %d 条 → 入池 %d 条(pool_only=%s,use_llm=%s)",
                date, len(raw), len(out), pool_only, use_llm)
    return out
