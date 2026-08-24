"""新闻采集(个股新闻)。

**个股维度并集多源**(提升召回,消单点):
- **主源**:akshare `stock_news_em`(东财,个股维度)。pandas 3.0 默认 pyarrow
  字符串会触发 akshare 内部正则报错,调用前关掉 `future.infer_string`(实测可绕过)。
- **新增源(并入,非替换)**:新浪财经个股新闻页 `vCB_AllNewsStock.php`(个股维度、
  SSR HTML,curl_cffi 伪装 chrome 可直取,与 UGC/资金流同套指纹)。东财对部分票近日
  条目稀疏(实测有票只返 10 条、最新停在数日前),新浪对同一票能覆盖到当日,二者互补。
  两源各拉一遍 → 去重合并(url 优先,无 url 则 title+日期)→ 统一时间窗过滤 → 倒序。
- **财联社电报 `stock_info_global_cls`(全市场快讯,按名过滤)**:改为**总是查并入**(不再
  只在两源皆空时兜底)——管制/宏观/突发类快讯常不进东财/新浪个股 feed,财联社能补盲区。
- **扩召回(可选,fetch_news(recall=True))**:按**行业主题词**召回未挂到个股的行业/宏观/管制
  消息 → LLM 宁严相关性初筛只留直接相关 → 并入(见 collectors.news_recall)。仅调用方开启时生效。
三/四源正交去重合并,meta.source 记实际贡献源(如 "eastmoney+新浪+财联社电报+扩召回")。
落盘:走 store 层(kind="news",json,原始新闻保留供 L1 抽取)。
契约见 docs/计划/P2C_新闻情绪LLM.md。
"""
from __future__ import annotations

import logging
import os
import re
import time

import pandas as pd

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.news")

_SOURCE = "eastmoney"        # 东财(主源,个股维度)
_SOURCE_SINA = "新浪"         # 新增源(新浪个股新闻页,个股维度,并入)
_SOURCE_CLS = "财联社电报"    # 备源(全市场快讯,按名过滤)
_COL_MAP = {"新闻标题": "title", "新闻内容": "content", "发布时间": "time",
            "文章来源": "source", "新闻链接": "url"}

# 新浪个股新闻页(SSR,gb2312;curl_cffi 伪装 chrome 可直取,与 UGC 同套指纹)
_SINA_URL = ("https://vip.stock.finance.sina.com.cn/corp/view/"
             "vCB_AllNewsStock.php?symbol={sym}&Page={page}")
_SINA_MAX_PAGES = 3          # 每票最多翻几页(7 天窗内一般 1~2 页即够,遇更早页早停)
_SINA_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", "10"))
# 页面把条目内联在 <div class="datelist"><ul>...</ul>;每条 "YYYY-MM-DD HH:MM <a href>title</a>"
_SINA_LIST_RE = re.compile(r'datelist"><ul>(.*?)</ul>', re.S)
_SINA_ENTRY_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*"
    r"<a[^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", re.S)


def _fetch_em(code: str) -> pd.DataFrame:
    """东财个股新闻。关掉 pyarrow 字符串推断以绕过 akshare 正则不兼容。"""
    pd.set_option("future.infer_string", False)
    import akshare as ak
    return ak.stock_news_em(symbol=code)


def _parse_em(df: pd.DataFrame, cutoff: str) -> list[dict]:
    """东财 df → 归一新闻条目(列映射 + 时间窗过滤 + 倒序)。"""
    items: list[dict] = []
    if df is not None and len(df):
        df = df.rename(columns=_COL_MAP)
        for _, r in df.iterrows():
            t = str(r.get("time", ""))
            if t[:10] < cutoff:      # 超窗丢弃
                continue
            items.append({"title": str(r.get("title", "")),
                          "content": str(r.get("content", ""))[:2000],
                          "time": t, "source": str(r.get("source", "")),
                          "url": str(r.get("url", ""))})
        items.sort(key=lambda x: x["time"], reverse=True)
    return items


def _sina_sym(code: str) -> str:
    """A 股代码 → 新浪 symbol 前缀:6 开头沪市 sh,其余深市 sz。"""
    return ("sh" if code.startswith("6") else "sz") + code


