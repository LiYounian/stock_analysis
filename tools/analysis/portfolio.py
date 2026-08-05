"""组合聚合(组合层)。

对全池结构化记录做跨票聚合:板块强弱、市场宽度、情绪温度、主线识别。
选股筛选见 tools/screener/。契约见 docs/需求与目标.md 第 3 节。
"""
from __future__ import annotations


def _trend_score(r: dict):
    return ((r.get("signals") or {}).get("trend") or {}).get("得分")


def aggregate(records: dict[str, dict]) -> dict:
    """全池聚合。输入 {code: serialize 记录};输出组合层概览。"""
    recs = [r for r in records.values() if r.get("signals")]
    if not recs:
        return {"n": 0}

    # 板块强弱(趋势得分均值)
    sec: dict[str, list] = {}
    for r in recs:
        sec.setdefault(r["meta"]["sector"], []).append(_trend_score(r) or 0)
    sectors = sorted(({"板块": s, "均分": round(sum(v) / len(v), 1), "只数": len(v)}
                      for s, v in sec.items()), key=lambda x: x["均分"], reverse=True)

    # 市场宽度:偏多/偏空/中性 占比
    ratings = [(r["signals"]["trend"] or {}).get("评级") for r in recs]
    breadth = {k: ratings.count(k) for k in ("偏多", "中性", "偏空")}

    # 情绪温度:公告利好-利空 净值(公司行为层)
    bull = bear = 0
    for r in recs:
        for e in r.get("events", []):
            if e.get("impact") == "利好":
                bull += 1
            elif e.get("impact") == "利空":
                bear += 1
    sentiment_temp = bull - bear

    # 主线:最强板块(均分×只数占比)
    hot = sectors[0]["板块"] if sectors else None

    return {
        "n": len(recs), "sectors": sectors, "breadth": breadth,
        "sentiment_temp": sentiment_temp, "bull_events": bull, "bear_events": bear,
        "hot_theme": hot,
    }
