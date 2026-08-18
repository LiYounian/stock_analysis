"""电子行业财报专家单测(锁住契约结构 + 每条专属红旗的命中/不命中语义)。

对齐 docs/财报分析专家/模块开发规范.md §6:
  - dimension_specs() 结构正确、KEY 是规范名;weights() 返回 dict/None;
  - 每条专属红旗至少一个"命中/不命中"边界断言;
  - extra_flags({}, {}) 与空三大表不抛异常、返回 list。
"""
from __future__ import annotations

from tools.analysis.financial.industry import 电子 as mod


# ── 契约结构 ────────────────────────────────────────────────
def test_key_是规范名():
    assert mod.KEY == "电子"
    assert isinstance(mod.NOTE, str) and mod.NOTE


def test_dimension_specs_结构():
    specs = mod.dimension_specs()
    assert isinstance(specs, dict) and specs
    # 沿用五维骨架
    assert set(specs) == {"成长", "质量", "健康", "运营", "回报"}
    for dim, subs in specs.items():
        assert isinstance(subs, list) and subs, dim
        for sub in subs:
            assert len(sub) == 4, sub
            名, 键, lo, hi = sub
            assert isinstance(名, str) and 名
            assert isinstance(键, str) and 键
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_质量维含研发费用率():
    """电子强化点:质量维必须含研发费用率子指标。"""
    键集 = {键 for (_名, 键, _lo, _hi) in mod.dimension_specs()["质量"]}
    assert "研发费用率" in 键集
    assert "毛利率" in 键集


def test_weights_返回dict或None():
    w = mod.weights()
    assert w is None or isinstance(w, dict)
    if isinstance(w, dict):
        # 质量/运营略加权
        assert w["质量"] >= 1.0 and w["运营"] >= 1.0


def test_skip_flags_是list():
    assert isinstance(mod.SKIP_FLAGS, list)
    # 电子(制造业)不做金融式大规模跳过
    assert mod.SKIP_FLAGS == []


# ── 空输入健壮性 ────────────────────────────────────────────
def test_extra_flags_空输入不抛():
    assert mod.extra_flags({}, {}) == []
    r = mod.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}})
    assert isinstance(r, list) and r == []
    # None 也不炸(纯函数兜底)
    assert isinstance(mod.extra_flags(None, None), list)


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


# ── 专属红旗:存货高企 ──────────────────────────────────────
def test_存货高企_命中():
    d = {"营收增速": 5.0, "存货增速": 40.0}   # 差值 35 > 15
    codes = _codes(mod.extra_flags(d, {}))
    assert "存货高企" in codes


def test_存货高企_叠加毛利率下滑升级为高():
    d = {"营收增速": 5.0, "存货增速": 40.0, "毛利率同比升": -6.0}
    f = next(x for x in mod.extra_flags(d, {}) if x["code"] == "存货高企")
    assert f["严重度"] == "高"


def test_存货高企_不命中():
    d = {"营收增速": 30.0, "存货增速": 35.0}   # 差值 5 < 15
    assert "存货高企" not in _codes(mod.extra_flags(d, {}))


# ── 专属红旗:毛利率持续下滑 ────────────────────────────────
def test_毛利率持续下滑_命中():
    d = {"毛利率同比升": -8.0}
    assert "毛利率持续下滑" in _codes(mod.extra_flags(d, {}))


def test_毛利率持续下滑_不命中():
    d = {"毛利率同比升": -2.0}   # 降幅 < 5pct
    assert "毛利率持续下滑" not in _codes(mod.extra_flags(d, {}))


# ── 专属红旗:研发占比过低 ──────────────────────────────────
def test_研发占比过低_命中():
    d = {"研发费用率": 1.5}   # < 3%
    assert "研发占比过低" in _codes(mod.extra_flags(d, {}))


def test_研发占比过低_不命中():
    d = {"研发费用率": 6.0}
    assert "研发占比过低" not in _codes(mod.extra_flags(d, {}))


# ── 专属红旗:存货减值高企 ──────────────────────────────────
def test_存货减值高企_命中_年报():
    s = {"report_type": "年报",
         "利润表": {"营业总收入": 1_000_000_000.0, "资产减值损失": -30_000_000.0}}  # 3% > 2%
    assert "存货减值高企" in _codes(mod.extra_flags({}, s))


def test_存货减值高企_季报不判():
    """减值明细仅年报/半报披露,季报期不判(避免误杀)。"""
    s = {"report_type": "一季报",
         "利润表": {"营业总收入": 1_000_000_000.0, "资产减值损失": -30_000_000.0}}
    assert "存货减值高企" not in _codes(mod.extra_flags({}, s))


def test_存货减值高企_占比不足不命中():
    s = {"report_type": "年报",
         "利润表": {"营业总收入": 1_000_000_000.0, "资产减值损失": -5_000_000.0}}  # 0.5% < 2%
    assert "存货减值高企" not in _codes(mod.extra_flags({}, s))


# ── 专属红旗:扩产错配 ──────────────────────────────────────
def test_扩产错配_命中():
    d = {"营收增速": -10.0}   # 营收走弱
    s = {"资产负债表": {"在建工程": 60_000_000.0, "固定资产": 100_000_000.0}}  # 0.6 > 0.35
    assert "扩产错配" in _codes(mod.extra_flags(d, s))


def test_扩产错配_营收增长时不命中():
    d = {"营收增速": 20.0}   # 营收增长,扩产被消化
    s = {"资产负债表": {"在建工程": 60_000_000.0, "固定资产": 100_000_000.0}}
    assert "扩产错配" not in _codes(mod.extra_flags(d, s))


def test_扩产错配_在建占比低不命中():
    d = {"营收增速": -10.0}
    s = {"资产负债表": {"在建工程": 10_000_000.0, "固定资产": 100_000_000.0}}  # 0.1 < 0.35
    assert "扩产错配" not in _codes(mod.extra_flags(d, s))


# ── 返回结构合法性 ──────────────────────────────────────────
def test_红旗结构合法():
    d = {"营收增速": 5.0, "存货增速": 40.0, "研发费用率": 1.0, "毛利率同比升": -8.0}
    s = {"report_type": "年报",
         "利润表": {"营业总收入": 1_000_000_000.0, "资产减值损失": -30_000_000.0},
         "资产负债表": {"在建工程": 60_000_000.0, "固定资产": 100_000_000.0}}
    for f in mod.extra_flags(d, s):
        assert set(f) >= {"code", "命中", "严重度", "值"}
        assert f["命中"] is True
        assert f["严重度"] in {"高", "中", "低"}