def _fetch_sina_html(sym: str, page: int) -> str:
    """curl_cffi 伪装 chrome 拉新浪个股新闻页(gb2312 解码)。抽出便于测试 mock。"""
    from curl_cffi import requests as creq

    r = creq.get(_SINA_URL.format(sym=sym, page=page),
                 impersonate="chrome", timeout=_SINA_TIMEOUT)
    r.raise_for_status()
    return r.content.decode("gb2312", "ignore")


def _parse_sina(html: str) -> list[dict]:
    """新浪个股新闻页 HTML → 归一新闻条目(未过滤时间窗;调用方统一过滤)。

    列表页仅给标题/链接/时间(无正文),content 回落标题保证下游有文本;
    source 统一记 `_SOURCE_SINA`(逐条出处需进详情页,成本高,不取)。
    """
    html = (html or "").replace("&nbsp;", " ")   # 页面用 &nbsp; 分隔日期/时间/锚点
    m = _SINA_LIST_RE.search(html)
    block = m.group(1) if m else html
    items: list[dict] = []
    for d, hm, url, raw_title in _SINA_ENTRY_RE.findall(block):
        title = re.sub(r"<.*?>", "", raw_title).strip()
        if not title:
            continue
        items.append({"title": title, "content": title[:2000],
                      "time": f"{d} {hm}:00", "source": _SOURCE_SINA,
                      "url": url.strip()})
    return items


def _fetch_sina(code: str, cutoff: str) -> list[dict]:
    """新浪个股新闻(个股维度,并入源)。翻页直到该页最早条目早于 cutoff 或到页上限。

    单页解析空即停(无更多);已早于窗口的页停翻。返回时间窗内条目(倒序由上层统一排)。
    """
    sym = _sina_sym(code)
    out: list[dict] = []
    for page in range(1, _SINA_MAX_PAGES + 1):
        items = _parse_sina(_fetch_sina_html(sym, page))
        if not items:
            break
        out.extend(it for it in items if it["time"][:10] >= cutoff)
        if min(it["time"][:10] for it in items) < cutoff:  # 本页已跨过窗口下界
            break
    return out


def _dedup_merge(*sources: list[dict]) -> list[dict]:
    """并集去重:同 url 视为同一条;无 url 时按 title+日期(time[:10])。先到者留。

    保序按传入顺序(主源在前),同键后到者丢弃。统一倒序由调用方做。
    """
    seen: set = set()
    merged: list[dict] = []
    for items in sources:
        for it in items:
            url = (it.get("url") or "").strip()
            key = url if url else (it.get("title", ""), str(it.get("time", ""))[:10])
            if key in seen:
                continue
            seen.add(key)
            merged.append(it)
    return merged


def _fetch_cls(code: str, cutoff: str) -> list[dict]:
    """备源:财联社电报(全市场)按股票名过滤成个股新闻。

    电报无个股维度,只能按 stock_pool 里的股票名做子串命中(召回低,属降级)。
    命中条目归一成新闻契约(source=财联社电报,url 缺置空)。取不到名/无命中返回 []。
    """
    from tools.config import stock_pool
    s = stock_pool.get(code)
    name = s.name if s else ""
    if not name:
        return []
    pd.set_option("future.infer_string", False)
    import akshare as ak
    df = ak.stock_info_global_cls()
    items: list[dict] = []
    if df is not None and len(df):
        for _, r in df.iterrows():
            title = str(r.get("标题", ""))
            content = str(r.get("内容", ""))
            if name not in title and name not in content:
                continue
            d = str(r.get("发布日期", ""))
            if d and d < cutoff:
                continue
            items.append({"title": title or content[:30],
                          "content": content[:2000],
                          "time": f"{d} {str(r.get('发布时间', ''))}".strip(),
                          "source": _SOURCE_CLS, "url": ""})
        items.sort(key=lambda x: x["time"], reverse=True)
    return items


