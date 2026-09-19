"""轻工制造行业财报专家单测(锁语义)。契约合法 + 应收周转放宽 + 专属红旗边界 + 空输入不抛异常。"""
import importlib

mod = importlib.import_module("tools.analysis.financial.industry.轻工制造")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


def test_key_is_canonical_sw_name():
    assert mod.KEY == "轻工制造"
    assert isinstance(mod.NOTE, str) and mod.NOTE


def test_dimension_specs_structure():
    specs = mod.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        for name, key, lo, hi in subs:
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_receivable_turnover_relaxed():
    specs = mod.dimension_specs()
    ar = [s for s in specs["运营"] if s[1] == "应收周转天数"][0]
    assert ar[2] >= 200 and ar[2] > ar[3]


def test_weights_none_or_dict():
    assert mod.weights() is None or isinstance(mod.weights(), dict)


def test_flag_receivable_swell_hit_and_miss():
    assert "应收膨胀" in _codes(mod.extra_flags({"应收增速": 40.0, "营收增速": 10.0}, {}))
    assert "应收膨胀" not in _codes(mod.extra_flags({"应收增速": 15.0, "营收增速": 10.0}, {}))


def test_flag_material_margin_squeeze_hit_and_miss():
    assert "原材料挤压毛利" in _codes(mod.extra_flags({"毛利率同比升": -6.0}, {}))
    assert "原材料挤压毛利" not in _codes(mod.extra_flags({"毛利率同比升": -1.0}, {}))


def test_empty_inputs_no_raise():
    assert isinstance(mod.extra_flags({}, {}), list)
    assert isinstance(mod.extra_flags(None, None), list)
    assert mod.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}}) == []
