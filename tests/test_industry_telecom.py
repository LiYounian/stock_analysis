"""通信(光模块/ICT 设备)财报专家模块单测。

锁语义:
  - 契约:KEY 为规范名"通信"、dimension_specs 结构合法、weights 为 dict/None、SKIP_FLAGS 合法;
  - 每条专属红旗各有一组"命中/不命中"边界断言(防未来重写误删规则);
  - 空/全空输入不抛异常、返回 list;
  - 阈值为工程占位,断言只锁"越界命中、界内不命中"的语义方向,不锁具体数值。
纯函数、无网络。
"""
import pytest

from tools.analysis.financial.industry import 通信 as telecom
from tools.analysis.financial.industry import get_expert


# 通用红旗名合法清单(SKIP_FLAGS 只能从中选)
_VALID_FLAG_NAMES = {
    "增收不增利", "现金含量不足", "应收存货激增", "商誉高企", "高负债",
    "扣非占比低", "短债覆盖不足", "扣非为负", "非标审计意见", "毛利率异常跳升",
}
_VALID_DIMS = {"成长", "质量", "健康", "运营", "回报"}
_EMPTY_STRUCTURED = {"利润表": {}, "资产负债表": {}, "现金流量表": {}}


# ———————————————————— 契约 ————————————————————
def test_key_is_canonical():
    assert telecom.KEY == "通信"


def test_registered_and_routable():
    # 自动发现:按 KEY 应能路由到本模块
    assert get_expert("通信") is telecom


def test_dimension_specs_structure():
    specs = telecom.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        assert dim in _VALID_DIMS
        assert isinstance(subs, list) and subs
        for sub in subs:
            assert isinstance(sub, tuple) and len(sub) == 4
            name, key, lo, hi = sub
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_dimension_specs_has_reverse_indicators():
    # 反向指标(资产负债率/周转天数/应收营收增速差)应写成 0分端 > 100分端
    specs = telecom.dimension_specs()
    rev_keys = {"资产负债率", "存货周转天数", "应收周转天数", "应收营收增速差"}
    seen = set()
    for subs in specs.values():
        for _n, key, lo, hi in subs:
            if key in rev_keys:
                assert lo > hi, f"{key} 反向区间应 0分端>100分端"
                seen.add(key)
    assert rev_keys <= seen, f"缺反向指标: {rev_keys - seen}"


def test_weights_shape_and_sum():
    w = telecom.weights()
    assert w is None or isinstance(w, dict)
    if isinstance(w, dict):
        assert set(w) <= _VALID_DIMS
        assert all(isinstance(v, (int, float)) for v in w.values())
        assert abs(sum(w.values()) - 1.0) < 1e-6  # 本模块用归一权重


def test_skip_flags_valid():
    assert isinstance(telecom.SKIP_FLAGS, list)
    for name in telecom.SKIP_FLAGS:
        assert name in _VALID_FLAG_NAMES
    assert "应收存货激增" in telecom.SKIP_FLAGS  # 备货重,通用激增红旗改由本模块接管


# ———————————————————— 健壮性 ————————————————————
def test_extra_flags_empty_inputs_no_raise():
    assert telecom.extra_flags({}, {}) == []
    assert telecom.extra_flags({}, _EMPTY_STRUCTURED) == []
    assert isinstance(telecom.extra_flags(None, None), list)


def _codes(flags):
    return {f["code"] for f in flags}


def _find(flags, code):
    return next((f for f in flags if f["code"] == code), None)


def test_all_emitted_flags_are_wellformed():
    # 造一组"全踩雷"的 derived,校验每条红旗结构成形
    derived = {
        "营收增速": 60.0, "扣非净利增速": 5.0, "毛利率同比升": -5.0,
        "存货增速": 120.0, "应收营收增速差": 35.0, "合同负债环比": -25.0,
        "研发费用率": 1.5,
    }
    flags = telecom.extra_flags(derived, _EMPTY_STRUCTURED)
    assert flags and isinstance(flags, list)
    for f in flags:
        assert set(f) >= {"code", "命中", "严重度", "值"}
        assert f["命中"] is True
        assert f["严重度"] in ("高", "中", "低")
        assert isinstance(f["值"], dict)


