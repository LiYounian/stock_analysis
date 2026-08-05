"""portfolio.aggregate + screener 预设筛选单测(fake 记录)。"""
from tools.analysis import portfolio
from tools.screener import screen as sc


def _rec(code, sector, trend_score, rating, rev_label="无", rev_score=0,
         flow_days=0, flow_today=0.0, roe=None, pe_valid=False, events=None):
    return {
        "meta": {"code": code, "name": code, "sector": sector},
        "signals": {"trend": {"评级": rating, "得分": trend_score},
                    "reversal": {"拐点标签": rev_label, "拐点评分": rev_score},
                    "ob_os": {"结论": "中性"}},
        "fundflow": {"主力连续净流入天数": flow_days, "今日主力净流入": flow_today},
        "fundamental": {"ROE": roe},
        "valuation": {"pe_valid": pe_valid},
        "events": events or [],
    }


def _pool():
    return {
        "A": _rec("A", "半导体", 40, "偏多", "反弹启动", 60, flow_days=3, flow_today=1e8,
                  roe=2.0, pe_valid=True, events=[{"impact": "利好"}]),
        "B": _rec("B", "半导体", -50, "偏空", "超跌待反弹", 30, flow_days=0, flow_today=-1e8,
                  events=[{"impact": "利空"}]),
        "C": _rec("C", "光通信", 10, "中性", flow_days=2, flow_today=5e7, roe=0.5, pe_valid=False),
    }


def test_aggregate():
    agg = portfolio.aggregate(_pool())
    assert agg["n"] == 3
    assert agg["breadth"] == {"偏多": 1, "中性": 1, "偏空": 1}
    assert agg["sentiment_temp"] == 0            # 1 利好 - 1 利空
    # 半导体均分(40-50)/2=-5,光通信10 → 最强板块=光通信
    assert agg["hot_theme"] == "光通信"


def test_screen_presets():
    pool = _pool()
    res = sc.run_presets(pool)
    assert res["超跌反弹候选"] == ["A", "B"] or set(res["超跌反弹候选"]) == {"A", "B"}
    assert res["主力吸筹"] == ["A", "C"] or set(res["主力吸筹"]) == {"A", "C"}
    assert res["趋势强势"] == ["A"]              # 仅 A 得分>=30
    assert res["质地优不高估"] == ["A"]          # 仅 A ROE达标+PE有效
    assert res["反弹+资金共振"] == ["A"]         # AND:A 同时满足


def test_screen_sorted_by_trend():
    pool = _pool()
    hit = sc.screen(pool, [sc.f_reversal])
    # A(40) 应排在 B(-50) 前
    assert hit[0] == "A"
