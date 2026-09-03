"""自选池实时盯盘(方案1 POC:本地侧,不改架构)。

后端拉全A当日 spot 快照(带短 TTL 缓存,多个轮询/刷新共享、避免频打东财),
过滤出自选池,合并当日 SEPA/VCP 收盘态形态标签。前端轮询 /api/watch 展示。

诚实边界:**行情=盘中快照(准实时,随刷新更新);形态标签=每日收盘态(非实时监控)**。
盯十几只自选票的轮询流量极小(全A spot 一次调用 + 本地过滤)。非投资建议、不自动下单。
"""
from __future__ import annotations

import time

from tools.collectors import gtimg_quote
from tools.config import stock_pool
from tools.store import repo as store

# 实时报价用腾讯 gtimg(非东财:本机对东财 spot 有 TLS 指纹墙;腾讯是 collectors 一直用的备用源)。
# 只拉自选十几只、一次批量调用,10s TTL 缓存(多标签页/多轮询共享,不频打)。
# 抓取与字段解析已上提到 `tools.collectors.gtimg_quote`(单一事实源,与盘中快照节点共用);
# 本层只留缓存与展示口径,下面两个名字保留为**薄委托**(调用方/单测的既有入口不变)。
_QUOTE_TTL_S = 10.0
_quote_cache: dict = {"ts": 0.0, "by_code": None}


def _gtimg_prefix(code: str) -> str:
    """6位A股代码 → 腾讯 gtimg 市场前缀(委托 collectors.gtimg_quote)。"""
    return gtimg_quote.market_prefix(code)


def _fetch_quotes(codes: list[str]) -> dict[str, dict]:
    """腾讯 gtimg 批量拉报价。返回 {code: {price,pct_chg,amount_wan,...}}(本页只用前三个)。"""
    return gtimg_quote.fetch_quotes(codes)


def _quotes_cached(codes: list[str]) -> dict[str, dict]:
    """自选报价,10s TTL 缓存。失败抛给上层(路由降级)。"""
    now = time.time()
    if _quote_cache["by_code"] is not None and (now - _quote_cache["ts"]) < _QUOTE_TTL_S:
        return _quote_cache["by_code"]
    by_code = _fetch_quotes(codes)
    _quote_cache["ts"] = now
    _quote_cache["by_code"] = by_code
    return by_code


def _sepa_tags_by_code(date: str = "latest") -> dict[str, list]:
    """当日 SEPA 观察池 code→标签(收盘态形态,如 VCP收缩中(收盘)/接近枢纽);缺视图→空。"""
    try:
        w = store.get_view("SEPA观察池", date=date)
    except FileNotFoundError:
        return {}
    out: dict[str, list] = {}
    for r in w.get("rows") or []:
        code = r.get("code")
        if code:
            out[code] = r.get("标签") or []
    return out


def watch_quotes(date: str = "latest") -> dict:
    """自选池实时盯盘:每只 = 名称/行业 + 实时价/涨跌幅/成交额 + 当日SEPA形态标签(收盘态)。

    返回 {rows:[...], quote_ok:bool, quote_err:str, as_of_tags:str}。
    行情拉取失败时 quote_ok=False、rows 仍给出(价格字段为 None),页面不崩。
    """
    pool = stock_pool.get_pool()
    tags_map = _sepa_tags_by_code(date)
    quote_ok, quote_err = True, ""
    q_by_code: dict[str, dict] = {}
    try:
        q_by_code = _quotes_cached([s.code for s in pool])
    except Exception as e:  # 网络/源异常 → 降级:只出名单+形态,价格留空
        quote_ok, quote_err = False, f"{type(e).__name__}: {e}"

    rows = []
    for s in pool:
        q = q_by_code.get(s.code) or {}
        price = q.get("price")
        pct = q.get("pct_chg")
        amount_wan = q.get("amount_wan")
        rows.append({
            "code": s.code,
            "name": s.name,
            "industry": s.industry or "—",
            "price": round(float(price), 2) if price is not None else None,
            "pct_chg": round(float(pct), 2) if pct is not None else None,
            "amount_wan": round(float(amount_wan), 1) if amount_wan is not None else None,
            "sepa_tags": tags_map.get(s.code) or [],
        })
    # 涨跌幅降序(盯盘习惯:强的在上);无报价的沉底
    rows.sort(key=lambda r: (r["pct_chg"] is None, -(r["pct_chg"] or 0)))
    return {"rows": rows, "quote_ok": quote_ok, "quote_err": quote_err}
