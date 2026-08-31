"""F7 单测:事件驱动专家(PEAD + 增减持/回购)。

锁语义:超预期判定、公司行为规模过滤、前瞻收益防未来函数、公告 fallback、无事件弃权、
专家产 ExpertVerdict 且进默认专家组(→ 合议默认组自动带上,web 勾选自动出现)。
"""
import pandas as pd
import pytest

from tools.analysis.event_driven import judge, summary
from tools.backtest import event_study
from tools.contracts.expert import validate_verdict


# ———————————— PEAD 超预期判定 ————————————
def test_pead_beat_positive_and_significant():
    v = judge.judge_pead(60.0)                       # 增速 60% → 近似超预期 0.6,达显著线(>0.2)
    assert v["方向"] == "看多" and v["超预期度"] > 0 and v["达显著线"] is True


def test_pead_miss_negative():
    v = judge.judge_pead(-40.0)
    assert v["方向"] == "看空" and v["超预期度"] < 0 and v["达显著线"] is True


def test_pead_below_significant_line():
    v = judge.judge_pead(5.0)                        # 增速 5% → 超预期 0.05 < 0.2 显著线
    assert v["方向"] == "看多" and v["达显著线"] is False


def test_pead_with_consensus_relative():
    # 实际 30% vs 一致预期 10% → 相对超预期 200%,显著
    v = judge.judge_pead(30.0, consensus=10.0)
    assert v["方向"] == "看多" and v["达显著线"] is True
    # 实际 8% vs 一致预期 10% → 略低于预期 → 看空
    v2 = judge.judge_pead(8.0, consensus=10.0)
    assert v2["方向"] == "看空"


def test_pead_no_data_neutral():
    assert judge.judge_pead(None)["方向"] == "中性"


# ———————————— 公司行为规模过滤 ————————————
def test_corporate_action_direction():
    assert judge.judge_corporate_action("增持", 0.05)["方向"] == "看多"
    assert judge.judge_corporate_action("回购", 0.05)["方向"] == "看多"
    assert judge.judge_corporate_action("减持", 0.05)["方向"] == "看空"


def test_corporate_action_symbolic_is_weak():
    from tools.config.strategy import THRESHOLDS
    thr = THRESHOLDS["事件驱动"]["增持规模占比门槛"]
    big = judge.judge_corporate_action("增持", thr * 5)
    small = judge.judge_corporate_action("增持", thr * 0.1)   # 象征性
    assert small["象征性"] is True and big["象征性"] is False
    assert abs(small["强度"]) < abs(big["强度"])              # 象征性信号更弱


def test_corporate_action_unknown_scale_keeps_direction():
    v = judge.judge_corporate_action("增持", None)
    assert v["方向"] == "看多" and v["强度"] > 0                # 规模未知仍给方向


# ———————————— 股权变动性质区分:协议转让给产业方 ≠ 二级抛售 ————————————
def test_classify_strategic_transfer_not_bearish():
    """协议转让给产业资本/战投(引资背书)→ 不判强看空(利好/中性)。"""
    cls = judge.classify_share_change("减持", method="协议转让",
                                      text="控股股东拟将10.47%股权协议转让给产业资本中际旭创,战略入股")
    assert cls["类别"] == "战略引资" and cls["看空有效"] is False
    v = judge.judge_corporate_action("减持", None, method="协议转让",
                                     text="拟协议转让给同业龙头,战略投资、业务协同")
    assert v["方向"] != "看空" and v["强度"] > -0.05           # 绝不是 -1.0 强看空
    assert v["类别"] == "战略引资"


def test_classify_secondary_market_sell_still_bearish():
    """集中竞价/大宗在二级市场抛售套现 → 仍看空(保留)。"""
    cls = judge.classify_share_change("减持", method="集中竞价",
                                      text="股东计划通过集中竞价方式减持不超过2%")
    assert cls["类别"] == "二级减持" and cls["看空有效"] is True
    v = judge.judge_corporate_action("减持", None, method="集中竞价交易", text="二级市场减持")
    assert v["方向"] == "看空" and v["强度"] < 0
    v2 = judge.judge_corporate_action("减持", None, method="大宗交易", text="大宗减持")
    assert v2["方向"] == "看空" and v2["强度"] < 0


def test_classify_protocol_transfer_unclear_is_neutral():
    """协议转让但受让方性质不明(识别不清)→ 中性/低置信,而非默认看空。"""
    cls = judge.classify_share_change("减持", method="协议转让", text="股份转让协议")
    assert cls["类别"] == "协议转让待定" and cls["看空有效"] is False
    v = judge.judge_corporate_action("减持", None, method="协议转让", text="")
    assert v["方向"] == "中性" and v["强度"] == 0.0


