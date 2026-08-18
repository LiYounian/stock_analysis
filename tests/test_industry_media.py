"""传媒(AI 应用 / 游戏)财报专家模块单测。

锁语义:
  - 契约结构正确(KEY 是规范名、dimension_specs 结构、weights 类型);
  - 每条专属红旗至少一组"命中/不命中"边界断言(防未来重写无意删规则);
  - extra_flags 对空输入 / 全空三表不抛异常、返回 list;
  - SKIP_FLAGS 只含合法通用红旗名,且不误跳"商誉高企"(传媒头号雷)。
纯函数、不触网、不读盘。
"""
import importlib

import pytest

media = importlib.import_module("tools.analysis.financial.industry.传媒")

# 通用红旗名清单(SKIP_FLAGS 只能从这里选)
_GENERIC_FLAGS = {
    "增收不增利", "现金含量不足", "应收存货激增", "商誉高企", "高负债",
    "扣非占比低", "短债覆盖不足", "扣非为负", "非标审计意见", "毛利率异常跳升",
}


def _empty_structured() -> dict:
    return {"利润表": {}, "资产负债表": {}, "现金流量表": {}}


# ———————————— 契约结构 ————————————
def test_key_is_canonical():
    assert media.KEY == "传媒"
    assert isinstance(media.NOTE, str) and media.NOTE


def test_dimension_specs_structure():
    specs = media.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        assert isinstance(dim, str)
        assert isinstance(subs, list) and subs
        for item in subs:
            assert len(item) == 4
            name, key, lo, hi = item
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_dimension_specs_five_dims_and_media_tuning():
    specs = media.dimension_specs()
    assert set(specs.keys()) == {"成长", "质量", "健康", "运营", "回报"}
    # 质量维毛利率区间上移(轻资产高毛利):0 分端应显著高于通用的 0
    毛利率 = [t for t in specs["质量"] if t[1] == "毛利率"][0]
    assert 毛利率[2] >= 30
    # 健康维含商誉占净资产,且为反向(0 分端 > 100 分端)
    商誉 = [t for t in specs["健康"] if t[1] == "商誉占净资产"][0]
    assert 商誉[2] > 商誉[3]
    # 运营维用合同负债环比替代周转
    keys_运营 = {t[1] for t in specs["运营"]}
    assert "合同负债环比" in keys_运营
    assert "应收周转天数" not in keys_运营 and "存货周转天数" not in keys_运营


def test_weights_type_and_sum():
    w = media.weights()
    assert isinstance(w, dict)
    assert set(w.keys()) == {"成长", "质量", "健康", "运营", "回报"}
    assert abs(sum(w.values()) - 1.0) < 1e-9
    # 质量 + 健康(商誉)应为最重的组合
    assert w["质量"] >= w["运营"] and w["健康"] >= w["运营"]


def test_skip_flags_valid_and_keeps_goodwill():
    assert isinstance(media.SKIP_FLAGS, list)
    for f in media.SKIP_FLAGS:
        assert f in _GENERIC_FLAGS, f"{f} 非合法通用红旗名"
    # 商誉高企是传媒头号雷,绝不能跳
    assert "商誉高企" not in media.SKIP_FLAGS
    # 应收存货激增(游戏无存货)应被跳过
    assert "应收存货激增" in media.SKIP_FLAGS


# ———————————— 专属红旗:商誉减值风险 ————————————
def test_goodwill_impairment_hit():
    derived = {"商誉占净资产": 45.0}
    structured = {"利润表": {"归母净利润": 1_000_000.0, "资产减值损失": -400_000.0},
                  "资产负债表": {"商誉": 5_000_000.0}, "现金流量表": {}}
    codes = {f["code"]: f for f in media.extra_flags(derived, structured)}
    assert "商誉减值风险" in codes
    assert codes["商誉减值风险"]["严重度"] == "高"  # 商誉>40% → 高危


def test_goodwill_impairment_medium_when_ratio_low():
    # 有商誉且减值显著,但商誉占净资产未过 40% → 中危
    derived = {"商誉占净资产": 20.0}
    structured = {"利润表": {"归母净利润": 1_000_000.0, "资产减值损失": -300_000.0},
                  "资产负债表": {"商誉": 2_000_000.0}, "现金流量表": {}}
    codes = {f["code"]: f for f in media.extra_flags(derived, structured)}
    assert codes.get("商誉减值风险", {}).get("严重度") == "中"


