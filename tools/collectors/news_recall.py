"""新闻扩召回 + LLM 相关性初筛(个股维度盲区补召回)。

现有个股新闻只采「东财/新浪按代码挂到该股 + 财联社按名过滤」,漏掉**不挂到个股、
但对该股重要的行业/宏观/管制类消息**(如「算力出口管制」对算力股)。本模块:

1. **按股票行业动态生成主题关键词**(不写死),扩大召回;
2. 扩召回后**用 LLM(关思考)做相关性初筛**,**中档**——留 ①点名本股 ②政策/管制/监管
   明确冲击其主营(即使不点名) ③直接上下游重大事件;滤掉 纯概念异动·涨停 / 其他个股 / 泛大盘综述。

**范围**:只对调用方传入的这批票生效(news.fetch_news(recall=True) 时按票调用),
不对全A。降级不崩:任一环节失败/空,返回 [](不污染既有三源并集)。

行业源优先级(见 `stock_industry`):
  ① 自选池 `config/stock_pool.json` 手工 sector(精调,准);
  ② 回退 baostock 证监会行业(`collectors.board` 已缓存的全市场映射;登录慢,故只读
     已缓存映射,不在本模块内触发 baostock 登录——依赖上游先跑
     `board.fetch_membership_baostock()`,缺映射则该票不扩召回,降级)。

关键词映射(见 `keywords_for`):自选池 sector 直接命中 `policy._INDUSTRY_TERMS`;
baostock 证监会大类(较粗/转型股会失真)按子串粗映射到主题词。**不把股票名当关键词**
(名已由个股 feed 覆盖;扩召回要的是未挂到个股的行业/主题消息)。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.collectors import news as nw
from tools.collectors import policy as pol
from tools.config import settings, stock_pool
from tools.llm import client as lc

logger = logging.getLogger("collectors.news_recall")

# —— baostock 证监会行业「门类名(子串)→ 主题词」粗映射 ——
# baostock query_stock_industry 返回证监会门类(如「计算机、通信和其他电子设备制造业」),
# 较 stock_pool 手工 sector 粗、且转型股会失真(如行云科技被分到「零售业」)。
# 按**子串命中**粗映射到主题词(顺序匹配,先命中先用);命中不到 → 空(该票不扩召回,宁严)。
_CSRC_TERMS: list[tuple[str, list[str]]] = [
    ("计算机", ["半导体", "芯片", "算力", "数据中心", "AI芯片", "国产替代"]),
    ("通信", ["通信设备", "光模块", "算力", "5G", "数据中心"]),
    ("电子", ["电子元件", "半导体", "消费电子", "PCB", "国产替代"]),
    ("软件", ["软件", "信创", "算力", "人工智能", "数据要素"]),
    ("信息技术", ["软件", "信创", "算力", "人工智能", "数据要素"]),
    ("专用设备", ["半导体设备", "机器人", "自动化", "先进制造"]),
    ("通用设备", ["机器人", "自动化", "工业母机", "先进制造"]),
    ("电气机械", ["新能源", "机器人", "电机", "储能", "光伏"]),
    ("汽车", ["新能源车", "汽车零部件", "智能驾驶", "汽车"]),
    ("电力", ["电力", "电价", "上网电价", "火电", "水电"]),
    ("热力", ["电力", "电价", "火电"]),
    ("有色", ["稀土", "永磁", "有色金属", "新能源材料"]),
    ("化学", ["新材料", "化工", "新能源材料"]),
    ("医药", ["医药", "创新药", "医疗器械"]),
    ("仪器仪表", ["仪器仪表", "传感器", "自动化"]),
]

# —— 相关性初筛(LLM)schema:只输出 相关/不相关 ——
_RELEVANCE_SCHEMA = {"相关": "相关/不相关 之一(该新闻是否与目标股票直接相关)"}


def stock_industry(code: str) -> str | None:
    """返回单票行业标签(供关键词映射)。优先级:自选池手工 sector > baostock 证监会。

    - 自选池命中:返回 `sector`(与 `policy._INDUSTRY_TERMS` 的 key 对齐,可直接取主题词)。
    - 否则:读 `collectors.board` 已缓存的 baostock 证监会映射(`board_of`,缺映射返回
      None,不在此触发 baostock 登录)。
    - 都无:None(该票不扩召回)。
    """
    s = stock_pool.get(code)
    if s and s.sector:
        return s.sector
    try:
        from tools.collectors import board
        return board.board_of(code)          # 缓存缺失 → None(advisory,不抛)
    except Exception as e:                    # noqa: BLE001 — 行业源是 advisory,任何失败都降级
        logger.warning("行业查询 %s 失败(降级不扩召回): %s", code, e)
        return None


def keywords_for(code: str, name: str, industry: str | None) -> list[str]:
    """行业标签 → 主题检索关键词(去重、上限 NEWS_RECALL_KEYWORD_CAP)。

    - `industry` 命中 `policy._INDUSTRY_TERMS`(自选池 sector)→ 直接取该板块主题词。
    - 否则按 `_CSRC_TERMS` 子串粗映射(baostock 证监会门类)。
    - 命中不到 → 空(该票不扩召回,宁严)。
    **不把股票名当关键词**(name/code 仅用于日志,不进关键词)。
    """
    if not industry:
        return []
    terms: list[str] = []
    if industry in pol._INDUSTRY_TERMS:
        terms = list(pol._INDUSTRY_TERMS[industry])
    else:
        for sub, mapped in _CSRC_TERMS:
            if sub in industry:
                terms = list(mapped)
                break
    # 去重保序 + 上限
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:settings.NEWS_RECALL_KEYWORD_CAP]


def _fetch_em_kw(keyword: str) -> pd.DataFrame:
    """东财按关键词检索(symbol 可传关键词,非仅代码)。抽出便于测试 mock。

    关掉 pyarrow 字符串推断以绕过 akshare 正则不兼容(与 news/policy 同处理)。
    """
    pd.set_option("future.infer_string", False)
    import akshare as ak
    return ak.stock_news_em(symbol=keyword)


def recall(keywords: list[str], cutoff: str,
           cap: int | None = None) -> list[dict]:
    """按主题关键词扩召回候选新闻(去重 + cutoff 过滤 + cap 控量)。

    每个关键词一次东财检索 → `news._parse_em` 归一 → 汇总去重(复用 `news._dedup_merge`)
    → 按 cutoff 过滤 → 倒序 → cap(默认 NEWS_RECALL_CANDIDATE_CAP)。
    单关键词失败隔离跳过。命中条目挂 `_recall_kw`(命中关键词,供上层打 source 标)。
    """
    cap = cap if cap is not None else settings.NEWS_RECALL_CANDIDATE_CAP
    per_kw: list[list[dict]] = []
    for kw in keywords:
        try:
            items = nw._parse_em(_fetch_em_kw(kw), cutoff)
            for it in items:
                it["_recall_kw"] = kw
            per_kw.append(items)
            logger.info("扩召回 [%s]:%d 条", kw, len(items))
        except Exception as e:                # noqa: BLE001 — 单关键词失败不影响其余
            logger.warning("扩召回关键词 [%s] 失败,跳过: %s", kw, e)
    merged = [it for it in nw._dedup_merge(*per_kw)
              if str(it.get("time", ""))[:10] >= cutoff]
    merged.sort(key=lambda x: x["time"], reverse=True)
    return merged[:cap]


def _relevance_instruction(name: str, industry: str | None) -> str:
    """相关性初筛指令(**中档**):留 点名本股 / 政策·管制·监管明确冲击其主营 / 直接上下游重大事件;
    滤掉 纯概念异动·涨停 / 其他个股 / 泛大盘综述。"""
    ind = f"(行业:{industry})" if industry else ""
    return (
        f"你是股票新闻相关性初筛助手。判断下面这条新闻是否与股票【{name}】{ind}相关。\n"
        f"判「相关」(满足任一即算):\n"
        f"- 新闻**明确讲这只股票本身**(公告/业绩/订单/调研/异动点名等);\n"
        f"- **政策 / 出口管制 / 监管 / 关税**类消息,且**明确冲击该股主营业务**(如「算力出口管制」"
        f"对算力股、「芯片管制」对半导体股)——**即使没点名该股也算相关**;\n"
        f"- 该股**直接上下游 / 主要客户或供应商**的重大事件,对其主营有实质影响。\n"
        f"判「不相关」:\n"
        f"- 纯**概念/板块异动、涨停、资金流**播报(未点名本股);\n"
        f"- **其他个股**消息、与本股主营无直接关系;\n"
        f"- 泛大盘/指数综述、宏观例行播报、仅同板块泛泛提及。\n"
        f"拿不准但属「政策/管制明确冲击主营」→ 判「相关」;其余拿不准 → 判「不相关」。\n"
        f"只输出「相关」或「不相关」。")


def llm_relevance_filter(candidates: list[dict], name: str,
                         industry: str | None, client=None) -> list[dict]:
    """对扩召回候选逐条 LLM 判相关性,只留「相关」(宁严)。

    - 关思考:靠 `settings.LLM_DISABLE_THINKING`(client 已在 chat 里走
      extra_body={"enable_thinking": False}),本函数不额外传参。
    - 并行 + 缓存:复用 `event._pmap`(有界线程池,按输入顺序回填)+ `event._cached_extract`
      (按 指令+文本 hash 缓存,指令含 name、文本含标题/正文 → 等价按「name+标题」缓存,
      重判命中缓存不再调 LLM)。
    - 降级:LLM 未配置且未显式传 client → 无法做宁严初筛,返回 [](不放行未筛候选);
      单条 LLM 失败 → 判「不相关」丢弃(宁严)。
    """
    if not candidates:
        return []
    if client is None:
        if not lc.is_configured():
            logger.warning("LLM 未配置,扩召回相关性初筛跳过(宁严:不放行未筛候选)")
            return []
        client = lc.get_client()
    from tools.analysis import event          # 延迟导入避免与 news 的模块级循环依赖
    instr = _relevance_instruction(name, industry)

    def _one(_i: int, it: dict) -> bool:
        text = f"标题:{it.get('title', '')}\n正文:{it.get('content', '')}"
        try:
            r = event._cached_extract(client, text, instr, _RELEVANCE_SCHEMA)
        except Exception as e:                # noqa: BLE001 — 判定失败按不相关丢(宁严)
            logger.warning("扩召回相关性 LLM 失败(判不相关): %s", str(e)[:80])
            return False
        return str(r.get("相关", "")).strip() == "相关"

    verdicts = event._pmap(_one, candidates, settings.LLM_EXTRACT_WORKERS)
    return [it for it, keep in zip(candidates, verdicts) if keep]


def recall_related(code: str, name: str, cutoff: str, client=None) -> list[dict]:
    """扩召回 + 初筛全流程:行业 → 关键词 → 召回候选 → LLM 相关性初筛 → 打 source 标。

    **保留条目原始 source**(东财文章来源),只清内部 `_recall_kw`;"扩召回"来路由 fetch_news 在 meta.source contributors 体现。返回相关条目。
    任一环节空/异常 → 返回 [](降级不崩,不污染既有三源并集)。
    """
    try:
        industry = stock_industry(code)
        kws = keywords_for(code, name, industry)
        if not kws:
            logger.info("扩召回 %s:无行业主题词(行业=%s),跳过", code, industry)
            return []
        cands = recall(kws, cutoff)
        if not cands:
            return []
        related = llm_relevance_filter(cands, name or code, industry, client=client)
        # 只清内部键,**保留条目原始 source**(东财 文章来源,如 证券时报网/央广);
        # "扩召回"这个来路由 fetch_news 在 meta.source 的 contributors 里体现,不覆盖单条 source。
        for it in cands:
            it.pop("_recall_kw", None)
        logger.info("扩召回 %s:候选 %d → 相关 %d(行业=%s)",
                    code, len(cands), len(related), industry)
        return related
    except Exception as e:                    # noqa: BLE001 — 扩召回整体降级不崩
        logger.warning("扩召回 %s 失败,降级为空: %s", code, e)
        return []
