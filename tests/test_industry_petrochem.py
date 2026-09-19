"""石油石化行业财报专家单测(锁语义)。契约合法 + 高负债区间校准 + 专属红旗边界 + 空输入不抛异常。"""
import importlib

mod = importlib.import_module("tools.analysis.financial.industry.石油石化")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


def test_key_is_canonical_sw_name():
    assert mod.KEY == "石油石化"
    assert isinstance(mod.NOTE, str) and mod.NOTE


def test_dimension_specs_structure():
    specs = mod.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        for name, key, lo, hi in subs:
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_leverage_relaxed():
    specs = mod.dimension_specs()
    dar = [s for s in specs["健康"] if s[1] == "资产负债率"][0]
    assert dar[2] > dar[3] and dar[3] >= 40


def test_weights_none_or_dict():
    assert mod.weights() is None or isinstance(mod.weights(), dict)


def test_flag_crack_spread_narrow_hit_and_miss():
    assert "炼化价差收窄" in _codes(mod.extra_flags({"毛利率同比升": -6.0}, {}))
    assert "炼化价差收窄" not in _codes(mod.extra_flags({"毛利率同比升": -1.0}, {}))


def test_flag_inventory_writedown_hit_and_miss():
    # 存货增速远超营收 + 毛利率走弱 → 命中
    assert "存货跌价风险" in _codes(mod.extra_flags({"存货增速": 45.0, "营收增速": 5.0, "毛利率同比升": -2.0}, {}))
    # 毛利率未走弱 → 不命中(区分正常备货)
    assert "存货跌价风险" not in _codes(mod.extra_flags({"存货增速": 45.0, "营收增速": 5.0, "毛利率同比升": 1.0}, {}))


def test_empty_inputs_no_raise():
    assert isinstance(mod.extra_flags({}, {}), list)
    assert isinstance(mod.extra_flags(None, None), list)
    assert mod.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}}) == []
