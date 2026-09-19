"""建筑装饰行业财报专家单测(锁语义)。契约合法 + 高杠杆/垫资区间校准 + 专属红旗边界 + 空输入不抛异常。"""
import importlib

mod = importlib.import_module("tools.analysis.financial.industry.建筑装饰")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


def test_key_is_canonical_sw_name():
    assert mod.KEY == "建筑装饰"
    assert isinstance(mod.NOTE, str) and mod.NOTE


def test_dimension_specs_structure():
    specs = mod.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        for name, key, lo, hi in subs:
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_leverage_and_receivable_relaxed():
    """高杠杆→资产负债率放宽;垫资→应收周转天数大幅放宽。"""
    specs = mod.dimension_specs()
    dar = [s for s in specs["健康"] if s[1] == "资产负债率"][0]
    assert dar[2] >= 85 and dar[2] > dar[3]
    ar = [s for s in specs["运营"] if s[1] == "应收周转天数"][0]
    assert ar[2] >= 300 and ar[2] > ar[3]


def test_skip_high_leverage():
    assert "高负债" in mod.SKIP_FLAGS


def test_weights_none_or_dict():
    assert mod.weights() is None or isinstance(mod.weights(), dict)


def test_flag_receivable_swell_hit_and_miss():
    assert "应收膨胀" in _codes(mod.extra_flags({"应收营收增速差": 35.0}, {}))
    assert "应收膨胀" not in _codes(mod.extra_flags({"应收营收增速差": 5.0}, {}))


def test_flag_negative_cfo_hit_and_miss():
    hit = mod.extra_flags({}, {"利润表": {"归母净利润": 5e8},
                               "现金流量表": {"经营活动现金流量净额": -3e8}})
    assert "经营现金流为负" in _codes(hit)
    miss = mod.extra_flags({}, {"利润表": {"归母净利润": 5e8},
                                "现金流量表": {"经营活动现金流量净额": 2e8}})
    assert "经营现金流为负" not in _codes(miss)


def test_empty_inputs_no_raise():
    assert isinstance(mod.extra_flags({}, {}), list)
    assert isinstance(mod.extra_flags(None, None), list)
    assert mod.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}}) == []
