"""农林牧渔行业财报专家单测(锁语义)。契约合法 + 周期区间放宽 + 专属红旗边界 + 空输入不抛异常。"""
import importlib

mod = importlib.import_module("tools.analysis.financial.industry.农林牧渔")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


def test_key_is_canonical_sw_name():
    assert mod.KEY == "农林牧渔"
    assert isinstance(mod.NOTE, str) and mod.NOTE


def test_dimension_specs_structure():
    specs = mod.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        for name, key, lo, hi in subs:
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_cycle_ranges_relaxed():
    """周期股:毛利率下界放到负值、存货周转天数区间放宽。"""
    specs = mod.dimension_specs()
    gm = [s for s in specs["质量"] if s[1] == "毛利率"][0]
    assert gm[2] < 0  # 下行周期可整体亏损
    inv = [s for s in specs["运营"] if s[1] == "存货周转天数"][0]
    assert inv[2] >= 500 and inv[2] > inv[3]


def test_weights_none_or_dict():
    assert mod.weights() is None or isinstance(mod.weights(), dict)


def test_flag_bio_asset_pileup_hit_and_miss():
    assert "生物资产堆积" in _codes(mod.extra_flags({"存货增速": 45.0, "营收增速": 5.0}, {}))
    assert "生物资产堆积" not in _codes(mod.extra_flags({"存货增速": 15.0, "营收增速": 10.0}, {}))


def test_flag_cycle_cash_bleed_hit_and_miss():
    hit = mod.extra_flags({}, {"利润表": {"归母净利润": -3e8},
                               "现金流量表": {"经营活动现金流量净额": -2e8}})
    assert "周期现金失血" in _codes(hit)
    # 亏损但经营现金为正 → 不命中
    miss = mod.extra_flags({}, {"利润表": {"归母净利润": -3e8},
                                "现金流量表": {"经营活动现金流量净额": 1e8}})
    assert "周期现金失血" not in _codes(miss)


def test_empty_inputs_no_raise():
    assert isinstance(mod.extra_flags({}, {}), list)
    assert isinstance(mod.extra_flags(None, None), list)
    assert mod.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}}) == []
