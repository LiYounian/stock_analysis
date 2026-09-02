"""多策略命中「同源信号闸门」语义锁(选股汇总层)。

锁死"为什么这么设计"的语义,防未来 prompt/代码重写误删规则:
  A 口径族计数:同源(价格动量族)多策略命中只计 1 次;跨族命中计 N;非确认族不计入确认;
    未登记策略按"独立族"保守计(不误合并)。
  B 游资情绪过热前置闸:多轴**同时命中才裁决**(默认 2),单轴不触发(不误杀正常强势股);
    分轴开关;缺数据保守不误裁决;kill-switch 关即全链路 no-op。
  C 统一风控 veto 汇聚复用:龙虎榜否决 → 选股层同向否决(tier 沉底);复用开关关 → no-op。
  排序分层:clean 多维确认票 > 过热/否决票(magnitude-robust,tier 优先于命中数)。
  主验收:002084-like(同源3策略 + 过热 + 龙虎榜)不居首/被降级;002811-like(双族 clean)居首;
         正常多维命中票排序不劣化;kill-switch 关退回原命中数排序。

⚠️ 非投资建议:闸门只改选股展示/入选排序。
"""
from __future__ import annotations

import pytest

from tools.analysis import multi_strategy_gate as gate

# —— 测试固定 config(免受默认漂移)——
CFG = {
    "启用": True,
    "非确认族": ["状态参考族"],
    "过热闸": {
        "启用": True, "模式": "软降级", "降级系数": 0.3, "最少命中轴数": 2,
        "轴": {
            "超买共振": {"启用": True, "共振门槛": 2},
            "涨幅透支": {"启用": True, "bias20门槛": 20.0, "涨幅窗口": 10, "涨幅门槛%": 30.0},
            "换手极端": {"启用": True, "换手门槛%": 20.0},
            "事件博弈": {"启用": True},
            "基本面空心": {"启用": True, "高危红旗数门槛": 1},
        },
    },
    "风控汇聚复用": {"启用": True},
}
CFG_OFF = {**CFG, "启用": False}


def _feat(**kw):
    f = dict(gate._EMPTY_FEATURES)
    f.update(kw)
    return f


# ————————————————— A. 口径族计数 —————————————————
def test_同源命中只计一次():
    """002084:最大范围+动量+反转,均价格动量族 → 独立口径命中数=1(而非 3)。"""
    r = gate.independent_hit_count(["最大范围选股", "动量组合", "反转低换手组合"], CFG)
    assert r["原始命中数"] == 3
    assert r["独立口径命中数"] == 1
    assert set(r["命中族"]) == {"价格动量族"}


def test_跨族命中计多维():
    """002811:量价放量(形态量能族)+ 最大范围(价格动量族)→ 独立口径命中数=2。"""
    r = gate.independent_hit_count(["量价放量", "最大范围选股"], CFG)
    assert r["原始命中数"] == 2 and r["独立口径命中数"] == 2
    assert set(r["命中族"]) == {"形态量能族", "价格动量族"}


def test_非确认族不计入确认():
    """状态参考族(指标条件化状态排序)命中但不计入独立口径命中数。"""
    r = gate.independent_hit_count(["动量组合", "指标条件化状态排序"], CFG)
    assert r["原始命中数"] == 2
    assert r["独立口径命中数"] == 1                       # 仅价格动量族计入
    assert "状态参考族" in r["非确认族命中"]


def test_未登记策略按独立族保守计():
    """未登记策略 → 各自独立族,绝不误并入已有族。"""
    r = gate.independent_hit_count(["动量组合", "某新策略X", "某新策略Y"], CFG)
    assert r["独立口径命中数"] == 3                       # 价格动量族 + 两个独立占位族
    assert set(r["未登记策略"]) == {"某新策略X", "某新策略Y"}


def test_命中去重():
    r = gate.independent_hit_count(["动量组合", "动量组合"], CFG)
    assert r["原始命中数"] == 1 and r["独立口径命中数"] == 1


# ————————————————— B. 游资情绪过热前置闸 —————————————————
def test_过热多轴同命中触发软降级():
    """超买共振 + 涨幅透支(双轴)→ 触发软降级(默认最少命中轴数=2)。"""
    f = _feat(ob_os_verdict="超买", ob_os_resonance=2, bias20=27.5, ret_n=45.0, ret_window=10)
    v = gate.overheat_verdict(f, CFG)
    assert v["触发"] is True and v["命中轴数"] == 2
    assert v["动作"] == "软降级" and v["降级系数"] == pytest.approx(0.3)