def fetch_news(codes: list[str], days: int | None = None,
               recall: bool | None = None) -> dict[str, list[dict]]:
    """拉取每票近 days 天新闻并落盘。

    输出:{code: [{title, content, time, source, url}, ...]}(按时间倒序)。
    单票失败记 logger 跳过,不中断整批。

    recall:是否开启**新闻扩召回 + LLM 相关性初筛**(见 collectors.news_recall)。
      None → 取 settings.NEWS_RECALL_ENABLED(默认 False);True/False 显式覆盖。
      为 True 时,每票在东财/新浪/财联社三源并集之外,再按**行业主题词**扩召回未挂到个股
      的行业/宏观/管制类消息 → LLM 宁严初筛只留直接相关 → 并入(_dedup_merge 去重)一起落盘。
      **只在调用方显式开启时生效**——范围天然限于调用方传入的这批票(如 screenall/pool 的
      llm_subset≈选出并集∪自选),不波及全A。扩召回失败/空则降级(仅原三源,不崩)。
    """
    settings.ensure_dirs()
    days = days or settings.NEWS_LOOKBACK_DAYS
    recall = settings.NEWS_RECALL_ENABLED if recall is None else recall
    cutoff = (pd.Timestamp.today() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")

    out: dict[str, list[dict]] = {}
    failed: list[str] = []
    n = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] 新闻 %s 采集...", i, n, code)
        from tools.config import stock_pool as _sp
        is_hk = _sp.is_hk(code)
        contributors: list[str] = []
        em_items: list[dict] = []
        sina_items: list[dict] = []
        err = None
        # 个股维度并集:东财 + 新浪各拉一遍,单源失败隔离不影响其余
        try:
            em_items = _parse_em(_fetch_em(code), cutoff)
            if em_items:
                contributors.append(_SOURCE)
        except Exception as e:
            err = e
            logger.warning("新闻 %s 东财失败: %s", code, e)
        if not is_hk:
            # 新浪个股新闻页只支持 A 股代码格式,港股跳过
            try:
                sina_items = _fetch_sina(code, cutoff)
                if sina_items:
                    contributors.append(_SOURCE_SINA)
            except Exception as e:
                err = err or e
                logger.warning("新闻 %s 新浪失败: %s", code, e)
        # 财联社电报(全市场快讯按名过滤)——改为**总是查并入**(不再只在个股源空时兜底):
        # 管制/宏观/突发类快讯常不进东财/新浪个股 feed,财联社快讯能补这类盲区。
        cls_items: list[dict] = []
        try:
            cls_items = _fetch_cls(code, cutoff)
            if cls_items:
                contributors.append(_SOURCE_CLS)
        except Exception as e:
            logger.warning("新闻 %s 财联社失败: %s", code, e)
            err = err or e
        # 扩召回(可选,仅 recall=True):按行业主题词补召回 + LLM 宁严初筛,并入三源之后
        # (三源为主,先到者留 → 扩召回只补三源未覆盖的行业/宏观消息)。降级不崩,失败返 []。
        recall_items: list[dict] = []
        if recall:
            from tools.collectors import news_recall     # 延迟导入:避免与 news_recall 的模块级循环依赖
            from tools.config import stock_pool
            s = stock_pool.get(code)
            recall_items = news_recall.recall_related(code, s.name if s else "", cutoff)
            if recall_items:
                contributors.append("扩召回")
        # 多源去重合并(东财→新浪→财联社→扩召回,先到者留)→ 统一按 cutoff 过滤 → 倒序
        items = [it for it in _dedup_merge(em_items, sina_items, cls_items, recall_items)
                 if str(it.get("time", ""))[:10] >= cutoff]
        items.sort(key=lambda x: x["time"], reverse=True)
        src = "+".join(contributors) if contributors else _SOURCE
        store.put_raw("news", code, items, meta={"source": src})
        out[code] = items
        if err and not items:                    # 各源皆挂且无数据才算失败(不静默)
            failed.append(code)
        logger.info("新闻 %s:%d 条(源=%s)", code, len(items), src)
        time.sleep(settings.FETCH_SLEEP_SEC)
    if failed:
        logger.warning("新闻拉取失败(%d): %s", len(failed), failed)
    return out


def load_news(code: str, date: str | None = None) -> list[dict]:
    """从本地缓存读单票新闻。缓存缺失抛 FileNotFoundError。

    date:缺省 None → "latest"(向后兼容);显式传日期即锁定读该日分区(不回退,缺则抛)。
    情绪引擎的 date-pin + 可识别回退走 store.get_raw_resolved,此处只做简单透传。
    """
    return store.get_raw("news", code, date=date or "latest")
