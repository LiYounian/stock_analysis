"""动量策略「高位超买抑制层」语义锁(策略4·动量组合 专属)。

锁死"为什么这么设计"的语义,防未来 prompt/代码重写误删规则:
  1. 双轴同时命中(超买共振 + 涨幅透支)→ 抑制触发;单轴不触发(默认最少命中轴数=2,防误杀)。
  2. 健康强票(涨幅透支不命中)→ 不误杀(不触发)。
  3. kill-switch(启用=False)→ 纯动量 no-op(overheat_verdict 不触发、apply_to_score 恒等)。
  4. 分轴开关:关某轴 → 该轴不发声。
  5. 缺数据保守:特征 None/空 → 不误抑制。
  6. sort_key 分层:未触发=tier0(与纯动量同层)、软降级=1、沉底=2、剔除=3(magnitude-robust)。
  7. 防未来函数:extract_features 剔除 date>as_of 的 K 线。
  8. screen 集成:超买透支票软降级挤出榜首 / 剔除移除;kill-switch 关 / 无命中不回归。

⚠️ 非投资建议:抑制层只改选股展示/入选排序。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.pipeline import screen_momentum as mom
from tools.store import repo as store
from tools.strategy import momentum_overheat as oh


# —— 测试固定 config(免受默认漂移)——
CFG_SOFT = {
    "启用": True, "模式": "软降级", "降级系数": 0.3, "最少命中轴数": 2, "评估候选数": 50,
    "轴": {"超买共振": {"启用": True, "共振门槛": 2},
          "涨幅透支": {"启用": True, "bias20门槛": 20.0, "涨幅窗口": 10, "涨幅门槛%": 30.0}},
}
CFG_SINK = {**CFG_SOFT, "模式": "沉底"}
CFG_DROP = {**CFG_SOFT, "模式": "剔除"}
CFG_OFF = {**CFG_SOFT, "启用": False}


def _feat(**kw):
    f = dict(oh._EMPTY_FEATURES)
    f.update(kw)
    return f


# ———————————————— 1. 双轴同时命中 / 单轴不触发 ————————————————
def test_both_axes_trigger():
    """超买共振(ob_os=超买/共振2)+ 涨幅透支(bias20 极端)→ 双轴命中,触发软降级。"""
    f = _feat(ob_os_verdict="超买", ob_os_resonance=2, bias20=27.5, ret_n=45.0, ret_window=10)
    v = oh.overheat_verdict(f, CFG_SOFT)
    assert v["触发"] is True and v["命中轴数"] == 2
    assert v["轴"]["超买共振"] and v["轴"]["涨幅透支"]
    assert v["动作"] == "软降级" and v["降级系数"] == pytest.approx(0.3)


def test_only_overbought_axis_not_triggered():
    """仅超买共振命中(涨幅未透支)→ 单轴 < 最少命中轴数2 → 不触发(防误杀健康强票)。"""
    f = _feat(ob_os_verdict="超买", ob_os_resonance=3, bias20=8.0, ret_n=12.0, ret_window=10)
    v = oh.overheat_verdict(f, CFG_SOFT)
    assert v["触发"] is False and v["命中轴数"] == 1 and v["轴"]["超买共振"]


def test_only_extension_axis_not_triggered():
    """仅涨幅透支命中(ob_os 非超买)→ 单轴 → 不触发。"""
    f = _feat(ob_os_verdict="中性", ob_os_resonance=1, bias20=30.0, ret_n=50.0, ret_window=10)
    v = oh.overheat_verdict(f, CFG_SOFT)
    assert v["触发"] is False and v["命中轴数"] == 1 and v["轴"]["涨幅透支"]


def test_resonance_threshold_gate():
    """共振门槛:ob_os=超买 但共振数 < 门槛 → 超买共振轴不发声。"""
    f = _feat(ob_os_verdict="超买", ob_os_resonance=1, bias20=30.0, ret_n=50.0, ret_window=10)
    v = oh.overheat_verdict(f, CFG_SOFT)
    assert v["轴"]["超买共振"] is False and v["触发"] is False


def test_extension_via_ret_only():
    """涨幅透支可由「近N日累计涨幅超阈」单独满足(bias 未极端)。"""
    f = _feat(ob_os_verdict="超买", ob_os_resonance=2, bias20=12.0, ret_n=40.0, ret_window=10)
    v = oh.overheat_verdict(f, CFG_SOFT)
    assert v["轴"]["涨幅透支"] is True and v["触发"] is True


# ———————————————— 2. 不误杀健康强票 ————————————————
def test_healthy_strong_not_killed():
    """健康强势(温和上行:bias/涨幅均未透支,即便 RSI 偏热)→ 不触发。"""
    f = _feat(ob_os_verdict="超买", ob_os_resonance=2, bias20=9.0, ret_n=10.0, ret_window=10)
    v = oh.overheat_verdict(f, CFG_SOFT)
    assert v["触发"] is False


# ———————————————— 3. kill-switch ————————————————
def test_kill_switch_off_no_op():
    """启用=False → 即便双轴特征命中也不触发(纯动量 no-op)。"""
    f = _feat(ob_os_verdict="超买", ob_os_resonance=3, bias20=50.0, ret_n=80.0, ret_window=10)
    v = oh.overheat_verdict(f, CFG_OFF)
    assert v["应用"] is False and v["触发"] is False


def test_sort_key_identity_when_not_triggered():
    """未触发 → sort_key = (0, base)(与纯动量同层,向后兼容)。"""
    v = oh.overheat_verdict(_feat(), CFG_SOFT)
    assert oh.sort_key(1.23, v) == (0, pytest.approx(1.23))
    assert oh.sort_key(-0.5, v) == (0, pytest.approx(-0.5))


# ———————————————— 4. 分轴开关 ————————————————
def test_axis_toggle_off_silences_axis():
    """关涨幅透支轴 → 只剩超买共振单轴 → 不触发。"""
    cfg = {**CFG_SOFT, "轴": {**CFG_SOFT["轴"], "涨幅透支": {"启用": False}}}
    f = _feat(ob_os_verdict="超买", ob_os_resonance=2, bias20=40.0, ret_n=60.0, ret_window=10)
    v = oh.overheat_verdict(f, cfg)
    assert v["轴"]["涨幅透支"] is False and v["触发"] is False


# ———————————————— 5. 缺数据保守 ————————————————
def test_none_features_conservative():
    """特征 None → 不误抑制。"""
    v = oh.overheat_verdict(None, CFG_SOFT)
    assert v["触发"] is False and v["命中轴数"] == 0


# ———————————————— 6. sort_key 分层(magnitude-robust)————————————————
def test_soft_downgrade_tier_below_clean_any_magnitude():
    """软降级=tier1,排到 clean(tier0)之后——即便被抑制票动量分**远高于** clean 票。"""
    f = _feat(ob_os_verdict="超买", ob_os_resonance=2, bias20=30.0, ret_n=50.0, ret_window=10)
    v = oh.overheat_verdict(f, CFG_SOFT)
    hot_key = oh.sort_key(2.0e5, v)                        # 急拉票动量分极大
    clean_key = oh.sort_key(0.5, oh.overheat_verdict(_feat(), CFG_SOFT))
    assert hot_key[0] == 1 and clean_key[0] == 0
    # 排序:tier 升序、分降序 → clean 仍排在 hot 之前(magnitude-robust)
    order = sorted([("HOT", hot_key), ("CLEAN", clean_key)], key=lambda x: (x[1][0], -x[1][1]))
    assert [c for c, _ in order] == ["CLEAN", "HOT"]


def test_same_tier_preserves_momentum_order():
    """同层(都软降级)保留相对动量序。"""
    f = _feat(ob_os_verdict="超买", ob_os_resonance=2, bias20=30.0, ret_n=50.0, ret_window=10)
    v = oh.overheat_verdict(f, CFG_SOFT)
    assert oh.sort_key(2.0, v) > oh.sort_key(1.0, v)      # (1,2.0) > (1,1.0)


def test_sink_and_drop_tiers():
    """沉底=tier2、剔除=tier3(均排在软降级 tier1 之后)。"""
    f = _feat(ob_os_verdict="超买", ob_os_resonance=2, bias20=30.0, ret_n=50.0, ret_window=10)
    v_sink = oh.overheat_verdict(f, CFG_SINK)
    assert v_sink["沉底"] is True and oh.sort_key(0.9, v_sink)[0] == 2
    v_drop = oh.overheat_verdict(f, CFG_DROP)
    assert v_drop["剔除"] is True and oh.sort_key(0.9, v_drop)[0] == 3


def test_pure_same_input_same_output():
    f = _feat(ob_os_verdict="超买", ob_os_resonance=2, bias20=30.0, ret_n=50.0, ret_window=10)
    assert oh.overheat_verdict(f, CFG_SOFT) == oh.overheat_verdict(f, CFG_SOFT)


# ———————————————— 7. 防未来函数:extract_features 剔除 date>as_of ————————————————
def _kdf(closes, start="2024-01-01", spread=0.02):
    n = len(closes)
    dates = pd.bdate_range(start, periods=n)
    c = np.asarray(closes, float)
    return pd.DataFrame({"date": dates, "open": c, "high": c * (1 + spread),
                         "low": c * (1 - spread), "close": c, "volume": [1e6] * n})


def test_extract_features_asof_slices_future():
    """extract_features 按 date<=as_of 切片:未来暴涨段不进特征(防未来函数)。"""
    calm = [100.0] * 30
    blowoff = [100.0 * 1.06 ** k for k in range(1, 16)]     # 之后才发生的暴涨
    df = _kdf(calm + blowoff)
    as_of = str(df["date"].iloc[29])[:10]                    # 切在暴涨之前
    f = oh.extract_features("X", as_of, kline=df, c=CFG_SOFT)
    assert f["bias20"] is not None and abs(f["bias20"]) < 5   # 平台期,无透支
    # 不切片(取到暴涨尾部)→ bias 显著更高,证明切片确实生效
    f_all = oh.extract_features("X", None, kline=df, c=CFG_SOFT)
    assert f_all["bias20"] > f["bias20"] + 10


# ———————————————— 8. screen 集成 ————————————————
def _hot():
    """高位超买 + 涨幅透支的动量票:平台后急拉,bias/涨幅/RSI 齐超阈。"""
    return _kdf([100.0] * 20 + [100.0 * 1.05 ** k for k in range(1, 21)])


def _healthy(r=0.01, n=40):
    """健康温和上行:动量'买'但 bias/涨幅未透支。"""
    return _kdf([round(100.0 * (1 + r) ** i, 4) for i in range(n)])


def test_screen_soft_downgrade_sinks_hot(monkeypatch, tmp_path):
    """默认软降级:超买透支票从榜首沉到健康强票之下,但仍在榜(命中数记录)。"""
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    monkeypatch.setattr(oh, "cfg", lambda: CFG_SOFT)
    kl = {"HOT": _hot(), "HEALTHY": _healthy()}
    monkeypatch.setattr(mom.market, "load_kline", lambda c: kl[c])
    v = mom.run_momentum_screen(["HOT", "HEALTHY"], as_of="2024-06-01",
                                fetch=False, top_k=10)
    picked = [x["code"] for x in v["入选清单"]]
    assert "HOT" in picked and "HEALTHY" in picked
    assert picked.index("HEALTHY") < picked.index("HOT")     # HOT 被沉到 HEALTHY 之下
    assert v["高位超买抑制层"]["启用"] is True
    assert "HOT" in v["高位超买抑制层"]["命中票"]


def test_screen_drop_removes_hot(monkeypatch, tmp_path):
    """剔除模式:超买透支票从入选清单移除。"""
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    monkeypatch.setattr(oh, "cfg", lambda: CFG_DROP)
    kl = {"HOT": _hot(), "HEALTHY": _healthy()}
    monkeypatch.setattr(mom.market, "load_kline", lambda c: kl[c])
    v = mom.run_momentum_screen(["HOT", "HEALTHY"], as_of="2024-06-01",
                                fetch=False, top_k=10)
    picked = [x["code"] for x in v["入选清单"]]
    assert "HOT" not in picked and "HEALTHY" in picked


def test_screen_kill_switch_no_regression(monkeypatch, tmp_path):
    """kill-switch 关:纯动量现状,HOT(动量更强)仍居首,无抑制层命中。"""
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    monkeypatch.setattr(oh, "cfg", lambda: CFG_OFF)
    kl = {"HOT": _hot(), "HEALTHY": _healthy()}
    monkeypatch.setattr(mom.market, "load_kline", lambda c: kl[c])
    v = mom.run_momentum_screen(["HOT", "HEALTHY"], as_of="2024-06-01",
                                fetch=False, top_k=10)
    picked = [x["code"] for x in v["入选清单"]]
    assert picked[0] == "HOT"                                 # 纯动量:急拉票动量最强
    assert v["高位超买抑制层"]["命中数"] == 0


def test_screen_apply_overheat_false_is_a_leg(monkeypatch, tmp_path):
    """apply_overheat=False 显式关(A/B 回测的 A 腿)→ 即便 config 开也纯动量。"""
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    monkeypatch.setattr(oh, "cfg", lambda: CFG_SOFT)
    kl = {"HOT": _hot(), "HEALTHY": _healthy()}
    monkeypatch.setattr(mom.market, "load_kline", lambda c: kl[c])
    v = mom.run_momentum_screen(["HOT", "HEALTHY"], as_of="2024-06-01",
                                fetch=False, top_k=10, apply_overheat=False)
    picked = [x["code"] for x in v["入选清单"]]
    assert picked[0] == "HOT" and v["高位超买抑制层"]["启用"] is False


def test_screen_healthy_only_no_suppression(monkeypatch, tmp_path):
    """只有健康强票(无透支)→ 抑制层空转,不误杀。"""
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    monkeypatch.setattr(oh, "cfg", lambda: CFG_SOFT)
    kl = {"H1": _healthy(r=0.012), "H2": _healthy(r=0.009)}
    monkeypatch.setattr(mom.market, "load_kline", lambda c: kl[c])
    v = mom.run_momentum_screen(["H1", "H2"], as_of="2024-06-01", fetch=False, top_k=10)
    assert v["高位超买抑制层"]["命中数"] == 0 and v["入选数"] == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
