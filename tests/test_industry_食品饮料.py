"""食品饮料行业财报专家模块单测。

锁语义(防未来 prompt/代码重写误删规则):
  - 契约:KEY 为申万一级规范名"食品饮料";被 get_expert 自动发现;dimension_specs 结构合法;
    weights 归一且质量最重;
  - 五维区间:质量维毛利率区间上移(高毛利本色)、回报维提权(现金牛 ROE);
  - SKIP_FLAGS 跳过通用"高负债"(白酒预收推高负债率属景气非风险);
  - 渠道景气转弱(预收/合同负债环比)为白酒领先信号、现金流质量背离为强红旗;
  - 每条专属红旗至少一个"命中/不命中"边界断言;空/半空输入不抛异常、返回 list。
纯函数、不触网、不读盘。
"""
import importlib

auto = importlib.import_module("tools.analysis.financial.industry.食品饮料")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


# ———————————— 契约:KEY / NOTE / 自动发现 ————————————
def test_key_is_canonical_sw_name():
    assert auto.KEY == "食品饮料"
    assert isinstance(auto.NOTE, str) and auto.NOTE


def test_discovered_by_get_expert():
    from tools.analysis.financial.industry import get_expert
    assert get_expert("食品饮料") is auto


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
    """食饮关键调整:毛利率区间上移(白酒 70%+),回报维 ROE 上端拉高(现金牛)。"""
    specs = auto.dimension_specs()
    毛利率 = [s for s in specs["质量"] if s[1] == "毛利率"][0]
    assert 毛利率[3] >= 70  # 上端拉高识别高端白酒
    roe = [s for s in specs["回报"] if s[1] == "ROE"][0]
    assert roe[3] >= 20    # 现金牛高 ROE


def test_weights_shape():
    w = auto.weights()
    assert isinstance(w, dict)
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert w["质量"] == max(w.values())  # 质量最重
    assert w["回报"] >= 0.2              # 回报提权(现金牛)


# ———————————— SKIP_FLAGS ————————————
def test_skip_flags():
    assert isinstance(auto.SKIP_FLAGS, list)
    # 跳过通用"高负债"(白酒预收/合同负债推高负债率属景气非偿债风险)
    assert "高负债" in auto.SKIP_FLAGS


# ———————————— 专属红旗:边界断言 ————————————
def test_flag_channel_prosperity_hit_and_miss():
    # 命中:合同负债(预收)环比骤降(白酒渠道景气领先信号,高严重度)
    hit = auto.extra_flags({"合同负债环比": -30.0}, {})
    assert "渠道景气转弱" in _codes(hit)
    assert [f for f in hit if f["code"] == "渠道景气转弱"][0]["严重度"] == "高"
    # 不命中:预收环比正增(景气上行)
    miss = auto.extra_flags({"合同负债环比": 20.0}, {})
    assert "渠道景气转弱" not in _codes(miss)


def test_flag_channel_stuffing_hit_and_miss():
    # 命中路径 A:应收增速远超营收增速
    hitA = auto.extra_flags({"应收增速": 40.0, "营收增速": 10.0}, {})
    assert "经销商压货" in _codes(hitA)
    # 命中路径 B:存货增速远超营收增速
    hitB = auto.extra_flags({"存货增速": 45.0, "营收增速": 10.0}, {})
    assert "经销商压货" in _codes(hitB)
    # 不命中:增速同步
    miss = auto.extra_flags({"应收增速": 12.0, "存货增速": 12.0, "营收增速": 10.0}, {})
    assert "经销商压货" not in _codes(miss)


def test_flag_brand_premium_loss_hit_and_miss():
    # 标定 2026-08:毛利率阈值 25.0→18.0(原 25≈行业中位=命中40%泛滥,把大众品常态低毛利误当红旗)
    assert "品牌溢价流失" in _codes(auto.extra_flags({"毛利率": 15.0}, {}))   # 15 < 18 → 命中(真·低毛利)
    # 毛利率 22:旧阈值 25 会误报,新阈值 18 不报(锁住去泛滥语义,防回退)
    assert "品牌溢价流失" not in _codes(auto.extra_flags({"毛利率": 22.0}, {}))
    assert "品牌溢价流失" not in _codes(auto.extra_flags({"毛利率": 60.0}, {}))


def test_flag_cashflow_divergence_hit_and_miss():
    # 命中:归母正而 CFO 负(消费股应现金充沛,背离=强红旗)
    hit = auto.extra_flags({}, {"利润表": {"归母净利润": 5e8},
                                "现金流量表": {"经营活动现金流量净额": -3e8}})
    assert "现金流质量背离" in _codes(hit)
    assert [f for f in hit if f["code"] == "现金流质量背离"][0]["严重度"] == "高"
    # 命中(中):现金含量偏低
    hit2 = auto.extra_flags({"现金含量_CFO比净利": 0.2},
                            {"利润表": {"归母净利润": 5e8}})
    assert "现金含量偏低" in _codes(hit2)
    # 不命中:现金充沛
    miss = auto.extra_flags({"现金含量_CFO比净利": 1.0},
                            {"利润表": {"归母净利润": 5e8},
                             "现金流量表": {"经营活动现金流量净额": 6e8}})
    assert "现金流质量背离" not in _codes(miss) and "现金含量偏低" not in _codes(miss)


# ———————————— 缺值/半空输入:不抛、返回 list ————————————
def test_extra_flags_empty_inputs_no_raise():
    assert isinstance(auto.extra_flags({}, {}), list)
    assert isinstance(auto.extra_flags(None, None), list)
    empty = auto.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}})
    assert isinstance(empty, list) and empty == []


def test_extra_flags_output_shape():
    for f in auto.extra_flags({"合同负债环比": -30.0, "毛利率": 18.0, "存货增速": 45.0,
                               "营收增速": 5.0}, {}):
        assert set(f) >= {"code", "命中", "严重度", "值"}
        assert f["严重度"] in ("高", "中", "低")
