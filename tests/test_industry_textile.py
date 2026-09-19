"""纺织服饰行业财报专家单测(锁语义)。契约合法 + 毛利/存货区间校准 + 专属红旗边界 + 空输入不抛异常。"""
import importlib

mod = importlib.import_module("tools.analysis.financial.industry.纺织服饰")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


def test_key_is_canonical_sw_name():
    assert mod.KEY == "纺织服饰"
    assert isinstance(mod.NOTE, str) and mod.NOTE


def test_dimension_specs_structure():
    specs = mod.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        for name, key, lo, hi in subs:
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_margin_wide_inventory_tightened():
    """毛利率区间覆盖品牌/代工分化(100分端上调);存货周转天数区间收紧(命门)。"""
    specs = mod.dimension_specs()
    gm = [s for s in specs["质量"] if s[1] == "毛利率"][0]
    assert gm[3] >= 55
    inv = [s for s in specs["运营"] if s[1] == "存货周转天数"][0]
    assert inv[2] <= 300 and inv[2] > inv[3]


def test_weights_none_or_dict():
    assert mod.weights() is None or isinstance(mod.weights(), dict)


def test_flag_inventory_pileup_hit_and_miss():
    assert "存货积压" in _codes(mod.extra_flags({"存货增速": 30.0, "营收增速": 5.0}, {}))
    assert "存货积压" not in _codes(mod.extra_flags({"存货增速": 12.0, "营收增速": 8.0}, {}))


def test_flag_receivable_swell_hit_and_miss():
    assert "应收膨胀" in _codes(mod.extra_flags({"应收营收增速差": 30.0}, {}))
    assert "应收膨胀" not in _codes(mod.extra_flags({"应收营收增速差": 5.0}, {}))


def test_empty_inputs_no_raise():
    assert isinstance(mod.extra_flags({}, {}), list)
    assert isinstance(mod.extra_flags(None, None), list)
    assert mod.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}}) == []
