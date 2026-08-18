"""交通运输(航运/油运)财报专家模块单测。

锁语义:
  - 契约结构正确(KEY 为申万一级规范名、五维区间格式、weights dict/None、SKIP_FLAGS ⊆ 通用清单);
  - 每条专属红旗至少一个命中/不命中边界断言(防未来重写无意删规则);
  - 空输入 / 全空三大表不抛异常、返回 list;
  - 模块能被行业注册表自动发现并按 KEY 取到。
纯函数,不触网、不读盘。
"""
import pytest

from tools.analysis.financial.industry import 交通运输 as m
from tools.analysis.financial.industry import get_expert

# 通用红旗名清单(规范 §4);SKIP_FLAGS 只能从这里选
_GENERIC_FLAGS = {
    "增收不增利", "现金含量不足", "应收存货激增", "商誉高企", "高负债",
    "扣非占比低", "短债覆盖不足", "扣非为负", "非标审计意见", "毛利率异常跳升",
}


# ———————————— 契约结构 ————————————
def test_key_is_canonical():
    assert m.KEY == "交通运输"
    assert isinstance(m.NOTE, str) and m.NOTE


def test_dimension_specs_shape():
    specs = m.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        assert isinstance(dim, str)
        assert isinstance(subs, list) and subs
        for item in subs:
            assert len(item) == 4
            name, key, lo, hi = item
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_dimension_specs_reverse_metrics():
    # 反向指标 0分端 > 100分端:资产负债率、应收周转天数
    specs = m.dimension_specs()
    dar = dict((k, (lo, hi)) for _n, k, lo, hi in specs["健康"])["资产负债率"]
    assert dar[0] > dar[1]
    ar = dict((k, (lo, hi)) for _n, k, lo, hi in specs["运营"])["应收周转天数"]
    assert ar[0] > ar[1]


def test_weights_dict_or_none():
    w = m.weights()
    assert w is None or isinstance(w, dict)
    if isinstance(w, dict):
        # 健康维度权重应最高(高杠杆是周期股命门)
        assert w["健康"] >= max(w.values())


def test_skip_flags_subset_of_generic():
    assert isinstance(m.SKIP_FLAGS, list)
    assert set(m.SKIP_FLAGS) <= _GENERIC_FLAGS
    # 强周期特性:毛利率跳升属常态应跳过;短债覆盖交由专属重资产版处理
    assert "毛利率异常跳升" in m.SKIP_FLAGS
    assert "短债覆盖不足" in m.SKIP_FLAGS


def test_registry_autodiscovery():
    assert get_expert("交通运输") is m


# ———————————— extra_flags 鲁棒性 ————————————
def test_extra_flags_empty_inputs():
    assert m.extra_flags({}, {}) == []
    assert isinstance(m.extra_flags(None, None), list)
    empty = {"利润表": {}, "资产负债表": {}, "现金流量表": {}}
    assert m.extra_flags({}, empty) == []


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


# ———————————— 专属红旗 1:运价下行致盈利崩塌 ————————————
def test_flag_profit_collapse_by_yoy():
    hit = m.extra_flags({"归母净利增速": -70.0}, {})
    assert "运价下行致盈利崩塌" in _codes(hit)


def test_flag_profit_collapse_by_qoq_and_low_margin():
    hit = m.extra_flags({"单季营收环比": -40.0, "毛利率": 5.0}, {})
    assert "运价下行致盈利崩塌" in _codes(hit)


def test_flag_profit_collapse_not_fired_when_healthy():
    hit = m.extra_flags({"归母净利增速": 20.0, "单季营收环比": 5.0, "毛利率": 30.0}, {})
    assert "运价下行致盈利崩塌" not in _codes(hit)


# ———————————— 专属红旗 2:高杠杆叠加运价下行 ————————————
def test_flag_davis_double_kill_fires():
    hit = m.extra_flags({"资产负债率": 72.0, "归母净利增速": -10.0}, {})
    assert "高杠杆叠加运价下行" in _codes(hit)


def test_flag_davis_double_kill_not_fired_low_leverage():
    # 杠杆不高即便盈利下行也不触发本项
    hit = m.extra_flags({"资产负债率": 50.0, "归母净利增速": -10.0}, {})
    assert "高杠杆叠加运价下行" not in _codes(hit)


def test_flag_davis_double_kill_not_fired_when_profit_up():
    hit = m.extra_flags({"资产负债率": 72.0, "归母净利增速": 15.0, "单季营收环比": 8.0}, {})
    assert "高杠杆叠加运价下行" not in _codes(hit)


# ———————————— 专属红旗 3:有息负债高企且短债覆盖不足 ————————————
def test_flag_debt_coverage_fires():
    s = {"资产负债表": {"货币资金": 10.0, "短期借款": 30.0, "一年内到期非流动负债": 10.0,
                    "长期借款": 50.0, "应付债券": 20.0, "资产总计": 300.0}}
    hit = m.extra_flags({}, s)
    assert "有息负债高企且短债覆盖不足" in _codes(hit)


def test_flag_debt_coverage_not_fired_when_cash_covers():
    # 货币资金足以覆盖短期有息 → 不触发
    s = {"资产负债表": {"货币资金": 100.0, "短期借款": 30.0, "一年内到期非流动负债": 10.0,
                    "长期借款": 50.0, "应付债券": 20.0, "资产总计": 300.0}}
    hit = m.extra_flags({}, s)
    assert "有息负债高企且短债覆盖不足" not in _codes(hit)


# ———————————— 专属红旗 4:经营现金流转负 ————————————
def test_flag_negative_operating_cf_fires():
    hit = m.extra_flags({}, {"现金流量表": {"经营活动现金流量净额": -1234.0}})
    assert "经营现金流转负" in _codes(hit)


def test_flag_negative_operating_cf_not_fired_when_positive():
    hit = m.extra_flags({}, {"现金流量表": {"经营活动现金流量净额": 5000.0}})
    assert "经营现金流转负" not in _codes(hit)


# ———————————— 专属红旗 5:折旧占营收过高 ————————————
def test_flag_high_depreciation_fires():
    s = {"现金流量表": {"固定资产折旧": 40.0, "无形资产摊销": 5.0},
         "利润表": {"营业总收入": 100.0}}
    hit = m.extra_flags({}, s)
    assert "折旧占营收过高" in _codes(hit)


def test_flag_high_depreciation_not_fired_when_low():
    s = {"现金流量表": {"固定资产折旧": 5.0, "无形资产摊销": 1.0},
         "利润表": {"营业总收入": 100.0}}
    hit = m.extra_flags({}, s)
    assert "折旧占营收过高" not in _codes(hit)


# ———————————— 输出结构规范 ————————————
def test_flag_output_schema():
    hit = m.extra_flags({"归母净利增速": -80.0}, {})
    assert hit
    for f in hit:
        assert set(f) >= {"code", "命中", "严重度", "值"}
        assert f["命中"] is True
        assert f["严重度"] in ("高", "中", "低")
