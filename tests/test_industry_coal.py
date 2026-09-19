"""煤炭行业财报专家单测(锁语义)。契约合法 + 周期区间上调 + 专属红旗边界 + 空输入不抛异常。"""
import importlib

mod = importlib.import_module("tools.analysis.financial.industry.煤炭")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


def test_key_is_canonical_sw_name():
    assert mod.KEY == "煤炭"
    assert isinstance(mod.NOTE, str) and mod.NOTE


def test_dimension_specs_structure():
    specs = mod.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        for name, key, lo, hi in subs:
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_margin_roe_upgraded():
    """景气期高毛利高ROE:毛利率/ROE 100分端上调。"""
    specs = mod.dimension_specs()
    gm = [s for s in specs["质量"] if s[1] == "毛利率"][0]
    assert gm[3] >= 50
    roe = [s for s in specs["回报"] if s[1] == "ROE"][0]
    assert roe[3] >= 20


def test_weights_none_or_dict():
    assert mod.weights() is None or isinstance(mod.weights(), dict)


def test_flag_coal_price_down_hit_and_miss():
    assert "煤价下行" in _codes(mod.extra_flags({"毛利率同比升": -8.0}, {}))
    assert "煤价下行" not in _codes(mod.extra_flags({"毛利率同比升": -2.0}, {}))


def test_flag_cfo_deteriorate_hit_and_miss():
    hit = mod.extra_flags({}, {"利润表": {"归母净利润": 8e8},
                               "现金流量表": {"经营活动现金流量净额": -1e8}})
    assert "经营现金流恶化" in _codes(hit)
    miss = mod.extra_flags({}, {"利润表": {"归母净利润": 8e8},
                                "现金流量表": {"经营活动现金流量净额": 5e8}})
    assert "经营现金流恶化" not in _codes(miss)


def test_empty_inputs_no_raise():
    assert isinstance(mod.extra_flags({}, {}), list)
    assert isinstance(mod.extra_flags(None, None), list)
    assert mod.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}}) == []
