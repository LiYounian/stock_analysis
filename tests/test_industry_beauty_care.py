"""美容护理行业财报专家单测(锁语义)。契约合法 + 高毛利/高ROE校准 + 专属红旗边界 + 空输入不抛异常。"""
import importlib

mod = importlib.import_module("tools.analysis.financial.industry.美容护理")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


def test_key_is_canonical_sw_name():
    assert mod.KEY == "美容护理"
    assert isinstance(mod.NOTE, str) and mod.NOTE


def test_dimension_specs_structure():
    specs = mod.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        for name, key, lo, hi in subs:
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_high_margin_high_roe():
    """高毛利→毛利率下界与100分端上移;品牌高回报→ROE上调。"""
    specs = mod.dimension_specs()
    gm = [s for s in specs["质量"] if s[1] == "毛利率"][0]
    assert gm[2] >= 20 and gm[3] >= 65
    roe = [s for s in specs["回报"] if s[1] == "ROE"][0]
    assert roe[3] >= 20


def test_weights_none_or_dict():
    assert mod.weights() is None or isinstance(mod.weights(), dict)


def test_flag_sales_expense_hit_and_miss():
    # 销售费用/营收 = 50% > 45% → 命中
    hit = mod.extra_flags({}, {"利润表": {"销售费用": 5e8, "营业总收入": 1e9}})
    assert "销售费用率畸高" in _codes(hit)
    # 30% → 不命中
    miss = mod.extra_flags({}, {"利润表": {"销售费用": 3e8, "营业总收入": 1e9}})
    assert "销售费用率畸高" not in _codes(miss)


def test_flag_channel_stuffing_hit_and_miss():
    assert "渠道压货" in _codes(mod.extra_flags({"存货增速": 40.0, "营收增速": 5.0}, {}))
    assert "渠道压货" not in _codes(mod.extra_flags({"存货增速": 15.0, "营收增速": 8.0}, {}))


def test_empty_inputs_no_raise():
    assert isinstance(mod.extra_flags({}, {}), list)
    assert isinstance(mod.extra_flags(None, None), list)
    assert mod.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}}) == []
