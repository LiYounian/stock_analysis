"""汽车(整车)行业财报专家模块单测。

锁语义(防未来 prompt/代码重写误删规则):
  - 契约:KEY 为申万一级规范名"汽车";dimension_specs 结构合法;weights 返回 dict/None;
  - 五维区间:质量维毛利率下调、扣非占归母提权;健康维资产负债率区间放宽(承接不 skip 的高负债);
  - SKIP_FLAGS 跳过通用"增收不增利"(改用扣非口径专属版);
  - 每条专属红旗至少一个"命中/不命中"边界断言;
  - 空/半空输入不抛异常、返回 list。
纯函数、不触网、不读盘。
"""
import importlib

import pytest

auto = importlib.import_module("tools.analysis.financial.industry.汽车")


# ———————————— 契约:KEY / NOTE ————————————
def test_key_is_canonical_sw_name():
    assert auto.KEY == "汽车"
    assert isinstance(auto.NOTE, str) and auto.NOTE


# ———————————— dimension_specs 结构合法 ————————————
def test_dimension_specs_structure():
    specs = auto.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        assert isinstance(dim, str) and subs
        for sub in subs:
            assert len(sub) == 4
            name, key, lo, hi = sub
            assert isinstance(name, str) and name
            assert isinstance(key, str) and key
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_dimension_specs_key_industry_adjustments():
    """整车关键调整:毛利率区间下调(<通用 0→50)、资产负债率反向区间放宽、周转天数收紧。"""
    specs = auto.dimension_specs()
    # 质量维:毛利率上限被下调到 25(远低于通用 50),识别薄毛利
    毛利率 = [s for s in specs["质量"] if s[1] == "毛利率"][0]
    assert 毛利率[2] == 5 and 毛利率[3] == 25
    # 扣非占归母提权:0分端 0.6(高于通用 0)
    扣非 = [s for s in specs["质量"] if s[1] == "扣非占归母"][0]
    assert 扣非[2] == 0.6
    # 健康维:资产负债率反向(0分端>100分端)且区间放宽(100分端 45,高于通用 30)
    dar = [s for s in specs["健康"] if s[1] == "资产负债率"][0]
    assert dar[2] > dar[3] and dar[3] == 45


def test_weights_shape():
    w = auto.weights()
    assert w is None or isinstance(w, dict)
    if isinstance(w, dict):
        assert abs(sum(w.values()) - 1.0) < 1e-6  # 权重归一(工程占位)
        assert w["质量"] == max(w.values())        # 质量最重


# ———————————— SKIP_FLAGS ————————————
def test_skip_flags():
    assert isinstance(auto.SKIP_FLAGS, list)
    # 跳过通用"增收不增利"(改用扣非口径专属版);不 skip 高负债(交给区间放宽)
    assert "增收不增利" in auto.SKIP_FLAGS
    assert "高负债" not in auto.SKIP_FLAGS


# ———————————— 专属红旗:边界断言 ————————————
def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


def test_flag_price_war_hit_and_miss():
    # 命中:营收增速 30、扣非增速 5(差 25 > 20pct)
    hit = auto.extra_flags({"营收增速": 30.0, "扣非净利增速": 5.0}, {})
    assert "增收不增利_价格战" in _codes(hit)
    # 不命中:扣非增速紧跟营收(差 5 < 20pct)
    miss = auto.extra_flags({"营收增速": 30.0, "扣非净利增速": 25.0}, {})
    assert "增收不增利_价格战" not in _codes(miss)
    # 不命中:营收负增(前提不满足)
    miss2 = auto.extra_flags({"营收增速": -5.0, "扣非净利增速": -40.0}, {})
    assert "增收不增利_价格战" not in _codes(miss2)


def test_flag_subsidy_prop_up_hit_and_miss():
    # 命中路径 A:扣非占归母 < 0.6
    hitA = auto.extra_flags({"扣非占归母": 0.3}, {})
    assert "靠补助撑利润" in _codes(hitA)
    # 命中路径 B:归母正而扣非负
    hitB = auto.extra_flags({}, {"利润表": {"归母净利润": 1e8, "扣非归母净利润": -2e7}})
    assert "靠补助撑利润" in _codes(hitB)
    # 不命中:扣非占归母健康
    miss = auto.extra_flags({"扣非占归母": 0.95}, {"利润表": {"归母净利润": 1e8, "扣非归母净利润": 9e7}})
    assert "靠补助撑利润" not in _codes(miss)


def test_flag_margin_pressure_hit_and_miss():
    assert "毛利率承压" in _codes(auto.extra_flags({"毛利率": 5.0}, {}))
    assert "毛利率承压" not in _codes(auto.extra_flags({"毛利率": 18.0}, {}))


def test_flag_cfo_deterioration_hit_and_miss():
    # 命中:归母正而 CFO 负
    hit = auto.extra_flags({}, {"利润表": {"归母净利润": 5e8},
                                "现金流量表": {"经营活动现金流量净额": -3e8}})
    assert "经营现金流恶化" in _codes(hit)
    # 不命中:CFO 为正
    miss = auto.extra_flags({}, {"利润表": {"归母净利润": 5e8},
                                 "现金流量表": {"经营活动现金流量净额": 6e8}})
    assert "经营现金流恶化" not in _codes(miss)


def test_flag_rd_capitalization_hit_and_miss():
    # 命中:开发支出 / 研发费用 > 1.0
    hit = auto.extra_flags({}, {"利润表": {"研发费用": 1e8},
                                "资产负债表": {"开发支出": 1.5e8}})
    assert "研发资本化激进" in _codes(hit)
    # 不命中:资本化占比低
    miss = auto.extra_flags({}, {"利润表": {"研发费用": 1e8},
                                 "资产负债表": {"开发支出": 2e7}})
    assert "研发资本化激进" not in _codes(miss)


# ———————————— 缺值/半空输入:不抛、返回 list ————————————
def test_extra_flags_empty_inputs_no_raise():
    assert isinstance(auto.extra_flags({}, {}), list)
    assert isinstance(auto.extra_flags(None, None), list)
    empty = auto.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}})
    assert isinstance(empty, list) and empty == []  # 全缺 → 无命中


def test_extra_flags_output_shape():
    """命中项字段齐全:code/命中/严重度/值。"""
    for f in auto.extra_flags({"营收增速": 30.0, "扣非净利增速": 0.0, "毛利率": 3.0}, {}):
        assert set(f) >= {"code", "命中", "严重度", "值"}
        assert f["严重度"] in ("高", "中", "低")