def test_classify_plain_reduction_defaults_bearish():
    """无任何方式/文本线索的减持 → 保守沿用看空(向后兼容,不误伤)。"""
    v = judge.judge_corporate_action("减持", None)
    assert v["方向"] == "看空" and v["强度"] < 0
    assert judge.classify_share_change("减持")["看空有效"] is True
    # 增持/回购不受性质区分影响
    assert judge.classify_share_change("增持")["类别"] == "常规"


# ———————————— 前瞻收益框架:防未来函数 ————————————
def _kline(prices, start="2026-01-01"):
    n = len(prices)
    return pd.DataFrame({"date": pd.date_range(start, periods=n, freq="D"),
                         "close": [float(p) for p in prices]})


def test_forward_returns_basic_and_window_bounds():
    kl = _kline([10, 11, 12, 13, 14, 15])            # 事件日 index0=10
    out = event_study.forward_returns(["2026-01-01"], kl, windows=(2, 5, 20))
    e = out[0]
    assert e["进场价"] == 10.0
    assert e["前瞻"][2] == round(12 / 10 - 1, 6)     # t0+2
    assert e["前瞻"][5] == round(15 / 10 - 1, 6)     # t0+5
    assert e["前瞻"][20] is None                      # 越界 → None,不编造


def test_forward_returns_no_lookahead_on_pre_event_change():
    """篡改事件日**之前**的价,不应改变事件后前瞻收益(只用 t0 及以后)。"""
    base = _kline([10, 11, 12, 13, 14])
    out_base = event_study.forward_returns(["2026-01-03"], base, windows=(2,))
    tampered = base.copy()
    tampered.loc[0, "close"] = 999.0                  # 改事件日(2026-01-03=index2)之前的 index0
    out_tamp = event_study.forward_returns(["2026-01-03"], tampered, windows=(2,))
    assert out_base[0]["前瞻"][2] == out_tamp[0]["前瞻"][2]


def test_forward_returns_alpha_vs_benchmark():
    stock = _kline([10, 11, 12])                      # +20% @t0+2
    bench = _kline([100, 101, 102])                   # +2% @t0+2
    out = event_study.forward_returns(["2026-01-01"], stock, windows=(2,), benchmark_df=bench)
    assert out[0]["alpha"][2] == round((12/10-1) - (102/100-1), 6)


def test_event_study_summarize():
    kl = _kline(list(range(10, 40)))
    out = event_study.forward_returns(["2026-01-01", "2026-01-05"], kl, windows=(5,))
    summ = event_study.summarize(out, windows=(5,))
    assert summ[5]["样本数"] == 2 and summ[5]["胜率"] == 1.0


# ———————————— summary:公告 fallback + 无事件弃权 ————————————
def _rec(code="000001", as_of="2026-08-08", events=None):
    return {"meta": {"code": code, "name": "测试", "as_of": as_of}, "events": events or []}


def test_summary_fallback_from_announcements(monkeypatch):
    # 断开精数值路径,只走公告粗判
    monkeypatch.setattr(summary, "_load_precise", lambda code, t, ann_text="": [])
    anns = [{"date": "2026-08-01", "type": "增持", "impact": "利好", "title": "股东增持"}]
    s = summary.summarize("000001", "2026-08-08", announcements=anns)
    assert s and s["方向"] == "看多" and s["数据充分度"] == "部分降级"


def test_summary_none_when_no_events(monkeypatch):
    monkeypatch.setattr(summary, "_load_precise", lambda code, t, ann_text="": [])
    assert summary.summarize("000001", "2026-08-08", announcements=[]) is None
    # 非事件驱动类公告也不算
    anns = [{"date": "2026-08-01", "type": "诉讼仲裁", "impact": "利空", "title": "x"}]
    assert summary.summarize("000001", "2026-08-08", announcements=anns) is None


def test_summary_stale_announcement_out_of_window(monkeypatch):
    monkeypatch.setattr(summary, "_load_precise", lambda code, t, ann_text="": [])
    anns = [{"date": "2020-01-01", "type": "增持", "impact": "利好", "title": "很久前"}]
    assert summary.summarize("000001", "2026-08-08", announcements=anns) is None


def test_summary_strategic_transfer_announcement_not_bearish(monkeypatch):
    """公告粗判:协议转让给产业方 → 不判看空(合议不再被错误拖空)。"""
    monkeypatch.setattr(summary, "_load_precise", lambda code, t, ann_text="": [])
    anns = [{"date": "2026-08-05", "type": "减持",
             "title": "控股股东协议转让10.47%股权引入战略投资者中际旭创",
             "summary": "产业资本战略入股,业务协同"}]
    s = summary.summarize("300684", "2026-08-08", announcements=anns)
    assert s and s["方向"] != "看空"                 # 关键:不再被误判为看空


