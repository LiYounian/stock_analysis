"""计算机(云计算/算力服务)行业财报专家单测。

锁语义:
  · 契约结构(KEY 规范名 / dimension_specs 结构 / weights 类型 / SKIP_FLAGS);
  · 每条专属红旗至少一组"命中 / 不命中"边界断言(防未来重写无意删规则);
  · 空输入 / 全空表不抛异常且返回 list(纯函数缺值不炸)。
纯函数 + 纯数据,不触网、不读盘。
"""
import pytest

from tools.analysis.financial.industry import 计算机 as mod


# ———————————— 契约结构 ————————————
def test_key_is_canonical_sw_name():
    # 必须正好是申万一级"计算机",否则 analyzer 路由不到
    assert mod.KEY == "计算机"


def test_dimension_specs_structure():
    specs = mod.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        assert isinstance(dim, str)
        assert isinstance(subs, list) and subs
        for item in subs:
            assert len(item) == 4
            name, key, lo, hi = item
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_dimension_specs_covers_five_dims():
    assert set(mod.dimension_specs()) == {"成长", "质量", "健康", "运营", "回报"}


def test_quality_has_net_margin_core_subindicator():
    # 方案 ④:净利率是算力服务生死线,须作为质量维核心子指标
    质量键 = [key for (_n, key, _lo, _hi) in mod.dimension_specs()["质量"]]
    assert "净利率" in 质量键


def test_reverse_indicator_ranges_are_descending():
    # 反向指标(资产负债率 / 应收周转天数 / 应收营收增速差)0分端 > 100分端
    specs = mod.dimension_specs()
    lookup = {key: (lo, hi) for subs in specs.values() for (_n, key, lo, hi) in subs}
    for k in ("资产负债率", "应收周转天数", "应收营收增速差"):
        lo, hi = lookup[k]
        assert lo > hi, f"{k} 应为反向指标(0分端>100分端)"


def test_weights_type_and_growth_deprioritized():
    w = mod.weights()
    assert isinstance(w, dict)
    assert set(w) == {"成长", "质量", "健康", "运营", "回报"}
    # 核心原则:压低成长、重配质量
    assert w["质量"] > w["成长"]
    assert w["质量"] == max(w.values())


def test_skip_flags_type():
    assert isinstance(mod.SKIP_FLAGS, list)
    # 应收存货激增改用行业专属口径替代
    assert "应收存货激增" in mod.SKIP_FLAGS


# ———————————— 工具:取某 code 命中 ————————————
def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


# ———————————— 专属红旗 1:空心增长 ————————————
def test_hollow_growth_hit():
    flags = mod.extra_flags({"营收增速": 80.0, "净利率": 1.5}, {})
    assert "空心增长" in _codes(flags)


def test_hollow_growth_miss_when_margin_ok():
    # 营收暴增但净利率健康 → 不算空心
    flags = mod.extra_flags({"营收增速": 80.0, "净利率": 12.0}, {})
    assert "空心增长" not in _codes(flags)


def test_hollow_growth_miss_when_growth_modest():
    flags = mod.extra_flags({"营收增速": 10.0, "净利率": 1.0}, {})
    assert "空心增长" not in _codes(flags)


# ———————————— 专属红旗 2:应收激增超营收 ————————————
def test_ar_surge_hit():
    flags = mod.extra_flags({"应收营收增速差": 45.0}, {})
    assert "应收激增超营收" in _codes(flags)


def test_ar_surge_miss():
    flags = mod.extra_flags({"应收营收增速差": 5.0}, {})
    assert "应收激增超营收" not in _codes(flags)


# ———————————— 专属红旗 3:经营现金流为负 ————————————
def test_negative_cfo_hit():
    d = {}
    s = {"现金流量表": {"经营活动现金流量净额": -5e8},
         "利润表": {"归母净利润": 3e8}}
    assert "经营现金流为负" in _codes(mod.extra_flags(d, s))


