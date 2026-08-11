"""政策采集:宏观/行业政策(国内 + 国外)。

情绪三层中的「政策」层。**本轮只负责采集 + 归并 + 落盘 + 按票池行业关键词命中打标**;
情绪判定(受影响行业/方向/强度)是后续 LLM 的事,不在此。

数据源(多源 fallback,消除单点故障):
  - 主源 东财 `stock_news_em(symbol=<关键词>)`——按关键词返回结构化条目
    (标题/内容/时间/来源/链接),与 news.py 同源可复用。
  - 备源 `news_cctv`(新闻联播,偏国内宏观)——主源(按全部关键词检索)失败或返回空时
    回落,拉近日联播条目归一成同款契约,再走同一套 region/行业打标 + 去重 + 落盘链路。
  pandas 3.0 默认 pyarrow 字符串会触发 akshare 内部正则报错,调用前关掉
  `future.infer_string`(实测可绕过)。

若上层(Claude 主控 WebSearch)已拿到原始检索结果,可直接调 `tag_and_dump(raw_items)`
走"归一 + 命中打标 + 落盘"链路,绕开 akshare 取数。

落盘:经 store 层写 policy kind(物理落 data/raw/policy/policy_{date}.json)。policy 按
日期聚合(非按 code),故 code 槽传 `policy_{date}`;meta.source 记实际命中源(eastmoney / cctv)。
契约:每条 {date, title, source, url, region(国内/国外), summary, industries, keyword}
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from tools.config import settings, stock_pool
from tools.store import repo as store

logger = logging.getLogger("collectors.policy")

# —— 行业关键词种子(命中打标 + 生成检索词共用):sector -> 触发词 ——
# key 与 stock_pool.Stock.sector 对齐,便于把政策映射回票池板块。
_INDUSTRY_TERMS: dict[str, list[str]] = {
    "半导体": ["半导体", "芯片", "集成电路", "晶圆", "存储", "光刻", "先进制程"],
    "电子元件": ["电子元件", "PCB", "MLCC", "被动元件", "覆铜板"],
    "机器人/自动化": ["机器人", "人形机器人", "自动化", "减速器", "伺服"],
    "光通信": ["光通信", "光模块", "光纤", "光器件"],
    "AI算力": ["算力", "数据中心", "AI芯片", "服务器", "液冷", "智算"],
    "消费电子": ["消费电子", "VR", "AR", "可穿戴", "声学"],
    "新能源材料": ["新能源", "锂电", "稀土", "永磁", "正极材料"],
    "公用事业": ["电力", "电价", "火电", "水电", "上网电价"],
}

# —— 通用政策/宏观词(与行业词组合成检索关键词)——
_POLICY_TERMS = ["政策", "补贴", "规划", "出口管制", "国产替代", "专项"]

# —— 宏观独立检索词(不与行业组合,单独成词)——
# 含 AI 芯片/算力出口管制类(BIS/英伟达/实体清单/海关),覆盖"中国AI企业获取高端算力"这类
# 急跌催化——此前个股 feed 常漏,补进政策池按行业命中打标。
_MACRO_TERMS = [
    "美联储", "关税", "出口管制", "半导体出口", "央行", "证监会", "发改委", "降准降息",
    "BIS", "英伟达", "AI芯片", "算力出口", "实体清单", "芯片管制", "海关", "商务部",
]

# —— region 判定:命中任一「国外标记」→ 国外,否则 国内 ——
_FOREIGN_MARKERS = [
    "美联储", "美国", "美方", "白宫", "特朗普", "拜登", "欧盟", "欧洲", "日本", "韩国",
    "英伟达", "台积电", "BIS", "商务部工业与安全局", "加息", "降息预期", "非农", "CPI",
]


def _policy_code(date: str) -> str:
    """store 的 code 槽:policy 按日期聚合,以 policy_{date} 作单文件键。"""
    return f"policy_{date}"


def default_keywords() -> list[str]:
    """基于股票池行业生成默认政策检索关键词。

    = 行业词 × 通用政策词(如「半导体 出口管制」「机器人 补贴」)
      + 宏观独立词(美联储/关税/央行…)。去重后返回。
    行业词只取每个板块的首个代表词,避免关键词爆炸(每词一次网络请求)。
    """
    pool_sectors = {s.sector for s in stock_pool.get_pool()}
    combos: list[str] = []
    for sector, terms in _INDUSTRY_TERMS.items():
        if sector not in pool_sectors or not terms:
            continue
        head = terms[0]                       # 代表词,如「半导体」「机器人」
        for pol in _POLICY_TERMS:
            combos.append(f"{head} {pol}")
    # 去重保序:行业组合词在前,宏观独立词在后
    seen: set[str] = set()
    out: list[str] = []
    for kw in combos + _MACRO_TERMS:
        if kw not in seen:
            seen.add(kw)
            out.append(kw)
    return out


def _classify_region(text: str) -> str:
    """按关键词粗判国内/国外。含外国主体标记→国外,否则国内。

    注:「关税/出口管制」既可能是中方对美、也可能是美方对华,单凭词不足以定向,
    因此靠是否出现「美国/美联储/白宫…」等外国主体标记来判,是启发式非精确。
    """
    return "国外" if any(m in text for m in _FOREIGN_MARKERS) else "国内"


def _match_industries(text: str) -> list[str]:
    """返回文本命中的票池板块列表(按 _INDUSTRY_TERMS 触发词)。"""
    hit: list[str] = []
    for sector, terms in _INDUSTRY_TERMS.items():
        if any(t in text for t in terms):
            hit.append(sector)
    return hit


def _normalize(row: dict, keyword: str) -> dict:
    """把一条原始检索结果(东财列名或已归一)归一到政策契约。"""
    title = str(row.get("title") or row.get("新闻标题") or "").strip()
    content = str(row.get("content") or row.get("summary") or row.get("新闻内容") or "")
    ts = str(row.get("time") or row.get("date") or row.get("发布时间") or "")
    src = str(row.get("source") or row.get("文章来源") or "").strip()
    url = str(row.get("url") or row.get("新闻链接") or "").strip()
    text = f"{title} {content}"
    return {
        "date": ts[:10],
        "title": title,
        "source": src,
        "url": url,
        "region": _classify_region(text),
        "summary": content.strip()[:200],
        "industries": _match_industries(text),
        "keyword": keyword,
    }


def tag_and_dump(raw_items: list[dict], days: int | None = None,
                 require_industry_hit: bool = True,
                 source: str = "eastmoney") -> list[dict]:
    """归一 + region/行业命中打标 + 时间窗过滤 + 去重 + 落盘(经 store 层)。

    取数与打标解耦:上层(WebSearch/akshare/mock)拿到原始条目后交此函数落库。
    raw_items 每条可为东财列名({新闻标题,新闻内容,发布时间,文章来源,新闻链接,关键词})
    或已归一键名({title,content,time,source,url,keyword})。
    require_industry_hit=True 时只保留命中票池行业的政策条目。
    source:实际命中的数据源名(eastmoney / cctv),写入 store 采集元数据 meta.source。
    """
    days = days or settings.NEWS_LOOKBACK_DAYS
    cutoff = (pd.Timestamp.today() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")

    seen: set[str] = set()          # url 优先,退化用 title 去重
    out: list[dict] = []
    for row in raw_items:
        rec = _normalize(row, str(row.get("keyword") or row.get("关键词") or ""))
        if not rec["title"]:
            continue
        if rec["date"] and rec["date"] < cutoff:      # 超窗丢弃
            continue
        if require_industry_hit and not rec["industries"]:
            continue
        dedup_key = rec["url"] or rec["title"]
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        out.append(rec)

    out.sort(key=lambda x: x["date"], reverse=True)
    today = pd.Timestamp.today().strftime("%Y-%m-%d")
    store.put_raw("policy", _policy_code(today), out, meta={"source": source})
    logger.info("政策 %s:入库 %d 条(去重后,源=%s)", today, len(out), source)
    return out


def _fetch_em(keyword: str) -> pd.DataFrame:
    """东财按关键词检索新闻。关掉 pyarrow 字符串推断以绕过 akshare 正则不兼容。

    抽成独立函数便于测试 mock。
    """
    pd.set_option("future.infer_string", False)
    import akshare as ak
    return ak.stock_news_em(symbol=keyword)


def _fetch_cctv(date: str) -> pd.DataFrame:
    """新闻联播文字稿(备源,偏国内宏观)。date 为 YYYYMMDD 串。

    返回列 {date(YYYYMMDD), title, content}。抽成独立函数便于测试 mock。
    同样关掉 pyarrow 字符串推断绕过 akshare 正则不兼容。
    """
    pd.set_option("future.infer_string", False)
    import akshare as ak
    return ak.news_cctv(date=date)


def _collect_cctv(days: int) -> list[dict]:
    """备源:逐日拉近 days 天新闻联播,归一成东财同款键名交给 tag_and_dump。

    news_cctv 列 {date,title,content} → 映射 {title,content,time,source,url,keyword}:
    url 联播无链接置空;time 用请求日期;source 记「新闻联播」;region 由打标默认「国内」。
    联播按日更新,逐日期请求;单日失败/空跳过,不中断整批。
    """
    raw: list[dict] = []
    today = pd.Timestamp.today()
    for i in range(max(days, 1)):
        d = today - pd.Timedelta(days=i)
        ds = d.strftime("%Y%m%d")
        try:
            df = _fetch_cctv(ds)
        except Exception as e:
            logger.error("联播备源 [%s] 失败: %s", ds, e)
            time.sleep(settings.FETCH_SLEEP_SEC)
            continue
        n = 0 if df is None else len(df)
        if n:
            iso = d.strftime("%Y-%m-%d")
            for _, r in df.iterrows():
                raw.append({
                    "title": str(r.get("title") or "").strip(),
                    "content": str(r.get("content") or "").strip(),
                    "time": iso,
                    "source": "新闻联播",
                    "url": "",
                    "keyword": "新闻联播",
                })
        logger.info("联播备源 [%s]:%d 条", ds, n)
        time.sleep(settings.FETCH_SLEEP_SEC)
    return raw


def fetch_policy(keywords: list[str] | None = None, days: int | None = None) -> list[dict]:
    """按关键词检索近 days 天政策/宏观新闻,归并 + 行业命中打标 + 落盘。

    输入:keywords 行业+政策关键词(缺省用 default_keywords());days 回看窗口。
    输出:[{date, title, source, url, region, summary, industries, keyword}, ...]。
    机制:逐关键词调东财 stock_news_em → 汇总;主源全失败/拿到空时回落新闻联播备源
    → 交 tag_and_dump 归一/打标/去重/落盘。meta.source 记实际命中源(eastmoney / cctv)。
    单关键词失败记 logger 跳过,不中断整批;两源均无结果才抛错不静默。
    """
    keywords = keywords or default_keywords()
    days = days or settings.NEWS_LOOKBACK_DAYS
    raw_items: list[dict] = []
    failed: list[str] = []
    for kw in keywords:
        try:
            df = _fetch_em(kw)
            if df is not None and len(df):
                for _, r in df.iterrows():
                    d = r.to_dict()
                    d["keyword"] = kw
                    raw_items.append(d)
            logger.info("政策检索 [%s]:%d 条", kw, 0 if df is None else len(df))
        except Exception as e:
            failed.append(kw)
            logger.error("政策检索 [%s] 失败: %s", kw, e)
        time.sleep(settings.FETCH_SLEEP_SEC)

    if failed:
        logger.warning("政策检索失败(%d): %s", len(failed), failed)

    source = "eastmoney"
    if not raw_items:
        # 主源(东财)按全部关键词检索失败或返回空 → 回落新闻联播备源。
        logger.warning("政策主源(东财)拉到空/全失败,回落新闻联播备源")
        raw_items = _collect_cctv(days)
        source = "cctv"

    if not raw_items:
        # 数据源无 SLA:两源皆空/被墙 → 降级为空(仍落空盘,保证下游 load_policy 不缺文件),
        # 绝不 raise 中止整条流水线(政策层此时降级,情绪的政策层为空)。
        logger.warning("政策采集两源均无结果(东财+联播,疑被墙/接口异常),降级为空,不中止流水线")
        return tag_and_dump([], days=days, source="none")
    return tag_and_dump(raw_items, days=days, source=source)


def load_policy(date: str | None = None) -> list[dict]:
    """读某日政策缓存(缺省今日,经 store 层)。缓存缺失抛 FileNotFoundError。"""
    date = date or pd.Timestamp.today().strftime("%Y-%m-%d")
    return store.get_raw("policy", _policy_code(date))
