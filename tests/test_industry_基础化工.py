"""基础化工(强周期)行业财报专家模块单测。

锁语义(防未来 prompt/代码重写误删规则):
  - 契约:KEY 为申万一级规范名"基础化工";dimension_specs 结构合法;weights 归一且质量最重;
  - 周期股画像:SKIP『毛利率异常跳升』(景气上行毛利跳升是周期常态);不 skip 高负债(区间放宽承接);
    成长压权(防追顶)、质量最重;
  - 每条专属红旗至少一个"命中/不命中"边界断言;
  - 空/半空输入不抛异常、返回 list。
纯函数、不触网、不读盘。
"""
import importlib

mod = importlib.import_module("tools.analysis.financial.industry.基础化工")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


# ———————————— 契约 ————————————
def test_key_is_canonical_sw_name():
    assert mod.KEY == "基础化工"
    assert isinstance(mod.NOTE, str) and mod.NOTE


def test_dimension_specs_structure():
    specs = mod.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        assert isinstance(dim, str) and subs
        for sub in subs:
            assert len(sub) == 4
            name, key, lo, hi = sub
            assert isinstance(name, str) and name
            assert isinstance(key, str) and key
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_dimension_specs_cyclical_adjustments():
    """周期调整:毛利率区间偏中(10~30);健康维资产负债率反向且放宽(100分端 40>通用 30)。"""
    specs = mod.dimension_specs()
    毛利率 = [s for s in specs["质量"] if s[1] == "毛利率"][0]
    assert 毛利率[2] == 10 and 毛利率[3] == 30
    dar = [s for s in specs["健康"] if s[1] == "资产负债率"][0]
    assert dar[2] > dar[3] and dar[3] == 40  # 反向 + 放宽


def test_weights_quality_heaviest_growth_pressed():
    """防追顶:质量最重,成长权重被压(<=质量)。"""
    w = mod.weights()
    assert isinstance(w, dict)
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert w["质量"] == max(w.values())
    assert w["成长"] <= w["质量"]


def test_skip_flags():
    assert "毛利率异常跳升" in mod.SKIP_FLAGS   # 周期常态非操纵
    assert "高负债" not in mod.SKIP_FLAGS        # 交给区间放宽,不 skip


# ———————————— 专属红旗:边界 ————————————
def test_flag_cycle_peak_hit_and_miss():
    # 命中:营收高增 45 且毛利率高位 30
    assert "周期顶部景气过热" in _codes(mod.extra_flags({"营收增速": 45.0, "毛利率": 30.0}, {}))
    # 不命中:营收高增但毛利率不高
    assert "周期顶部景气过热" not in _codes(mod.extra_flags({"营收增速": 45.0, "毛利率": 15.0}, {}))
    # 不命中:毛利率高但营收未高增
    assert "周期顶部景气过热" not in _codes(mod.extra_flags({"营收增速": 10.0, "毛利率": 30.0}, {}))


def test_flag_inventory_hoarding_hit_and_miss():
    assert "存货囤货减值风险" in _codes(mod.extra_flags({"存货增速": 40.0, "营收增速": 10.0}, {}))
    assert "存货囤货减值风险" not in _codes(mod.extra_flags({"存货增速": 15.0, "营收增速": 10.0}, {}))
    # 存货负增(前提不满足)
    assert "存货囤货减值风险" not in _codes(mod.extra_flags({"存货增速": -5.0, "营收增速": -30.0}, {}))


def test_flag_impairment_hit_and_miss():
    hit = mod.extra_flags({}, {"利润表": {"资产减值损失": -5e7, "归母净利润": 1e8}})
    assert "资产减值显著" in _codes(hit)   # 5000万/1亿=0.5>0.3
    miss = mod.extra_flags({}, {"利润表": {"资产减值损失": -1e7, "归母净利润": 1e8}})
    assert "资产减值显著" not in _codes(miss)


def test_flag_capex_aggressive_hit_and_miss():
    hit = mod.extra_flags({}, {"资产负债表": {"在建工程": 3e8, "资产总计": 1e9}})
    assert "扩产激进" in _codes(hit)   # 0.3>0.2
    miss = mod.extra_flags({}, {"资产负债表": {"在建工程": 1e8, "资产总计": 1e9}})
    assert "扩产激进" not in _codes(miss)


def test_flag_margin_kill_hit_and_miss():
    assert "杀毛利" in _codes(mod.extra_flags({"毛利率": 5.0}, {}))
    assert "杀毛利" not in _codes(mod.extra_flags({"毛利率": 20.0}, {}))


def test_flag_cfo_deterioration_hit_and_miss():
    hit = mod.extra_flags({}, {"利润表": {"归母净利润": 5e8},
                               "现金流量表": {"经营活动现金流量净额": -3e8}})
    assert "经营现金流恶化" in _codes(hit)
    miss = mod.extra_flags({}, {"利润表": {"归母净利润": 5e8},
                                "现金流量表": {"经营活动现金流量净额": 6e8}})
    assert "经营现金流恶化" not in _codes(miss)


# ———————————— 缺值/半空 ————————————
def test_extra_flags_empty_inputs_no_raise():
    assert isinstance(mod.extra_flags({}, {}), list)
    assert isinstance(mod.extra_flags(None, None), list)
    empty = mod.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}})
    assert isinstance(empty, list) and empty == []


def test_extra_flags_output_shape():
    for f in mod.extra_flags({"营收增速": 45.0, "毛利率": 5.0, "存货增速": 50.0}, {}):
        assert set(f) >= {"code", "命中", "严重度", "值"}
        assert f["严重度"] in ("高", "中", "低")