def test_negative_cfo_miss_when_profit_negative():
    # 亏损期 CFO 为负不算"纸面利润"(需归母为正才成立)
    s = {"现金流量表": {"经营活动现金流量净额": -5e8},
         "利润表": {"归母净利润": -2e8}}
    assert "经营现金流为负" not in _codes(mod.extra_flags({}, s))


def test_negative_cfo_miss_when_cfo_positive():
    s = {"现金流量表": {"经营活动现金流量净额": 4e8},
         "利润表": {"归母净利润": 3e8}}
    assert "经营现金流为负" not in _codes(mod.extra_flags({}, s))


# ———————————— 专属红旗 4:自由现金流大额为负 ————————————
def test_negative_fcf_hit():
    # CFO 2亿 − capex 10亿 = -8亿,营收 10亿 → FCF/营收 = -0.8 < -0.20
    s = {"现金流量表": {"经营活动现金流量净额": 2e8,
                     "购建固定资产无形资产等支付现金": 10e8},
         "利润表": {"营业总收入": 10e8}}
    assert "自由现金流大额为负" in _codes(mod.extra_flags({}, s))


def test_negative_fcf_miss_when_fcf_healthy():
    s = {"现金流量表": {"经营活动现金流量净额": 5e8,
                     "购建固定资产无形资产等支付现金": 1e8},
         "利润表": {"营业总收入": 10e8}}
    assert "自由现金流大额为负" not in _codes(mod.extra_flags({}, s))


# ———————————— 专属红旗 5:存货激增 ————————————
def test_inventory_surge_hit():
    flags = mod.extra_flags({"存货增速": 100.0, "营收增速": 20.0}, {})
    assert "存货激增" in _codes(flags)


def test_inventory_surge_miss():
    flags = mod.extra_flags({"存货增速": 25.0, "营收增速": 20.0}, {})
    assert "存货激增" not in _codes(flags)


# ———————————— 专属红旗 6:资本开支超前收入 ————————————
def test_capex_ahead_hit():
    # capex 5亿 / 营收 10亿 = 0.5 > 0.30,且营收增速 3% < 10%
    s = {"现金流量表": {"购建固定资产无形资产等支付现金": 5e8},
         "利润表": {"营业总收入": 10e8}}
    assert "资本开支超前收入" in _codes(mod.extra_flags({"营收增速": 3.0}, s))


def test_capex_ahead_miss_when_revenue_growing():
    # 资本开支大但收入同步放量 → 不算超前
    s = {"现金流量表": {"购建固定资产无形资产等支付现金": 5e8},
         "利润表": {"营业总收入": 10e8}}
    assert "资本开支超前收入" not in _codes(mod.extra_flags({"营收增速": 50.0}, s))


# ———————————— 专属红旗 7:合同负债骤降 ————————————
def test_contract_liab_drop_hit():
    flags = mod.extra_flags({"合同负债环比": -50.0}, {})
    assert "合同负债骤降" in _codes(flags)


def test_contract_liab_drop_miss():
    flags = mod.extra_flags({"合同负债环比": 10.0}, {})
    assert "合同负债骤降" not in _codes(flags)


# ———————————— 缺值 / 空输入不抛异常 ————————————
def test_empty_inputs_no_raise():
    assert mod.extra_flags({}, {}) == []
    assert isinstance(mod.extra_flags(None, None), list)


def test_empty_tables_no_raise():
    empty = {"利润表": {}, "资产负债表": {}, "现金流量表": {}}
    assert isinstance(mod.extra_flags({}, empty), list)


def test_only_hit_flags_returned():
    # extra_flags 只返回命中项(未命中不列)
    flags = mod.extra_flags({"营收增速": 80.0, "净利率": 1.0}, {})
    assert flags and all(f["命中"] for f in flags)
    for f in flags:
        assert set(f) == {"code", "命中", "严重度", "值"}
        assert f["严重度"] in ("高", "中", "低")
