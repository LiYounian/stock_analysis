"""有色金属(锂矿)行业财报专家模块单测。

锁语义(防未来 prompt/代码重写误删规则):
  - 契约:KEY 为申万一级规范名"有色金属";dimension_specs 结构合法;weights 返回 dict/None;
  - 周期股取向:成长维权重被压低(防追周期顶点高增速),质量(毛利)权重最高;
  - SKIP_FLAGS 跳过通用"毛利率异常跳升"(锂价上行毛利暴涨=周期常态);不 skip 高负债;
  - 每条专属红旗至少一个"命中/不命中"边界断言(周期顶点/下行杀业绩/成本崩/存货减值/资产减值/扩产);
  - 空/半空输入不抛异常、返回 list。
纯函数、不触网、不读盘。
"""
import importlib

mt = importlib.import_module("tools.analysis.financial.industry.有色金属")


# ———————————— 契约:KEY / NOTE ————————————
def test_key_is_canonical_sw_name():
    assert mt.KEY == "有色金属"
    assert isinstance(mt.NOTE, str) and mt.NOTE


# ———————————— 被 get_expert 自动发现 ————————————
def test_discovered_by_get_expert():
    from tools.analysis.financial.industry import get_expert
    assert get_expert("有色金属") is mt


# ———————————— dimension_specs 结构合法 ————————————
def test_dimension_specs_structure():
    specs = mt.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        assert isinstance(dim, str) and subs
        for sub in subs:
            assert len(sub) == 4
            name, key, lo, hi = sub
            assert isinstance(name, str) and name
            assert isinstance(key, str) and key
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_dimension_specs_wide_gross_margin():
    """资源周期:毛利率区间宽且上端高(锂矿毛利厚且波动大)。"""
    specs = mt.dimension_specs()
    gm = [s for s in specs["质量"] if s[1] == "毛利率"][0]
    assert gm[3] >= 50  # 上端拉高承接资源高毛利


def test_weights_cyclical_growth_suppressed():
    """周期股:成长维权重被压低,质量(毛利护城河)最高。"""
    w = mt.weights()
    assert isinstance(w, dict)
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert w["质量"] == max(w.values())      # 毛利护城河最重
    assert w["成长"] == min(w.values())      # 成长(单期高增速)压低,防追周期顶点
    assert w["成长"] < w["回报"]


# ———————————— SKIP_FLAGS ————————————
def test_skip_flags():
    assert isinstance(mt.SKIP_FLAGS, list)
    assert "毛利率异常跳升" in mt.SKIP_FLAGS
    assert "高负债" not in mt.SKIP_FLAGS


# ———————————— 专属红旗:边界断言 ————————————
def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


def test_flag_cycle_peak_hit_and_miss():
    # 命中:毛利率 60 且营收增速 80(高毛利+高增速=周期顶点)
    hit = mt.extra_flags({"毛利率": 60.0, "营收增速": 80.0}, {})
    assert "周期顶点均值回归风险" in _codes(hit)
    # 不命中:毛利率高但增速平缓
    miss = mt.extra_flags({"毛利率": 60.0, "营收增速": 5.0}, {})
    assert "周期顶点均值回归风险" not in _codes(miss)


def test_flag_cycle_down_hit_and_miss():
    # 命中:营收增速 10 而归母增速 -40(量增价跌,差 50 > 30)
    hit = mt.extra_flags({"营收增速": 10.0, "归母净利增速": -40.0}, {})
    assert "周期下行杀业绩" in _codes(hit)
    # 不命中:利润与营收同步
    miss = mt.extra_flags({"营收增速": 10.0, "归母净利增速": 8.0}, {})
    assert "周期下行杀业绩" not in _codes(miss)


def test_flag_cost_margin_collapse_hit_and_miss():
    assert "成本承压毛利崩塌" in _codes(mt.extra_flags({"毛利率": 5.0}, {}))
    assert "成本承压毛利崩塌" not in _codes(mt.extra_flags({"毛利率": 40.0}, {}))


def test_flag_inventory_impairment_hit_and_miss():
    # 命中:存货增速 60 远超营收增速 20(差 40 > 25)
    hit = mt.extra_flags({"存货增速": 60.0, "营收增速": 20.0}, {})
    assert "存货跌价减值风险" in _codes(hit)
    miss = mt.extra_flags({"存货增速": 22.0, "营收增速": 20.0}, {})
    assert "存货跌价减值风险" not in _codes(miss)


def test_flag_asset_impairment_hit_and_miss():
    hit = mt.extra_flags({}, {"利润表": {"资产减值损失": -5e8, "归母净利润": 1e9}})
    assert "资产减值显著" in _codes(hit)
    miss = mt.extra_flags({}, {"利润表": {"资产减值损失": -1e7, "归母净利润": 1e9}})
    assert "资产减值显著" not in _codes(miss)


def test_flag_capex_aggressive_hit_and_miss():
    hit = mt.extra_flags({}, {"资产负债表": {"在建工程": 3e9, "资产总计": 1e10}})
    assert "扩产激进" in _codes(hit)
    miss = mt.extra_flags({}, {"资产负债表": {"在建工程": 5e8, "资产总计": 1e10}})
    assert "扩产激进" not in _codes(miss)


# ———————————— 缺值/半空输入:不抛、返回 list ————————————
def test_extra_flags_empty_inputs_no_raise():
    assert isinstance(mt.extra_flags({}, {}), list)
    assert isinstance(mt.extra_flags(None, None), list)
    empty = mt.extra_flags({}, {"利润表": {}, "资产负债表": {}})
    assert isinstance(empty, list) and empty == []


def test_extra_flags_output_shape():
    for f in mt.extra_flags({"毛利率": 60.0, "营收增速": 80.0, "归母净利增速": 5.0}, {}):
        assert set(f) >= {"code", "命中", "严重度", "值"}
        assert f["严重度"] in ("高", "中", "低")