def test_过热单轴不触发不误杀():
    """仅涨幅透支单轴(超买/换手/事件/基本面均不命中)→ 单轴 < 2 → 不触发。"""
    f = _feat(ob_os_verdict="中性", ob_os_resonance=1, bias20=30.0, ret_n=50.0, ret_window=10)
    v = gate.overheat_verdict(f, CFG)
    assert v["触发"] is False and v["命中轴数"] == 1


def test_过热换手轴():
    """换手极端 + 涨幅透支 → 双轴触发。"""
    f = _feat(bias20=25.0, ret_n=40.0, ret_window=10, turnover=35.0)
    v = gate.overheat_verdict(f, CFG)
    assert v["轴"]["换手极端"] and v["轴"]["涨幅透支"] and v["触发"] is True


def test_过热事件与基本面轴():
    """龙虎榜(事件博弈)+ 基本面空心(高危红旗)→ 双轴触发(游资空心票典型组合)。"""
    f = _feat(lhb={"triggered": True, "reason": "净买上榜"}, fund_high_flags=2)
    v = gate.overheat_verdict(f, CFG)
    assert v["轴"]["事件博弈"] and v["轴"]["基本面空心"] and v["触发"] is True


def test_过热分轴开关():
    """关涨幅透支轴 → 只剩超买单轴 → 不触发。"""
    c = {**CFG, "过热闸": {**CFG["过热闸"],
         "轴": {**CFG["过热闸"]["轴"], "涨幅透支": {"启用": False}}}}
    f = _feat(ob_os_verdict="超买", ob_os_resonance=2, bias20=40.0, ret_n=60.0, ret_window=10)
    v = gate.overheat_verdict(f, c)
    assert v["轴"]["涨幅透支"] is False and v["触发"] is False


def test_过热缺数据保守():
    v = gate.overheat_verdict(None, CFG)
    assert v["触发"] is False and v["命中轴数"] == 0


def test_过热killswitch():
    """总开关关 → 即便多轴命中也 no-op。"""
    f = _feat(ob_os_verdict="超买", ob_os_resonance=3, bias20=50.0, ret_n=80.0, turnover=40.0)
    v = gate.overheat_verdict(f, CFG_OFF)
    assert v["应用"] is False and v["触发"] is False


# ————————————————— C. veto 汇聚复用 —————————————————
def test_veto_龙虎榜否决():
    """龙虎榜净买上榜裁决(触发)+ 风控汇聚龙虎榜轴为否决模式 → 选股层否决。"""
    # 依赖 config 风控汇聚.龙虎榜.模式;若默认为降权则否决=False,此处只断言"应用"链路通。
    v = gate.veto_verdict(0, {"triggered": True, "reason": "净买上榜", "n_recent": 1}, CFG)
    assert v["应用"] is True


def test_veto_复用开关关闭_noop():
    c = {**CFG, "风控汇聚复用": {"启用": False}}
    v = gate.veto_verdict(3, {"triggered": True}, c)
    assert v["应用"] is False and v["否决"] is False


def test_veto_总开关关闭_noop():
    v = gate.veto_verdict(3, {"triggered": True}, CFG_OFF)
    assert v["应用"] is False and v["否决"] is False


# ————————————————— 排序分层 sort_key —————————————————
def test_sortkey_clean多维优先():
    """clean 双族(indep=2)排在 clean 单族(indep=1)之前。"""
    a = {"独立口径命中数": 2, "原始命中数": 2, "过热闸": {"触发": False}, "veto": {"否决": False}}
    b = {"独立口径命中数": 1, "原始命中数": 3, "过热闸": {"触发": False}, "veto": {"否决": False}}
    assert gate.sort_key(a) < gate.sort_key(b)             # (0,-2,-2) < (0,-1,-3)


def test_sortkey_过热沉到clean之后():
    """过热软降级(tier1)排到 clean(tier0)之后,即便原始命中数更高。"""
    clean = {"独立口径命中数": 1, "原始命中数": 1, "过热闸": {"触发": False}, "veto": {"否决": False}}
    hot = {"独立口径命中数": 1, "原始命中数": 3,
           "过热闸": {"触发": True, "动作": "软降级"}, "veto": {"否决": False}}
    assert gate.sort_key(clean) < gate.sort_key(hot)
    assert gate.sort_key(hot)[0] == 1 and gate.sort_key(clean)[0] == 0