def test_summary_secondary_reduction_announcement_bearish(monkeypatch):
    """公告粗判:集中竞价二级减持 → 仍看空。"""
    monkeypatch.setattr(summary, "_load_precise", lambda code, t, ann_text="": [])
    anns = [{"date": "2026-08-05", "type": "减持",
             "title": "股东拟集中竞价减持不超过2%", "summary": "二级市场竞价交易"}]
    s = summary.summarize("000001", "2026-08-08", announcements=anns)
    assert s and s["方向"] == "看空"


def test_summary_zhongshi_precise_reduction_flips_from_strong_bearish(monkeypatch):
    """中石科技300684 真实误判回归:6 条 ggcg 减持 + 协议转让/战投公告文本。

    改前:6×(-0.4) 累加 clamp 到 -1.0 强看空(充分置信);
    改后:公告文本辨出"战略引资",事件驱动分从 -1.0 变为更中性/偏正、且低置信。
    """
    import pandas as _pd
    from tools.collectors import event_driven as col
    fake = _pd.DataFrame([{"code": "300684", "方向": "减持", "变动股数": 1e6,
                           "方式": None, "日期": "2026-08-01"} for _ in range(6)])
    monkeypatch.setattr(col, "load_insider_trades", lambda tag="latest": fake)
    # 断开业绩精数值路径(避免触网),只留 ggcg
    monkeypatch.setattr(summary, "_quarter_ends_before", lambda a, b: [])
    anns = [{"date": "2026-08-02", "type": "减持",
             "title": "实控人拟协议转让10.47%股权给中际旭创,引入产业战略投资者",
             "summary": "光模块龙头战略入股、业务协同"}]
    s = summary.summarize("300684", "2026-08-08", announcements=anns)
    assert s is not None
    assert s["强度"] > -0.5                           # 不再是 -1.0 强看空
    assert s["方向"] != "看空"                        # 偏正/中性
    assert s["数据充分度"] == "部分降级"              # 识别性质靠文本 → 诚实低置信


def test_summary_precise_overrides_and_significant(monkeypatch):
    precise = [{"来源": "yjyg", "方向": "看多", "强度": 0.6, "达显著线": True, "依据": "增速60%", "类别": "业绩"}]
    monkeypatch.setattr(summary, "_load_precise", lambda code, t, ann_text="": precise)
    s = summary.summarize("000001", "2026-08-08", announcements=[])
    assert s["方向"] == "看多" and s["数据充分度"] == "充分" and s["置信度"] == 1.0


# ———————————— 专家:产 ExpertVerdict / 弃权 / 进默认组 ————————————
def test_expert_abstains_when_no_events(monkeypatch):
    from tools.analysis import experts as ex
    monkeypatch.setattr(summary, "_load_precise", lambda code, t, ann_text="": [])
    v = ex.build("事件驱动", _rec(events=[]))
    assert v.方向 == "中性" and v.置信度 == 0.0 and v.数据充分度 == "缺失"
    assert validate_verdict(v) == []


def test_expert_produces_verdict_from_announcement(monkeypatch):
    from tools.analysis import experts as ex
    monkeypatch.setattr(summary, "_load_precise", lambda code, t, ann_text="": [])
    anns = [{"date": "2026-08-05", "type": "回购", "impact": "利好", "title": "公司回购"}]
    v = ex.build("事件驱动", _rec(events=anns))
    assert v.方向 == "看多" and v.强度 > 0 and validate_verdict(v) == []


def test_event_expert_in_default_group_and_council_uses_it():
    from tools.config.strategy import THRESHOLDS
    from tools.analysis import council
    assert "事件驱动" in THRESHOLDS["合议"]["默认专家组"]
    assert "事件驱动" in THRESHOLDS["合议"]["默认权重"]
    # 合议默认组会纳入事件驱动(参与专家里出现)
    r = council.convene_default(_rec(events=[{"date": "2026-08-05", "type": "增持",
                                              "impact": "利好", "title": "增持"}]))
    assert "事件驱动" in r["参与专家"]


def test_event_expert_does_not_break_bias_equivalence():
    """F7 加专家进默认组,不得破坏 F4 买卖倾向等价(bias_council 用独立硬编码 5 因子)。"""
    from tools.analysis import predict as pr
    tech = {"ob_os": {"结论": "超卖"}, "reversal": {"拐点标签": "反弹启动"}, "signal": {"评级": "偏多"}}
    assert pr.bias_recommendation(tech, None, None) == pr.bias_recommendation_council(tech, None, None)
