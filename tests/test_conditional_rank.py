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


def test_liquidity_breaks_same_state_tie():
    """同上涨概率+同置信度(同指标状态格)→ 按成交额(流动性)破并列、高者靠前;不再由 code 序决定(命门修复)。"""
    recs = {
        "1": {**_rec("1", "A", **{"1日": _h(55.0, conf="高")}), "snapshot": {"amount_wan": 100.0}},
        "2": {**_rec("2", "B", **{"1日": _h(55.0, conf="高")}), "snapshot": {"amount_wan": 900.0}},
        "3": {**_rec("3", "C", **{"1日": _h(55.0, conf="高")}), "snapshot": {"amount_wan": 500.0}},
    }
    out = cr.conditional_rank_screen(recs, horizons=["1日"])
    assert _codes(out["排行"]["1日"]) == ["2", "3", "1"]   # 900>500>100,而非 code 序 1/2/3


def test_registered_in_registry():
    from tools.strategy import registry
    assert "指标条件化状态排序" in registry.list_strategies("选股")


# ————————————————————————— 「破下轨接飞刀」市场广度门 —————————————————————————
# 锁语义:平静日(市场破下轨广度低=个股孤立破位=真接飞刀)对破位候选降权 + 标风险,避免"抄在半山腰";
# 恐慌日(广度高=全市场超卖,反弹是真 edge)不动;breadth=None → 门关、与旧版逐字等价(向后兼容,无未来函数纯变换)。

def _rec_boll(code, name, boll, **horizons):
    """带 snapshot.布林位置 的合成 record(接飞刀门读此字段)。"""
    return {"meta": {"code": code, "name": name},
            "snapshot": {"amount_wan": 100.0, "布林位置": boll},
            "prediction": {"指标条件化预测": horizons}}


def test_knife_gate_demotes_break_low_on_calm_day():
    """平静日(广度 0.01 < 门槛 0.02):破下轨高概率票被降权(54−20=34)压到非超卖低概率票(50)之下,并标接飞刀风险。"""
    recs = {
        "A": _rec_boll("A", "破位票", "破下轨", **{"1日": _h(54.0)}),   # 超卖破位,平静日=接飞刀
        "B": _rec_boll("B", "中性票", "中性", **{"1日": _h(50.0)}),     # 非超卖,不降权
    }
    out = cr.conditional_rank_screen(recs, horizons=["1日"], breadth=0.01,
                                     knife_breadth=0.02, knife_demote=20.0)
    rows = out["排行"]["1日"]
    assert _codes(rows) == ["B", "A"]                    # A 被降权压到 B 之下(否则 54>50 应 A 在前)
    flag = {r["code"]: r["接飞刀风险"] for r in rows}
    assert flag["A"] is True and flag["B"] is False      # 只有破位票被标接飞刀
    assert out["市场广度"]["接飞刀门生效"] is True
    # 展示"上涨概率%"不被降权改写(降权只作用于排序键)
    a = next(r for r in rows if r["code"] == "A")
    assert a["上涨概率%"] == 54.0


def test_knife_gate_inactive_on_high_breadth():
    """恐慌日(广度 0.10 ≥ 门槛):破下轨=全市场超卖,反弹是真 edge → 不降权、不标风险,按概率正常排(A 在前)。"""
    recs = {
        "A": _rec_boll("A", "破位票", "破下轨", **{"1日": _h(54.0)}),
        "B": _rec_boll("B", "中性票", "中性", **{"1日": _h(50.0)}),
    }
    out = cr.conditional_rank_screen(recs, horizons=["1日"], breadth=0.10,
                                     knife_breadth=0.02, knife_demote=20.0)
    rows = out["排行"]["1日"]
    assert _codes(rows) == ["A", "B"]                    # 54 > 50,破位票保持榜首
    assert all(r["接飞刀风险"] is False for r in rows)
    assert out["市场广度"]["接飞刀门生效"] is False


def test_knife_gate_off_when_breadth_none_backward_compat():
    """breadth=None(单测/降级)→ 门整体关闭,排序与旧版逐字一致、无票被标接飞刀(向后兼容红线)。"""
    recs = {
        "A": _rec_boll("A", "破位票", "破下轨", **{"1日": _h(54.0)}),
        "B": _rec_boll("B", "中性票", "中性", **{"1日": _h(50.0)}),
    }
    out = cr.conditional_rank_screen(recs, horizons=["1日"], breadth=None)
    rows = out["排行"]["1日"]
    assert _codes(rows) == ["A", "B"]                    # 无降权,54 > 50
    assert all(r["接飞刀风险"] is False for r in rows)
    assert out["市场广度"]["破下轨占比"] is None


def test_knife_gate_only_flags_break_low_boll():
    """平静日只降权/标"破位档(破下轨)";触下轨/中性等非破位票即使概率更低也不被门碰(门只治破位接飞刀)。"""
    recs = {
        "A": _rec_boll("A", "破位票", "破下轨", **{"1日": _h(55.0)}),
        "B": _rec_boll("B", "触下轨", "触下轨", **{"1日": _h(52.0)}),
    }
    out = cr.conditional_rank_screen(recs, horizons=["1日"], breadth=0.005,
                                     knife_breadth=0.02, knife_demote=20.0,
                                     knife_boll=["破下轨"])
    rows = out["排行"]["1日"]
    flag = {r["code"]: r["接飞刀风险"] for r in rows}
    assert flag["A"] is True and flag["B"] is False      # 触下轨不算破位接飞刀
    assert _codes(rows) == ["B", "A"]                    # A 55−20=35 < B 52


def test_knife_gate_disabled_by_switch():
    """knife_on=False → 即便平静日 + 破位票也不降权、不标风险(总开关关停)。"""
    recs = {
        "A": _rec_boll("A", "破位票", "破下轨", **{"1日": _h(54.0)}),
        "B": _rec_boll("B", "中性票", "中性", **{"1日": _h(50.0)}),
    }
    out = cr.conditional_rank_screen(recs, horizons=["1日"], breadth=0.001,
                                     knife_on=False)
    rows = out["排行"]["1日"]
    assert _codes(rows) == ["A", "B"]
    assert all(r["接飞刀风险"] is False for r in rows)
