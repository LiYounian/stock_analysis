"""家用电器行业财报专家单测(锁语义)。契约合法 + 高ROE/毛利中枢校准 + 专属红旗边界 + 空输入不抛异常。"""
import importlib

mod = importlib.import_module("tools.analysis.financial.industry.家用电器")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


def test_key_is_canonical_sw_name():
    assert mod.KEY == "家用电器"
    assert isinstance(mod.NOTE, str) and mod.NOTE


def test_dimension_specs_structure():
    specs = mod.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        for name, key, lo, hi in subs:
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_high_roe_margin_center():
    """龙头高ROE→回报100分端上调;毛利率中枢上移(下界>0)。"""
    specs = mod.dimension_specs()
    roe = [s for s in specs["回报"] if s[1] == "ROE"][0]
    assert roe[3] >= 20
    gm = [s for s in specs["质量"] if s[1] == "毛利率"][0]
    assert gm[2] >= 10


def test_weights_none_or_dict():
    assert mod.weights() is None or isinstance(mod.weights(), dict)


def test_flag_channel_stuffing_hit_and_miss():
    # 应收侧命中
    assert "渠道压货" in _codes(mod.extra_flags({"应收营收增速差": 30.0}, {}))
    # 存货侧命中
    assert "渠道压货" in _codes(mod.extra_flags({"存货增速": 40.0, "营收增速": 5.0}, {}))
    # 均不达阈 → 不命中
    assert "渠道压货" not in _codes(mod.extra_flags({"应收营收增速差": 5.0, "存货增速": 10.0, "营收增速": 8.0}, {}))


def test_flag_material_margin_squeeze_hit_and_miss():
    assert "原材料侵蚀毛利" in _codes(mod.extra_flags({"毛利率同比升": -5.0}, {}))
    assert "原材料侵蚀毛利" not in _codes(mod.extra_flags({"毛利率同比升": 0.5}, {}))


def test_empty_inputs_no_raise():
    assert isinstance(mod.extra_flags({}, {}), list)
    assert isinstance(mod.extra_flags(None, None), list)
    assert mod.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}}) == []
