"""合议「多因子专家」价值维加市值分位(REVS阶段1b V入合议)单测。

断言锁住"为什么这么设计":
  · 开关默认关 → raw_factors 不输出总市值 → 截面自动跳过 → 价值因子退回纯 PE/PB(与旧行为逐票等价)。
  · 开关开 → 价值因子含市值分位,方向 -1(小市值高分);同 PE/PB 下小市值票价值分更高。
  · 开关是 no-op 安全:关时市值差异完全不影响价值因子/综合分。
"""
from __future__ import annotations

import pytest

from tools.analysis.factor import factor as fac
from tools.analysis.factor import score as sc
from tools.config.strategy import THRESHOLDS

_MF = THRESHOLDS["合议"]["多因子"]


@pytest.fixture
def mktcap_flag():
    """切换「价值市值分位启用」,用完还原(不污染其它用例/全局配置)。"""
    orig = _MF.get("价值市值分位启用", False)

    def _set(v):
        _MF["价值市值分位启用"] = v
    yield _set
    _MF["价值市值分位启用"] = orig


def _rec(pe, pb, mktcap_yi):
    return {"valuation": {"pe_ttm": pe, "pb": pb, "mktcap_yi": mktcap_yi},
            "fundamental": {}}


def test_config_value_has_mktcap_key():
    """config 价值因子已挂总市值(市值分位原料);权重块不受影响。"""
    assert _MF["因子"]["价值"].get("总市值") == -1


def test_raw_factors_gated_off(mktcap_flag):
    """开关关:raw_factors 总市值=None(不输出,截面会跳过)。"""
    mktcap_flag(False)
    r = fac.raw_factors(_rec(20.0, 2.0, 100.0))
    assert r["总市值"] is None
    assert r["PE_TTM"] == 20.0 and r["PB"] == 2.0     # PE/PB 恒输出


def test_raw_factors_gated_on(mktcap_flag):
    """开关开:raw_factors 输出总市值(市值分位原料)。"""
    mktcap_flag(True)
    r = fac.raw_factors(_rec(20.0, 2.0, 100.0))
    assert r["总市值"] == 100.0


def test_value_factor_smallcap_higher_when_on(mktcap_flag):
    """开关开:同 PE/PB 下,小市值票的价值因子分更高(市值分位方向 -1)。"""
    mktcap_flag(True)
    raw = {
        "SMALL": fac.raw_factors(_rec(20.0, 2.0, 30.0)),    # 小市值
        "MID": fac.raw_factors(_rec(20.0, 2.0, 300.0)),
        "BIG": fac.raw_factors(_rec(20.0, 2.0, 3000.0)),    # 大市值
    }
    out = sc.cross_section(raw)
    v_small = out["SMALL"]["各因子分位"]["价值"]
    v_big = out["BIG"]["各因子分位"]["价值"]
    assert v_small > v_big                               # 小市值 → 价值分更高


def test_estvalue_monitor_smallcap_tilt(mktcap_flag):
    """估值位监控:高价值票偏小盘 → 小市值倾斜<1(风格回撤敞口可观测)。"""
    mktcap_flag(True)
    raw = {"A": {"总市值": 30.0}, "B": {"总市值": 300.0}, "C": {"总市值": 3000.0}}
    scored = {"A": {"各因子分位": {"价值": 0.9}},   # 高价值=小市值
              "B": {"各因子分位": {"价值": 0.5}},
              "C": {"各因子分位": {"价值": 0.1}}}
    mon = sc._estvalue_monitor(raw, scored)
    assert mon["启用"] is True and mon["总市值覆盖"] == 1.0
    assert mon["小市值倾斜"] < 1.0                    # 高价值票中位市值 < 全池中位 → 偏小盘


def test_estvalue_monitor_off_returns_none(mktcap_flag):
    """开关关 → 监控返回 None(不误报)。"""
    mktcap_flag(False)
    assert sc._estvalue_monitor({"A": {"总市值": 30.0}},
                                {"A": {"各因子分位": {"价值": 0.9}}}) is None


def test_value_factor_unchanged_when_off(mktcap_flag):
    """开关关:市值差异完全不影响价值因子(no-op 安全;PE/PB 相同→价值分相等)。"""
    mktcap_flag(False)
    raw = {
        "SMALL": fac.raw_factors(_rec(20.0, 2.0, 30.0)),
        "BIG": fac.raw_factors(_rec(20.0, 2.0, 3000.0)),
    }
    out = sc.cross_section(raw)
    # PE/PB 相同 + 总市值不参与 → 两票价值因子分位相等(市值差异被忽略)
    assert out["SMALL"]["各因子分位"]["价值"] == out["BIG"]["各因子分位"]["价值"]
