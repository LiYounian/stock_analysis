"""国防军工行业财报专家模块单测。

锁语义(防未来 prompt/代码重写误删规则):
  - 契约:KEY 为申万一级规范名"国防军工";dimension_specs 结构合法;weights 归一;
  - 行业画像:运营维应收/存货周转天数区间**放宽**(军方账期长/在产周期长,避免系统性误杀);
    SKIP『现金含量不足』(回款慢致 CFO 常年弱于利润是行业特性),改用更严专属"经营现金流恶化";
  - 每条专属红旗至少一个"命中/不命中"边界断言;
  - 空/半空输入不抛异常、返回 list。
纯函数、不触网、不读盘。
"""
import importlib

mod = importlib.import_module("tools.analysis.financial.industry.国防军工")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


# ———————————— 契约 ————————————
def test_key_is_canonical_sw_name():
    assert mod.KEY == "国防军工"
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


def test_dimension_specs_turnover_relaxed():
    """军方账期长/在产周期长:应收、存货周转天数反向区间放宽(0分端更大)。"""
    specs = mod.dimension_specs()
    ar = [s for s in specs["运营"] if s[1] == "应收周转天数"][0]
    inv = [s for s in specs["运营"] if s[1] == "存货周转天数"][0]
    assert ar[2] > ar[3] and ar[2] >= 200    # 放宽:0分端 >= 200 天(>通用 180)
    assert inv[2] > inv[3] and inv[2] >= 300  # 放宽:0分端 >= 300 天


def test_weights_shape():
    w = mod.weights()
    assert isinstance(w, dict) and abs(sum(w.values()) - 1.0) < 1e-6


def test_skip_flags():
    # 回款慢=CFO 常年弱于利润,SKIP 通用现金含量不足(改用专属经营现金流恶化)
    assert "现金含量不足" in mod.SKIP_FLAGS


# ———————————— 专属红旗:边界 ————————————
def test_flag_order_visibility_hit_and_miss():
    assert "订单能见度下滑" in _codes(mod.extra_flags({"合同负债环比": -25.0}, {}))
    assert "订单能见度下滑" not in _codes(mod.extra_flags({"合同负债环比": -5.0}, {}))
    assert "订单能见度下滑" not in _codes(mod.extra_flags({"合同负债环比": 10.0}, {}))


def test_flag_receivable_deterioration_hit_and_miss():
    # 命中:应收增速 40、营收增速 10(差 30 > 20)
    assert "应收回款恶化" in _codes(mod.extra_flags({"应收增速": 40.0, "营收增速": 10.0}, {}))
    # 不命中:应收紧跟营收
    assert "应收回款恶化" not in _codes(mod.extra_flags({"应收增速": 15.0, "营收增速": 10.0}, {}))


def test_flag_inventory_pileup_hit_and_miss():
    # 命中:存货增速 50、营收 10(差 40 > 25)
    assert "存货积压" in _codes(mod.extra_flags({"存货增速": 50.0, "营收增速": 10.0}, {}))
    assert "存货积压" not in _codes(mod.extra_flags({"存货增速": 20.0, "营收增速": 10.0}, {}))


def test_flag_rd_underinvest_hit_and_miss():
    # 命中:研发费用率 2% < 3%(军工技术壁垒隐忧)
    assert "研发投入不足" in _codes(mod.extra_flags({"研发费用率": 2.0}, {}))
    # 不命中:研发充足
    assert "研发投入不足" not in _codes(mod.extra_flags({"研发费用率": 8.0}, {}))
    # 不命中:研发费用率为负(异常/缺,不误判)
    assert "研发投入不足" not in _codes(mod.extra_flags({"研发费用率": -1.0}, {}))


def test_flag_margin_pressure_hit_and_miss():
    assert "毛利率承压" in _codes(mod.extra_flags({"毛利率": 6.0}, {}))
    assert "毛利率承压" not in _codes(mod.extra_flags({"毛利率": 25.0}, {}))


def test_flag_cfo_deterioration_hit_and_miss():
    # 专属:归母为正而 CFO 为负才发声(更严,避免误杀回款慢的正常票)
    hit = mod.extra_flags({}, {"利润表": {"归母净利润": 5e8},
                               "现金流量表": {"经营活动现金流量净额": -3e8}})
    assert "经营现金流恶化" in _codes(hit)
    miss = mod.extra_flags({}, {"利润表": {"归母净利润": 5e8},
                                "现金流量表": {"经营活动现金流量净额": 2e8}})
    assert "经营现金流恶化" not in _codes(miss)


# ———————————— 缺值/半空 ————————————
def test_extra_flags_empty_inputs_no_raise():
    assert isinstance(mod.extra_flags({}, {}), list)
    assert isinstance(mod.extra_flags(None, None), list)
    empty = mod.extra_flags({}, {"利润表": {}, "现金流量表": {}})
    assert isinstance(empty, list) and empty == []


def test_extra_flags_output_shape():
    for f in mod.extra_flags({"合同负债环比": -30.0, "研发费用率": 1.0, "毛利率": 5.0}, {}):
        assert set(f) >= {"code", "命中", "严重度", "值"}
        assert f["严重度"] in ("高", "中", "低")