def test_goodwill_impairment_no_hit_when_no_impairment():
    # 商誉高但当期无减值 → 不命中(减值风险专条只在减值发生时升)
    derived = {"商誉占净资产": 45.0}
    structured = {"利润表": {"归母净利润": 1_000_000.0, "资产减值损失": 0.0},
                  "资产负债表": {"商誉": 5_000_000.0}, "现金流量表": {}}
    codes = {f["code"] for f in media.extra_flags(derived, structured)}
    assert "商誉减值风险" not in codes


# ———————————— 专属红旗:投资收益撑利润 ————————————
def test_invest_income_prop_hit():
    structured = {"利润表": {"投资收益": 800_000.0, "营业利润": 1_000_000.0},
                  "资产负债表": {}, "现金流量表": {}}
    codes = {f["code"] for f in media.extra_flags({}, structured)}
    assert "投资收益撑利润" in codes


def test_invest_income_prop_no_hit_low_share():
    structured = {"利润表": {"投资收益": 100_000.0, "营业利润": 1_000_000.0},
                  "资产负债表": {}, "现金流量表": {}}
    codes = {f["code"] for f in media.extra_flags({}, structured)}
    assert "投资收益撑利润" not in codes


def test_invest_income_prop_no_hit_when_op_negative():
    # 营业利润为负时占比无意义,不命中
    structured = {"利润表": {"投资收益": 800_000.0, "营业利润": -500_000.0},
                  "资产负债表": {}, "现金流量表": {}}
    codes = {f["code"] for f in media.extra_flags({}, structured)}
    assert "投资收益撑利润" not in codes


# ———————————— 专属红旗:合同负债转弱 ————————————
def test_contract_liab_weakening_hit():
    codes = {f["code"] for f in media.extra_flags({"合同负债环比": -25.0}, _empty_structured())}
    assert "合同负债转弱" in codes


def test_contract_liab_weakening_no_hit_when_growing():
    codes = {f["code"] for f in media.extra_flags({"合同负债环比": 10.0}, _empty_structured())}
    assert "合同负债转弱" not in codes


# ———————————— 专属红旗:买量过重 ————————————
def test_marketing_overweight_hit():
    structured = {"利润表": {"销售费用": 600_000.0, "营业收入": 1_000_000.0},
                  "资产负债表": {}, "现金流量表": {}}
    codes = {f["code"] for f in media.extra_flags({}, structured)}
    assert "买量过重" in codes


def test_marketing_overweight_no_hit_when_moderate():
    structured = {"利润表": {"销售费用": 200_000.0, "营业收入": 1_000_000.0},
                  "资产负债表": {}, "现金流量表": {}}
    codes = {f["code"] for f in media.extra_flags({}, structured)}
    assert "买量过重" not in codes


# ———————————— 健壮性:空输入不抛异常 ————————————
def test_extra_flags_empty_inputs_no_raise():
    assert media.extra_flags({}, {}) == [] or isinstance(media.extra_flags({}, {}), list)
    assert isinstance(media.extra_flags({}, {}), list)
    assert isinstance(media.extra_flags({}, _empty_structured()), list)


def test_extra_flags_returns_only_hits():
    # 全部返回项都必须命中=True,且四要素齐全
    structured = {"利润表": {"投资收益": 900_000.0, "营业利润": 1_000_000.0,
                          "销售费用": 500_000.0, "营业收入": 1_000_000.0,
                          "归母净利润": 1_000_000.0, "资产减值损失": -300_000.0},
                  "资产负债表": {"商誉": 5_000_000.0}, "现金流量表": {}}
    flags = media.extra_flags({"商誉占净资产": 45.0, "合同负债环比": -30.0}, structured)
    assert flags and all(f["命中"] is True for f in flags)
    for f in flags:
        assert set(f.keys()) >= {"code", "命中", "严重度", "值"}
        assert f["严重度"] in ("高", "中", "低")


def test_module_registered_as_expert():
    # 自动发现:模块应能被注册表按 KEY 找到
    from tools.analysis.financial.industry import get_expert
    assert get_expert("传媒") is media
