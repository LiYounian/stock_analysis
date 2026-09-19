"""公用事业行业财报专家模块单测(锁语义,防未来重写误删规则)。
契约合法 + 负债放宽 + 每条专属红旗命中/不命中边界 + 空输入不抛异常。纯函数、不触网、不读盘。"""
import importlib

mod = importlib.import_module("tools.analysis.financial.industry.公用事业")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


def test_key_is_canonical_sw_name():
    assert mod.KEY == "公用事业"
    assert isinstance(mod.NOTE, str) and mod.NOTE


def test_dimension_specs_structure():
    specs = mod.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        for sub in subs:
            assert len(sub) == 4
            name, key, lo, hi = sub
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_leverage_relaxed():
    """重资产高负债:资产负债率反向区间放宽(0分端高于通用 80 的收敛端)。"""
    specs = mod.dimension_specs()
    dar = [s for s in specs["健康"] if s[1] == "资产负债率"][0]
    assert dar[2] > dar[3] and dar[3] >= 40  # 100分端放宽到 >=40


def test_weights_none_or_dict():
    assert mod.weights() is None or isinstance(mod.weights(), dict)


def test_skip_high_leverage():
    assert "高负债" in mod.SKIP_FLAGS


def test_flag_receivable_swell_hit_and_miss():
    assert "应收膨胀" in _codes(mod.extra_flags({"应收营收增速差": 30.0}, {}))
    assert "应收膨胀" not in _codes(mod.extra_flags({"应收营收增速差": 5.0}, {}))
    # 缺派生差值时用原子相减
    assert "应收膨胀" in _codes(mod.extra_flags({"应收增速": 40.0, "营收增速": 5.0}, {}))


def test_flag_fuel_margin_squeeze_hit_and_miss():
    assert "燃料成本挤压毛利" in _codes(mod.extra_flags({"毛利率同比升": -5.0}, {}))
    assert "燃料成本挤压毛利" not in _codes(mod.extra_flags({"毛利率同比升": 1.0}, {}))


def test_empty_inputs_no_raise():
    assert isinstance(mod.extra_flags({}, {}), list)
    assert isinstance(mod.extra_flags(None, None), list)
    assert mod.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}}) == []
