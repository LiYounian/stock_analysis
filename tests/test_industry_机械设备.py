"""机械设备行业财报专家模块单测。

锁语义(防未来 prompt/代码重写误删规则):
  - 契约:KEY 为申万一级规范名"机械设备";被 get_expert 自动发现;dimension_specs 结构合法;
    weights 归一且质量最重;
  - 五维区间:运营维应收周转天数区间放宽(工程机械账期长);
  - SKIP_FLAGS 跳过通用"毛利率异常跳升"(周期修复毛利跳升是常态),不 skip 高负债;
  - 应收回款风险为工程机械命门(高严重度)、订单转弱看合同负债环比;
  - 每条专属红旗至少一个"命中/不命中"边界断言;空/半空输入不抛异常、返回 list。
纯函数、不触网、不读盘。
"""
import importlib

auto = importlib.import_module("tools.analysis.financial.industry.机械设备")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


# ———————————— 契约:KEY / NOTE / 自动发现 ————————————
def test_key_is_canonical_sw_name():
    assert auto.KEY == "机械设备"
    assert isinstance(auto.NOTE, str) and auto.NOTE


def test_discovered_by_get_expert():
    from tools.analysis.financial.industry import get_expert
    assert get_expert("机械设备") is auto


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
    """机械关键调整:应收周转天数反向且区间放宽(工程机械账期长);资产负债率反向。"""
    specs = auto.dimension_specs()
    ar = [s for s in specs["运营"] if s[1] == "应收周转天数"][0]
    assert ar[2] > ar[3] and ar[2] >= 150  # 反向且 0分端放宽(容忍长账期)
    dar = [s for s in specs["健康"] if s[1] == "资产负债率"][0]
    assert dar[2] > dar[3]  # 反向


def test_weights_shape():
    w = auto.weights()
    assert isinstance(w, dict)
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert w["质量"] == max(w.values())  # 质量最重(回款/毛利真赚钱)
    assert w["成长"] >= 0.2              # 成长次重(订单弹性)


# ———————————— SKIP_FLAGS ————————————
def test_skip_flags():
    assert isinstance(auto.SKIP_FLAGS, list)
    # 跳过通用"毛利率异常跳升"(周期修复毛利跳升是常态);不 skip 高负债(重资产常态,交区间)
    assert "毛利率异常跳升" in auto.SKIP_FLAGS
    assert "高负债" not in auto.SKIP_FLAGS


# ———————————— 专属红旗:边界断言 ————————————
def test_flag_receivable_collection_hit_and_miss():
    # 命中路径 A:应收/营收 偏高(工程机械命门,高严重度)
    hitA = auto.extra_flags({}, {"资产负债表": {"应收账款": 5e8},
                                 "利润表": {"营业总收入": 1e9}})
    assert "应收回款风险" in _codes(hitA)
    assert [f for f in hitA if f["code"] == "应收回款风险"][0]["严重度"] == "高"
    # 命中路径 B:应收增速远超营收增速
    hitB = auto.extra_flags({"应收增速": 40.0, "营收增速": 10.0}, {})
    assert "应收回款风险" in _codes(hitB)
    # 不命中:应收占比低且增速同步
    miss = auto.extra_flags({"应收增速": 12.0, "营收增速": 10.0},
                            {"资产负债表": {"应收账款": 1e8}, "利润表": {"营业总收入": 1e9}})
    assert "应收回款风险" not in _codes(miss)


def test_flag_aggressive_capex_hit_and_miss():
    # 命中:在建工程/资产总计 > 0.15
    hit = auto.extra_flags({}, {"资产负债表": {"在建工程": 3e8, "资产总计": 1e9}})
    assert "扩产激进" in _codes(hit)
    # 不命中:在建占比低
    miss = auto.extra_flags({}, {"资产负债表": {"在建工程": 5e7, "资产总计": 1e9}})
    assert "扩产激进" not in _codes(miss)


def test_flag_order_weakening_hit_and_miss():
    # 命中:合同负债(订单/预收)环比骤降
    assert "订单转弱" in _codes(auto.extra_flags({"合同负债环比": -30.0}, {}))
    # 不命中:订单环比正增
    assert "订单转弱" not in _codes(auto.extra_flags({"合同负债环比": 20.0}, {}))


def test_flag_inventory_pileup_hit_and_miss():
    hit = auto.extra_flags({"存货增速": 45.0, "营收增速": 10.0}, {})
    assert "存货积压" in _codes(hit)
    miss = auto.extra_flags({"存货增速": 12.0, "营收增速": 10.0}, {})
    assert "存货积压" not in _codes(miss)


def test_flag_cfo_deterioration_hit_and_miss():
    hit = auto.extra_flags({}, {"利润表": {"归母净利润": 5e8},
                                "现金流量表": {"经营活动现金流量净额": -3e8}})
    assert "经营现金流恶化" in _codes(hit)
    miss = auto.extra_flags({}, {"利润表": {"归母净利润": 5e8},
                                 "现金流量表": {"经营活动现金流量净额": 6e8}})
    assert "经营现金流恶化" not in _codes(miss)


def test_flag_rd_underinvestment_hit_and_miss():
    # 命中:研发费用率 < 2.0(新兴高端制造迭代掉队,低严重度提示)
    hit = auto.extra_flags({"研发费用率": 1.0}, {})
    assert "研发投入不足" in _codes(hit)
    assert [f for f in hit if f["code"] == "研发投入不足"][0]["严重度"] == "低"
    # 不命中:研发投入充足
    miss = auto.extra_flags({"研发费用率": 6.0}, {})
    assert "研发投入不足" not in _codes(miss)


# ———————————— 缺值/半空输入:不抛、返回 list ————————————
def test_extra_flags_empty_inputs_no_raise():
    assert isinstance(auto.extra_flags({}, {}), list)
    assert isinstance(auto.extra_flags(None, None), list)
    empty = auto.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}})
    assert isinstance(empty, list) and empty == []


def test_extra_flags_output_shape():
    for f in auto.extra_flags({"应收增速": 40.0, "营收增速": 5.0, "合同负债环比": -30.0,
                               "研发费用率": 1.0}, {}):
        assert set(f) >= {"code", "命中", "严重度", "值"}
        assert f["严重度"] in ("高", "中", "低")
