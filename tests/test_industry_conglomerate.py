"""综合行业财报专家单测(锁语义)。极简版:契约合法 + 五维沿用通用默认 + 唯一红旗边界 + 空输入不抛异常。"""
import importlib

mod = importlib.import_module("tools.analysis.financial.industry.综合")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


def test_key_is_canonical_sw_name():
    assert mod.KEY == "综合"
    assert isinstance(mod.NOTE, str) and mod.NOTE


def test_dimension_specs_structure_full_five_dims():
    """极简版仍须导出完整五维(避免整体替换后缺维)。"""
    specs = mod.dimension_specs()
    assert set(specs.keys()) == {"成长", "质量", "健康", "运营", "回报"}
    for dim, subs in specs.items():
        for name, key, lo, hi in subs:
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_weights_none_or_dict():
    assert mod.weights() is None or isinstance(mod.weights(), dict)


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
