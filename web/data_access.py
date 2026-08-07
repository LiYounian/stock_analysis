"""Web 数据访问层:经 store 只读 data/analysis / data/raw(按日期)。

Web 不做计算、不触网,只读离线 run.py 产出的数据。store 按日期分区存储,
本层把"要看哪一天"(date)透传给 store.get_*(date=...);date 缺省 "latest"=最新日期。
展示层只依赖 config + store(基座只读层),不 import 分析器。
"""
from __future__ import annotations

from tools.store import repo as store


def available_dates() -> list[str]:
    """所有可选分析日期,倒序(最新在前),供页面日期下拉。"""
    return list(reversed(store.list_dates("analysis")))


def as_of(date: str = "latest") -> str:
    """当前展示的数据日期(具体日期直接回显;latest / 非法 → 最新)。"""
    dates = store.list_dates("analysis")
    if date and date != "latest" and date in dates:
        return date
    return dates[-1] if dates else "-"


def _load_all(date: str = "latest") -> dict[str, dict]:
    """某日期(缺省最新)下全部个股中心记录 {code: rec}。"""
    return {r["meta"]["code"]: r for r in store.iter_records(date=date)}


def list_records(date: str = "latest") -> list[dict]:
    """全池记录,按趋势得分降序。"""
    recs = list(_load_all(date).values())
    recs.sort(key=lambda r: ((r.get("signals") or {}).get("trend") or {}).get("得分", -999),
              reverse=True)
    return recs


def get_record(code: str, date: str = "latest") -> dict | None:
    try:
        return store.get_record(code, date=date)
    except FileNotFoundError:
        return None


def get_kline(code: str, date: str = "latest") -> dict:
    """读预生成的 K线图表视图(analysis/<日期>/chart)。展示层只读、不算(§9.3)。"""
    try:
        return store.get_code_view("chart", code, date=date)
    except FileNotFoundError:
        return {"dates": [], "open": [], "high": [], "low": [], "close": [],
                "ma5": [], "ma20": [], "ma60": [], "volume": []}


def _name(recs, code):
    r = recs.get(code)
    return r["meta"]["name"] if r else code


def screen_page(date: str = "latest") -> dict:
    """选股页数据:读 screen 视图 + 补每票关键字段。"""
    recs = _load_all(date)
    try:
        data = store.get_view("screen", date=date)
    except FileNotFoundError:
        return {"presets": {}, "aggregate": {}, "meta": {}, "as_of": as_of(date)}
    detail = {}
    for name, codes in data.get("presets", {}).items():
        rows = []
        for c in codes:
            r = recs.get(c, {})
            rows.append({
                "code": c, "name": _name(recs, c),
                "sector": (r.get("meta") or {}).get("sector"),
                "trend": ((r.get("signals") or {}).get("trend") or {}).get("评级"),
                "tendency": ((r.get("prediction") or {}).get("买卖倾向") or {}).get("结论"),
                "flow": (r.get("fundflow") or {}).get("今日主力净流入"),
            })
        detail[name] = rows
    return {"presets": detail, "aggregate": data.get("aggregate", {}), "as_of": as_of(date)}



def pool_page(date: str = "latest") -> dict:
    """票池管理页数据:当前票池(按板块归组)+ 每票在该日期下是否已有分析数据。"""
    from tools.config import stock_pool
    recs = _load_all(date)
    rows = [{"code": s.code, "name": s.name, "industry": s.industry,
             "sector": s.sector, "has_data": s.code in recs}
            for s in stock_pool.get_pool()]
    rows.sort(key=lambda x: (x["sector"], x["code"]))
    return {"pool": rows, "count": len(rows), "as_of": as_of(date)}


def news_page(date: str = "latest") -> list[dict]:
    """新闻页数据:全池公司行为公告(利好/利空),按日期倒序。"""
    recs = _load_all(date)
    out = []
    for r in recs.values():
        for e in r.get("events", []):
            out.append({"code": r["meta"]["code"], "name": r["meta"]["name"],
                        "sector": r["meta"]["sector"], **e})
    out.sort(key=lambda x: x.get("date", ""), reverse=True)
    return out


# ————————————————————————————————————————————————
# 新闻(读统一「新闻+AI」视图 data/analysis/<日期>/news_ai/{code}.json,经 store)
# 每条:{title, time, source, url, content, ai:{方向, 强度, 与本股关系, 评论, 原因}}
# 缺 news_ai(未跑 enrich / LLM 未配置)→ 回退原始新闻,ai 置空(向后兼容不崩)。
# /news 列、个股页新闻块、详情页 共用此单一 reader,零重复逻辑。
# ————————————————————————————————————————————————
def _empty_ai() -> dict:
    """回退原始新闻时的空 ai 块(中性占位,前端可安全取 .ai.方向)。"""
    return {"方向": "中性", "强度": 0, "与本股关系": "", "评论": "", "原因": ""}