# ———————————————————— 逐条专属红旗 命中/不命中 ————————————————————
def test_flag_gross_margin_drop():
    # 命中:毛利率同比降幅达阈值(-3pct)
    assert "毛利率下滑" in _codes(telecom.extra_flags({"毛利率同比升": -4.0}, {}))
    f = _find(telecom.extra_flags({"毛利率同比升": -4.0}, {}), "毛利率下滑")
    assert f["严重度"] == "高"
    # 不命中:小幅波动 / 缺失
    assert "毛利率下滑" not in _codes(telecom.extra_flags({"毛利率同比升": -1.0}, {}))
    assert "毛利率下滑" not in _codes(telecom.extra_flags({}, {}))


def test_flag_inventory_glut():
    # 命中:存货增速远超营收 且 毛利率走弱
    d = {"营收增速": 40.0, "存货增速": 90.0, "毛利率同比升": -2.0}
    assert "存货畸高滞销" in _codes(telecom.extra_flags(d, {}))
    # 不命中:存货虽超但毛利率未走弱(良性备货,不误杀)
    d2 = {"营收增速": 40.0, "存货增速": 90.0, "毛利率同比升": 1.0}
    assert "存货畸高滞销" not in _codes(telecom.extra_flags(d2, {}))
    # 不命中:差值不足阈值
    d3 = {"营收增速": 40.0, "存货增速": 50.0, "毛利率同比升": -2.0}
    assert "存货畸高滞销" not in _codes(telecom.extra_flags(d3, {}))


def test_flag_receivables_surge():
    # 命中:应收营收增速差 > 20pct
    assert "应收暴增于营收" in _codes(telecom.extra_flags({"应收营收增速差": 25.0}, {}))
    # 命中(退化路径):无现成差值,用原子相减
    d = {"应收增速": 80.0, "营收增速": 50.0}
    assert "应收暴增于营收" in _codes(telecom.extra_flags(d, {}))
    # 不命中:差值在界内
    assert "应收暴增于营收" not in _codes(telecom.extra_flags({"应收营收增速差": 10.0}, {}))


def test_flag_contract_liability_weakening():
    # 命中:合同负债环比大幅负增(景气见顶)
    assert "合同负债转弱" in _codes(telecom.extra_flags({"合同负债环比": -20.0}, {}))
    # 不命中:环比正增 / 小幅
    assert "合同负债转弱" not in _codes(telecom.extra_flags({"合同负债环比": 5.0}, {}))
    assert "合同负债转弱" not in _codes(telecom.extra_flags({"合同负债环比": -3.0}, {}))


def test_flag_weak_rd():
    # 命中:营收高增但研发费用率偏低
    d = {"营收增速": 60.0, "研发费用率": 1.5}
    f = _find(telecom.extra_flags(d, {}), "研发投入偏弱")
    assert f is not None and f["严重度"] == "低"
    # 不命中:研发费用率充足
    assert "研发投入偏弱" not in _codes(telecom.extra_flags({"营收增速": 60.0, "研发费用率": 8.0}, {}))
    # 不命中:营收未高增(低增期不苛求研发强度)
    assert "研发投入偏弱" not in _codes(telecom.extra_flags({"营收增速": 5.0, "研发费用率": 1.5}, {}))


def test_flag_profit_divergence():
    # 命中:营收高增但扣非增速显著落后
    d = {"营收增速": 60.0, "扣非净利增速": 5.0}
    assert "增收利润背离" in _codes(telecom.extra_flags(d, {}))
    # 不命中:利润跟得上营收
    assert "增收利润背离" not in _codes(telecom.extra_flags({"营收增速": 60.0, "扣非净利增速": 55.0}, {}))
    # 不命中:营收未高增
    assert "增收利润背离" not in _codes(telecom.extra_flags({"营收增速": 10.0, "扣非净利增速": -40.0}, {}))
