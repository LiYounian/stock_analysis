"""钢铁行业财报专家单测(锁语义)。契约合法 + 高负债/周期亏损区间校准 + 专属红旗边界 + 空输入不抛异常。"""
import importlib

mod = importlib.import_module("tools.analysis.financial.industry.钢铁")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


def test_key_is_canonical_sw_name():
    assert mod.KEY == "钢铁"
    assert isinstance(mod.NOTE, str) and mod.NOTE


def test_dimension_specs_structure():
    specs = mod.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        for name, key, lo, hi in subs:
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_leverage_relaxed_and_cyclical_loss():
    """高负债→资产负债率放宽;周期亏损→毛利率/ROE 下界为负。"""
    specs = mod.dimension_specs()
    dar = [s for s in specs["健康"] if s[1] == "资产负债率"][0]
    assert dar[2] > dar[3] and dar[3] >= 40
    gm = [s for s in specs["质量"] if s[1] == "毛利率"][0]
    assert gm[2] < 0
    roe = [s for s in specs["回报"] if s[1] == "ROE"][0]
    assert roe[2] < 0


def test_skip_high_leverage():
    assert "高负债" in mod.SKIP_FLAGS


def test_weights_none_or_dict():
    assert mod.weights() is None or isinstance(mod.weights(), dict)


def test_flag_steel_price_down_hit_and_miss():
    assert "钢价下行" in _codes(mod.extra_flags({"毛利率同比升": -6.0}, {}))
    assert "钢价下行" not in _codes(mod.extra_flags({"毛利率同比升": -1.0}, {}))


def test_flag_cfo_deteriorate_hit_and_miss():
    hit = mod.extra_flags({}, {"利润表": {"归母净利润": 6e8},
                               "现金流量表": {"经营活动现金流量净额": -2e8}})
    assert "经营现金流恶化" in _codes(hit)
    miss = mod.extra_flags({}, {"利润表": {"归母净利润": 6e8},
                                "现金流量表": {"经营活动现金流量净额": 3e8}})
    assert "经营现金流恶化" not in _codes(miss)


def test_empty_inputs_no_raise():
    assert isinstance(mod.extra_flags({}, {}), list)
    assert isinstance(mod.extra_flags(None, None), list)
    assert mod.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}}) == []