def news_list(code: str, date: str = "latest") -> list[dict]:
    """某票某日「新闻+AI」列表(时间倒序,生产时已排序)。

    优先读 news_ai 视图;缺失回退原始新闻并补空 ai。两源皆缺返回 []。
    """
    try:
        items = store.get_code_view("news_ai", code, date=date)
        if isinstance(items, list):
            return items
    except FileNotFoundError:
        pass
    try:
        raw = store.get_raw("news", code, date=date)
    except FileNotFoundError:
        return []
    if not isinstance(raw, list):
        return []
    return [{**n, "ai": _empty_ai()} for n in raw]


def news_detail(code: str, idx: int, date: str = "latest") -> dict | None:
    """某票某日第 idx 条新闻(含完整正文+来源+链接+AI 评论)。越界返回 None。"""
    items = news_list(code, date)
    if 0 <= idx < len(items):
        return items[idx]
    return None


def news_flow(date: str = "latest") -> list[dict]:
    """全市场当日新闻流:遍历全池各票新闻拍平,按时间倒序。

    每项 = {code, name, sector} + 新闻字段(title/time/source/url/content) + ai。
    """
    recs = _load_all(date)
    out: list[dict] = []
    for code, r in recs.items():
        meta = r.get("meta") or {}
        for i, item in enumerate(news_list(code, date)):
            out.append({"code": code, "name": meta.get("name", code),
                        "sector": meta.get("sector", ""), "idx": i, **item})
    out.sort(key=lambda x: x.get("time") or "", reverse=True)
    return out


def dashboard(date: str = "latest") -> dict:
    """首页聚合:板块强弱、超买超卖、拐点榜、资金流榜、买卖倾向汇总、重要公告。"""
    recs = [r for r in _load_all(date).values() if r.get("signals")]

    # 板块强弱(趋势得分均值)
    sec: dict[str, list] = {}
    for r in recs:
        sec.setdefault(r["meta"]["sector"], []).append(r["signals"]["trend"]["得分"])
    sectors = sorted(({"板块": s, "均分": round(sum(v) / len(v), 1), "只数": len(v)}
                      for s, v in sec.items()), key=lambda x: x["均分"], reverse=True)

    def _meta(r):
        return {"code": r["meta"]["code"], "name": r["meta"]["name"],
                "sector": r["meta"]["sector"]}

    # 超买超卖(共振)
    oversold = [{**_meta(r), "verdict": r["signals"]["ob_os"].get("结论")}
                for r in recs if r["signals"]["ob_os"].get("结论") == "超卖"]
    overbought = [{**_meta(r), "verdict": r["signals"]["ob_os"].get("结论")}
                  for r in recs if r["signals"]["ob_os"].get("结论") == "超买"]

    # 拐点榜
    rev = [{**_meta(r), "标签": r["signals"]["reversal"].get("拐点标签"),
            "评分": r["signals"]["reversal"].get("拐点评分", 0)}
           for r in recs if r["signals"]["reversal"].get("拐点标签", "无") != "无"]
    rev.sort(key=lambda x: x["评分"], reverse=True)

    # 资金流榜(今日主力净流入)
    flow = [{**_meta(r), "主力净流入": (r.get("fundflow") or {}).get("今日主力净流入"),
             "连续天数": (r.get("fundflow") or {}).get("主力连续净流入天数", 0)}
            for r in recs if (r.get("fundflow") or {}).get("今日主力净流入") is not None]
    flow.sort(key=lambda x: x["主力净流入"] or 0, reverse=True)

    # 买卖倾向汇总
    tend = {"偏买入": [], "偏卖出": [], "观望": []}
    for r in recs:
        t = ((r.get("prediction") or {}).get("买卖倾向") or {}).get("结论")
        if t in tend:
            tend[t].append(_meta(r))

    # 重要公告(近 25 条)
    important = {"业绩预告", "业绩快报", "增持", "减持", "回购", "合同订单",
                 "诉讼仲裁", "权益变动", "股权激励", "再融资"}
    anns = []
    for r in recs:
        for e in r.get("events", []):
            if e.get("type") in important:
                anns.append({**_meta(r), **e})
    anns.sort(key=lambda x: x.get("date", ""), reverse=True)

    return {"sectors": sectors, "oversold": oversold, "overbought": overbought,
            "reversal": rev, "flow": flow[:10], "flow_out": flow[-5:][::-1],
            "tendency": tend, "announcements": anns[:25], "as_of": as_of(date),
            "total": len(recs)}
