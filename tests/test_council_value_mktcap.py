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


# ————————————————————————————————————————————————————————————————
# 监控落盘:锁住"须持续监控风格回撤"这条安全纪律真的可执行——
# 读数不再只活在即时 console log,而是落按日视图「估值位监控」、可事后按交易日回读。
# (背景:开启前该读数被 run_factor 丢弃、全库无承接,注释还指向不存在的 view;本组测试防回归。)
# ————————————————————————————————————————————————————————————————
def test_estvalue_monitor_persisted_and_readable(analysis_tmpdir):
    """监控读数落按日视图「估值位监控」,store.get_view 可按交易日回读(含小市值倾斜+as_of)。"""
    from tools.store import repo as store
    mon = {"启用": True, "总市值覆盖": 1.0, "全池中位总市值_亿": 300.0,
           "高价值票中位总市值_亿": 30.0, "小市值倾斜": 0.1}
    p = sc._persist_estvalue_monitor(mon, "2026-09-10")
    assert p is not None
    back = store.get_view("估值位监控", "2026-09-10")
    assert back["小市值倾斜"] == 0.1 and back["as_of"] == "2026-09-10"


def test_estvalue_monitor_not_persisted_when_none(analysis_tmpdir):
    """开关关(mon=None,无敞口)→ 不落盘,不误建空视图。"""
    from tools.store import repo as store
    assert sc._persist_estvalue_monitor(None, "2026-09-10") is None
    with pytest.raises(FileNotFoundError):
        store.get_view("估值位监控", "2026-09-10")


def test_precompute_persists_estvalue_monitor(monkeypatch, analysis_tmpdir, mktcap_flag):
    """端到端锁落盘链路:跑 precompute(开开关)→「估值位监控」按日视图可回读,
    且值与返回一致——防"函数在、但没接进 precompute"这类静默断链。"""
    from tools.analysis import serialize
    from tools.collectors import market
    from tools.store import repo as store

    mktcap_flag(True)
    recs = {"000001": _rec(20.0, 2.0, 30.0),      # 高价值=小市值
            "000002": _rec(20.0, 2.0, 300.0),
            "000003": _rec(20.0, 2.0, 3000.0)}
    monkeypatch.setattr(serialize, "load_record", lambda c, date=None: recs[c])

    def _no_kline(c):
        raise FileNotFoundError                    # K线缺失→kdf=None(不影响价值维/市值分位)

    monkeypatch.setattr(market, "load_kline_recent", _no_kline)

    r = sc.precompute(as_of="2026-09-10", codes=list(recs))
    assert r["估值位监控"]["小市值倾斜"] < 1.0     # 高价值票偏小盘→敞口存在
    back = store.get_view("估值位监控", "2026-09-10")
    assert back["小市值倾斜"] == r["估值位监控"]["小市值倾斜"]
    assert back["as_of"] == "2026-09-10"