def test_sortkey_veto否决沉底tier3():
    veto = {"独立口径命中数": 3, "原始命中数": 3,
            "过热闸": {"触发": False}, "veto": {"否决": True}}
    assert gate.sort_key(veto)[0] == 3


# ————————————————— evaluate 编排(主验收) —————————————————
def _provider(table):
    """构造 feature_provider:table[code] -> (feats, high_flags, lhb)。"""
    def fp(code, as_of, c=None):
        return table.get(code, (dict(gate._EMPTY_FEATURES), 0, None))
    return fp


def test_evaluate_主验收_同源过热票不居首_双族clean居首():
    """002084(同源3策略 + 超买+涨幅+换手 过热 + 龙虎榜)被降级不居首;
    002811(量价放量+最大范围 双族 clean)居首;正常单族 clean 保留其位。"""
    picks = {
        "最大范围选股": ["002084", "002811", "600000"],
        "动量组合": ["002084"],
        "反转低换手组合": ["002084"],
        "量价放量": ["002811"],
    }
    feats_2084 = _feat(ob_os_verdict="超买", ob_os_resonance=3, bias20=30.0, ret_n=90.0,
                       ret_window=10, turnover=40.0,
                       lhb={"triggered": True, "reason": "净买上榜", "n_recent": 1},
                       fund_high_flags=2, 扣非为负=True)
    table = {
        "002084": (feats_2084, 2, {"triggered": True, "reason": "净买上榜", "n_recent": 1}),
        "002811": (dict(gate._EMPTY_FEATURES), 0, None),
        "600000": (dict(gate._EMPTY_FEATURES), 0, None),
    }
    res = gate.evaluate(picks, "2026-09-02", CFG, feature_provider=_provider(table))
    order = [r["code"] for r in res["票"]]
    # 双族 clean 002811 居首;同源过热 002084 不居首(被沉到 clean 之后)
    assert order[0] == "002811"
    assert order.index("002811") < order.index("002084")
    assert order.index("600000") < order.index("002084")   # 正常单族 clean 也在过热票之前
    # 002084 命中口径:原始3 → 独立1;过热触发 + veto 命中
    v2084 = next(r for r in res["票"] if r["code"] == "002084")
    assert v2084["原始命中数"] == 3 and v2084["独立口径命中数"] == 1
    assert v2084["过热闸"]["触发"] is True
    assert "002084" in res["过热命中"]


def test_evaluate_正常多维不劣化():
    """真·双族 clean 票(002811)与另一双族 clean 票按独立口径命中数排序,不被误降级。"""
    picks = {"量价放量": ["A", "B"], "最大范围选股": ["A"], "半导体多因子": ["B"]}
    table = {"A": (dict(gate._EMPTY_FEATURES), 0, None),
             "B": (dict(gate._EMPTY_FEATURES), 0, None)}
    res = gate.evaluate(picks, "2026-09-02", CFG, feature_provider=_provider(table))
    for r in res["票"]:
        assert r["过热闸"]["触发"] is False and r["tier"] == 0
    # A: 形态量能+价格动量=2;B: 形态量能+基本面=2;都 indep=2,保留(不劣化)
    assert {r["code"] for r in res["票"]} == {"A", "B"}
    assert all(r["独立口径命中数"] == 2 for r in res["票"])


def test_evaluate_killswitch_退回原命中数():
    """kill-switch 关 → 退回原命中数排序;不调用 feature_provider(no-op,不读盘)。"""
    picks = {"最大范围选股": ["002084", "002811"], "动量组合": ["002084"],
             "反转低换手组合": ["002084"]}

    def _boom(code, as_of, c=None):
        raise AssertionError("kill-switch 关时不应调用 feature_provider")

    res = gate.evaluate(picks, "2026-09-02", CFG_OFF, feature_provider=_boom)
    assert res["启用"] is False
    order = [r["code"] for r in res["票"]]
    # 原命中数口径:002084 命中3 → 居首(现状不回归)
    assert order[0] == "002084"
    v = next(r for r in res["票"] if r["code"] == "002084")
    assert v["原始命中数"] == 3 and v["独立口径命中数"] == 3   # 关闸不归族


def test_evaluate_空输入():
    res = gate.evaluate({}, "2026-09-02", CFG, feature_provider=_provider({}))
    assert res["票"] == [] and res["启用"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
