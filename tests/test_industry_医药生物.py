"""医药生物行业财报专家模块单测。

锁语义(防未来 prompt/代码重写误删规则):
  - 契约:KEY 为申万一级规范名"医药生物";被 get_expert 自动发现;dimension_specs 结构合法;
    weights 返回归一 dict 且质量最重;
  - 五维区间:质量维毛利率区间上移(识别集采压缩)、健康维含商誉占净资产反向子项;
  - SKIP_FLAGS 跳过通用"商誉高企"(改用专属高严重度版),不 skip 高负债;
  - 每条专属红旗至少一个"命中/不命中"边界断言;研发过度资本化为头号红旗;
  - 空/半空输入不抛异常、返回 list。
纯函数、不触网、不读盘。
"""
import importlib

auto = importlib.import_module("tools.analysis.financial.industry.医药生物")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


# ———————————— 契约:KEY / NOTE / 自动发现 ————————————
def test_key_is_canonical_sw_name():
    assert auto.KEY == "医药生物"
    assert isinstance(auto.NOTE, str) and auto.NOTE


def test_discovered_by_get_expert():
    from tools.analysis.financial.industry import get_expert
    assert get_expert("医药生物") is auto


# ———————————— dimension_specs 结构合法 ————————————
def test_dimension_specs_structure():
    specs = auto.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        assert isinstance(dim, str) and subs
        for sub in subs:
            assert len(sub) == 4
            name, key, lo, hi = sub
            assert isinstance(name, str) and name
            assert isinstance(key, str) and key
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_dimension_specs_key_industry_adjustments():
    """医药关键调整:毛利率区间上移(高毛利本色,识别集采压缩);健康维含商誉占净资产反向子项。"""
    specs = auto.dimension_specs()
    毛利率 = [s for s in specs["质量"] if s[1] == "毛利率"][0]
    assert 毛利率[2] >= 25 and 毛利率[3] >= 60  # 区间明显上移(高于制造业)
    商誉 = [s for s in specs["健康"] if s[1] == "商誉占净资产"][0]
    assert 商誉[2] > 商誉[3]  # 反向指标(占比越低越好)


def test_weights_shape():
    w = auto.weights()
    assert isinstance(w, dict)
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert w["质量"] == max(w.values())  # 质量最重


# ———————————— SKIP_FLAGS ————————————
def test_skip_flags():
    assert isinstance(auto.SKIP_FLAGS, list)
    # 跳过通用"商誉高企"(改用专属高严重度版);不 skip 高负债(药企轻资产,交区间)
    assert "商誉高企" in auto.SKIP_FLAGS
    assert "高负债" not in auto.SKIP_FLAGS


# ———————————— 专属红旗:边界断言 ————————————
def test_flag_rd_over_capitalization_hit_and_miss():
    # 命中:开发支出/研发费用 > 1.0(头号利润质量红旗,高严重度)
    hit = auto.extra_flags({}, {"利润表": {"研发费用": 1e8},
                                "资产负债表": {"开发支出": 1.5e8}})
    assert "研发过度资本化" in _codes(hit)
    assert [f for f in hit if f["code"] == "研发过度资本化"][0]["严重度"] == "高"
    # 不命中:资本化占比低
    miss = auto.extra_flags({}, {"利润表": {"研发费用": 1e8},
                                 "资产负债表": {"开发支出": 2e7}})
    assert "研发过度资本化" not in _codes(miss)


def test_flag_centralized_procurement_hit_and_miss():
    # 命中:毛利率低于阈值(医药本应高毛利)
    assert "集采降价冲击" in _codes(auto.extra_flags({"毛利率": 20.0}, {}))
    # 不命中:高毛利健康
    assert "集采降价冲击" not in _codes(auto.extra_flags({"毛利率": 60.0}, {}))


def test_flag_receivable_risk_hit_and_miss():
    # 命中路径 A:应收/营收 偏高
    hitA = auto.extra_flags({}, {"资产负债表": {"应收账款": 6e8},
                                 "利润表": {"营业总收入": 1e9}})
    assert "应收账款风险" in _codes(hitA)
    # 命中路径 B:应收增速远超营收增速
    hitB = auto.extra_flags({"应收增速": 40.0, "营收增速": 10.0}, {})
    assert "应收账款风险" in _codes(hitB)
    # 不命中:应收占比低且增速同步
    miss = auto.extra_flags({"应收增速": 12.0, "营收增速": 10.0},
                            {"资产负债表": {"应收账款": 1e8}, "利润表": {"营业总收入": 1e9}})
    assert "应收账款风险" not in _codes(miss)


def test_flag_inventory_expiry_hit_and_miss():
    hit = auto.extra_flags({"存货增速": 40.0, "营收增速": 10.0}, {})
    assert "存货效期减值风险" in _codes(hit)
    miss = auto.extra_flags({"存货增速": 12.0, "营收增速": 10.0}, {})
    assert "存货效期减值风险" not in _codes(miss)


def test_flag_goodwill_impairment_hit_and_miss():
    # 命中:商誉/归母净资产 > 0.30(专属高严重度,替代通用商誉高企)
    hit = auto.extra_flags({}, {"资产负债表": {"商誉": 5e8, "归母股东权益": 1e9}})
    assert "商誉减值暴雷风险" in _codes(hit)
    assert [f for f in hit if f["code"] == "商誉减值暴雷风险"][0]["严重度"] == "高"
    # 不命中:商誉占比低
    miss = auto.extra_flags({}, {"资产负债表": {"商誉": 1e8, "归母股东权益": 1e9}})
    assert "商誉减值暴雷风险" not in _codes(miss)


def test_flag_cfo_deterioration_hit_and_miss():
    hit = auto.extra_flags({}, {"利润表": {"归母净利润": 5e8},
                                "现金流量表": {"经营活动现金流量净额": -3e8}})
    assert "经营现金流恶化" in _codes(hit)
    miss = auto.extra_flags({}, {"利润表": {"归母净利润": 5e8},
                                 "现金流量表": {"经营活动现金流量净额": 6e8}})
    assert "经营现金流恶化" not in _codes(miss)


# ———————————— 缺值/半空输入:不抛、返回 list ————————————
def test_extra_flags_empty_inputs_no_raise():
    assert isinstance(auto.extra_flags({}, {}), list)
    assert isinstance(auto.extra_flags(None, None), list)
    empty = auto.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}})
    assert isinstance(empty, list) and empty == []


def test_extra_flags_output_shape():
    for f in auto.extra_flags({"毛利率": 20.0, "存货增速": 40.0, "营收增速": 5.0}, {}):
        assert set(f) >= {"code", "命中", "严重度", "值"}
        assert f["严重度"] in ("高", "中", "低")
