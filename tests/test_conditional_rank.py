"""策略11 指标条件化状态排序 单测(脱离建池,用合成 record 锁排序/过滤语义)。

守约法第6条:锁"为什么这么排"——基线上涨概率%降序 + 置信度 tiebreak + 剔除退回/数据不足 + top_k + 三维度独立。
"""
from tools.strategy import conditional_rank as cr


def _h(p, conf="高", 退回=False, 方向="看涨", lvl="精确"):
    return {"上涨概率%": p, "置信度": conf, "方向": 方向, "放宽层级": lvl, "是否退回": 退回}


def _rec(code, name, **horizons):
    return {"meta": {"code": code, "name": name},
            "prediction": {"指标条件化预测": horizons}}


def _codes(rank_rows):
    return [r["code"] for r in rank_rows]


def test_rank_by_prob_desc():
    recs = {
        "1": _rec("1", "A", **{"1日": _h(55.0)}),
        "2": _rec("2", "B", **{"1日": _h(60.0)}),
        "3": _rec("3", "C", **{"1日": _h(48.0)}),
    }
    out = cr.conditional_rank_screen(recs, horizons=["1日"])
    assert _codes(out["排行"]["1日"]) == ["2", "1", "3"]   # 60 > 55 > 48
    assert out["有效样本"]["1日"] == 3


def test_confidence_tiebreak():
    recs = {
        "1": _rec("1", "A", **{"1日": _h(55.0, conf="中")}),
        "2": _rec("2", "B", **{"1日": _h(55.0, conf="高")}),
        "3": _rec("3", "C", **{"1日": _h(55.0, conf="低")}),
    }
    out = cr.conditional_rank_screen(recs, horizons=["1日"])
    assert _codes(out["排行"]["1日"]) == ["2", "1", "3"]   # 同概率 → 高>中>低


def test_drop_fallback_and_insufficient():
    recs = {
        "1": _rec("1", "A", **{"1日": _h(70.0, 退回=True)}),          # 退回 → 剔除
        "2": _rec("2", "B", **{"1日": _h(65.0, 方向="数据不足")}),    # 数据不足 → 剔除
        "3": _rec("3", "C", **{"1日": _h(52.0)}),                     # 保留
    }
    out = cr.conditional_rank_screen(recs, horizons=["1日"])
    assert _codes(out["排行"]["1日"]) == ["3"]
    assert out["跳过"]["1日"]["退回"] == 1 and out["跳过"]["1日"]["数据不足"] == 1
    # 关掉剔除 → 退回票也进(按概率排最前)
    out2 = cr.conditional_rank_screen(recs, horizons=["1日"], drop_fallback=False, drop_insufficient=False)
    assert _codes(out2["排行"]["1日"])[0] == "1"   # 70 最高


def test_top_k_cap():
    recs = {str(i): _rec(str(i), f"N{i}", **{"1日": _h(50.0 + i)}) for i in range(5)}
    out = cr.conditional_rank_screen(recs, horizons=["1日"], top_k=2)
    assert len(out["排行"]["1日"]) == 2
    assert _codes(out["排行"]["1日"]) == ["4", "3"]   # 54, 53


def test_three_horizons_independent():
    recs = {
        "1": _rec("1", "A", **{"1日": _h(60.0), "5日": _h(45.0), "10日": _h(55.0)}),
        "2": _rec("2", "B", **{"1日": _h(50.0), "5日": _h(58.0), "10日": _h(52.0)}),
    }
    out = cr.conditional_rank_screen(recs)
    assert _codes(out["排行"]["1日"]) == ["1", "2"]
    assert _codes(out["排行"]["5日"]) == ["2", "1"]
    assert _codes(out["排行"]["10日"]) == ["1", "2"]


def test_prob_floor_marker_only():
    recs = {
        "1": _rec("1", "A", **{"1日": _h(48.0)}),   # <50 → 过下限 False,但仍入排行(软标记)
        "2": _rec("2", "B", **{"1日": _h(62.0)}),
    }
    out = cr.conditional_rank_screen(recs, horizons=["1日"], prob_floor=50)
    rows = out["排行"]["1日"]
    assert len(rows) == 2                              # 软标记不硬筛
    mark = {r["code"]: r["过下限"] for r in rows}
    assert mark["2"] is True and mark["1"] is False


def test_missing_prediction_block_skipped():
    recs = {
        "1": {"meta": {"code": "1", "name": "A"}},                     # 无 prediction
        "2": _rec("2", "B", **{"1日": _h(55.0)}),
        "3": {"meta": {"code": "3"}, "prediction": {"指标条件化预测": {"error": "暂不可用"}}},
    }
    out = cr.conditional_rank_screen(recs, horizons=["1日"])
    assert _codes(out["排行"]["1日"]) == ["2"]
    assert out["跳过"]["1日"]["无预测块"] == 2


def test_registered_in_registry():
    from tools.strategy import registry
    assert "指标条件化状态排序" in registry.list_strategies("选股")
