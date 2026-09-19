"""社会服务行业财报专家单测(锁语义)。契约合法 + 高毛利/弹性区间校准 + 专属红旗边界 + 空输入不抛异常。"""
import importlib

mod = importlib.import_module("tools.analysis.financial.industry.社会服务")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


def test_key_is_canonical_sw_name():
    assert mod.KEY == "社会服务"
    assert isinstance(mod.NOTE, str) and mod.NOTE


def test_dimension_specs_structure():
    specs = mod.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        for name, key, lo, hi in subs:
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_high_margin_cash_center():
    """服务业高毛利→毛利率100分端上调;预收沉淀→现金含量要求上调。"""
    specs = mod.dimension_specs()
    gm = [s for s in specs["质量"] if s[1] == "毛利率"][0]
    assert gm[3] >= 50
    cash = [s for s in specs["质量"] if s[1] == "现金含量_CFO比净利"][0]
    assert cash[3] >= 1.5


def test_weights_none_or_dict():
    assert mod.weights() is None or isinstance(mod.weights(), dict)


def test_flag_booking_weakness_hit_and_miss():
    assert "预订景气转弱" in _codes(mod.extra_flags({"合同负债环比": -20.0}, {}))
    assert "预订景气转弱" not in _codes(mod.extra_flags({"合同负债环比": -3.0}, {}))


def test_flag_cfo_deteriorate_hit_and_miss():
    hit = mod.extra_flags({}, {"利润表": {"归母净利润": 3e8},
                               "现金流量表": {"经营活动现金流量净额": -1e8}})
    assert "经营现金流恶化" in _codes(hit)
    miss = mod.extra_flags({}, {"利润表": {"归母净利润": 3e8},
                                "现金流量表": {"经营活动现金流量净额": 2e8}})
    assert "经营现金流恶化" not in _codes(miss)


def test_empty_inputs_no_raise():
    assert isinstance(mod.extra_flags({}, {}), list)
    assert isinstance(mod.extra_flags(None, None), list)
    assert mod.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}}) == []
