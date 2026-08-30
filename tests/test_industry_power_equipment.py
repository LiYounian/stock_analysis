"""电力设备(锂电/光伏/风电)行业财报专家模块单测。

锁语义(防未来 prompt/代码重写误删规则):
  - 契约:KEY 为申万一级规范名"电力设备";dimension_specs 结构合法;weights 返回 dict/None;
  - 周期成长双属性:成长与质量并重(两者权重最高);
  - SKIP_FLAGS 跳过通用"毛利率异常跳升"(锂价上行毛利暴涨=周期常态,非操纵);不 skip 高负债;
  - 每条专属红旗至少一个"命中/不命中"边界断言(存货囤货减值/资产减值/扩产/杀毛利/现金流/大客户);
  - 空/半空输入不抛异常、返回 list。
纯函数、不触网、不读盘。
"""
import importlib

pe = importlib.import_module("tools.analysis.financial.industry.电力设备")


# ———————————— 契约:KEY / NOTE ————————————
def test_key_is_canonical_sw_name():
    assert pe.KEY == "电力设备"
    assert isinstance(pe.NOTE, str) and pe.NOTE


# ———————————— 被 get_expert 自动发现 ————————————
def test_discovered_by_get_expert():
    from tools.analysis.financial.industry import get_expert
    assert get_expert("电力设备") is pe


# ———————————— dimension_specs 结构合法 ————————————
def test_dimension_specs_structure():
    specs = pe.dimension_specs()
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
    """锂电关键调整:资产负债率反向且放宽(承接不 skip 的高负债);成长上端拉高(弹性)。"""
    specs = pe.dimension_specs()
    dar = [s for s in specs["健康"] if s[1] == "资产负债率"][0]
    assert dar[2] > dar[3]  # 反向
    # 成长营收上端拉高(> 通用整车 40)体现成长弹性
    rev = [s for s in specs["成长"] if s[1] == "营收增速"][0]
    assert rev[3] >= 50


def test_weights_growth_and_quality_top():
    w = pe.weights()
    assert w is None or isinstance(w, dict)
    if isinstance(w, dict):
        assert abs(sum(w.values()) - 1.0) < 1e-6
        # 双属性:成长与质量为最高两权
        top2 = sorted(w, key=w.get, reverse=True)[:2]
        assert set(top2) == {"成长", "质量"}


# ———————————— SKIP_FLAGS ————————————
def test_skip_flags():
    assert isinstance(pe.SKIP_FLAGS, list)
    assert "毛利率异常跳升" in pe.SKIP_FLAGS  # 周期上行毛利暴涨非操纵
    assert "高负债" not in pe.SKIP_FLAGS      # 扩产常态,交给区间放宽


# ———————————— 专属红旗:边界断言 ————————————
def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


def test_flag_inventory_stockpile_hit_and_miss():
    # 命中:存货增速 50 远超营收增速 10(差 40 > 20)
    hit = pe.extra_flags({"存货增速": 50.0, "营收增速": 10.0}, {})
    assert "存货囤货减值风险" in _codes(hit)
    # 不命中:存货增速与营收同步
    miss = pe.extra_flags({"存货增速": 12.0, "营收增速": 10.0}, {})
    assert "存货囤货减值风险" not in _codes(miss)


def test_flag_impairment_hit_and_miss():
    # 命中:资产减值 -5e8,归母 1e9(占比 0.5 > 0.3)
    hit = pe.extra_flags({}, {"利润表": {"资产减值损失": -5e8, "归母净利润": 1e9}})
    assert "资产减值显著" in _codes(hit)
    # 不命中:减值占比小
    miss = pe.extra_flags({}, {"利润表": {"资产减值损失": -1e7, "归母净利润": 1e9}})
    assert "资产减值显著" not in _codes(miss)


def test_flag_capex_aggressive_hit_and_miss():
    hit = pe.extra_flags({}, {"资产负债表": {"在建工程": 3e9, "资产总计": 1e10}})
    assert "扩产激进" in _codes(hit)  # 0.3 > 0.2
    miss = pe.extra_flags({}, {"资产负债表": {"在建工程": 5e8, "资产总计": 1e10}})
    assert "扩产激进" not in _codes(miss)


def test_flag_margin_kill_hit_and_miss():
    assert "杀毛利" in _codes(pe.extra_flags({"毛利率": 5.0}, {}))
    assert "杀毛利" not in _codes(pe.extra_flags({"毛利率": 20.0}, {}))


def test_flag_cfo_quality_hit_and_miss():
    # 命中路径 A:归母正而 CFO 负
    hitA = pe.extra_flags({}, {"利润表": {"归母净利润": 5e8},
                               "现金流量表": {"经营活动现金流量净额": -1e8}})
    assert "经营现金流恶化" in _codes(hitA)
    # 命中路径 B:CFO 为正但现金含量过低
    hitB = pe.extra_flags({"现金含量_CFO比净利": 0.1},
                          {"利润表": {"归母净利润": 5e8},
                           "现金流量表": {"经营活动现金流量净额": 5e7}})
    assert "现金含量偏低" in _codes(hitB)
    # 不命中:现金充沛
    miss = pe.extra_flags({"现金含量_CFO比净利": 1.0},
                          {"利润表": {"归母净利润": 5e8},
                           "现金流量表": {"经营活动现金流量净额": 5e8}})
    assert not ({"经营现金流恶化", "现金含量偏低"} & _codes(miss))


def test_flag_customer_concentration_proxy_hit_and_miss():
    hit = pe.extra_flags({}, {"资产负债表": {"应收账款": 6e8},
                              "利润表": {"营业总收入": 1e9}})
    assert "大客户依赖(代理)" in _codes(hit)  # 0.6 > 0.4
    miss = pe.extra_flags({}, {"资产负债表": {"应收账款": 1e8},
                               "利润表": {"营业总收入": 1e9}})
    assert "大客户依赖(代理)" not in _codes(miss)


# ———————————— 缺值/半空输入:不抛、返回 list ————————————
def test_extra_flags_empty_inputs_no_raise():
    assert isinstance(pe.extra_flags({}, {}), list)
    assert isinstance(pe.extra_flags(None, None), list)
    empty = pe.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}})
    assert isinstance(empty, list) and empty == []


def test_extra_flags_output_shape():
    for f in pe.extra_flags({"存货增速": 50.0, "营收增速": 10.0, "毛利率": 3.0}, {}):
        assert set(f) >= {"code", "命中", "严重度", "值"}
        assert f["严重度"] in ("高", "中", "低")
