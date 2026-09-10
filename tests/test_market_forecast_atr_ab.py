"""tech_atr 提维/维内加权 A/B 的语义锁单测(2026-09-10 B 方案)。

锁语义(防"提维实验被误接进生产"、防"实验退化成 no-op"):
  · 生产 CompositeModel 口径**不变**:恰好四维{技术,广度,消息面,资金流}、资金流组权重=0、
    tech_atr 落在**技术维内**(未被提为独立维);
  · A/B 变体确实改变了 tech_atr 在合成里的有效权重(实验有效、非 no-op),且未污染生产类;
  · 变体沿用防未来函数口径(只 subclass 生产模型、覆写分组/维内加权,fit 仍只用训练集)。

⚠️ A/B 结论:判据不达标、未接生产(见 docs/计划/2026-09-10_tech_atr提维加权_AB结论.md)。
本测试守住"未接生产"这条决策的语义,防未来 prompt/重写时无意把提维接进生产。
构造数据、不依赖真实行情。
"""
import numpy as np
import pandas as pd

from tools.analysis.market_forecast import predictor as P
from tools.analysis.market_forecast.features import _TECH_COLS
from tools.backtest import market_forecast_atr_ab as AB


def _synth_panel(n=220, seed=1):
    rng = np.random.default_rng(seed)
    cols = P.FEATURE_COLS
    X = rng.standard_normal((n, len(cols)))
    df = pd.DataFrame(X, columns=cols,
                      index=pd.date_range("2020-01-01", periods=n, freq="B"))
    df.index.name = "date"
    # 让 tech_atr 与 fwd 强相关(模拟"最强单特征"),其余弱
    df["fwd_ret"] = 0.02 * df["tech_atr"] + 0.02 * rng.standard_normal(n)
    return df


def test_production_composite_unchanged():
    """生产口径锁:四维、资金流权重=0、tech_atr 在技术维内(提维实验未接进生产)。"""
    m = P.CompositeModel()
    assert set(m._groups.keys()) == {"技术", "广度", "消息面", "资金流"}, \
        "生产 CompositeModel 维度集合被改动(不应出现'波动'/'涨停'等实验维)"
    assert m.group_w["资金流"] == 0.0, "资金流 kill-switch(权重0)被改动"
    assert "tech_atr" in m._groups["技术"], "tech_atr 被移出技术维(提维实验被误接进生产)"
    # 技术维仍是全部 tech_* 列等权(未做维内加权)
    assert m._groups["技术"] == list(_TECH_COLS)


def test_variants_change_atr_effective_weight():
    """A/B 变体确实改变 tech_atr 的有效权重(实验非 no-op),且不污染生产类。"""
    pan = _synth_panel()
    y = (pan["fwd_ret"] > 0).astype(float).to_numpy()

    base = P.CompositeModel().fit(pan, y)
    # B1/B3:tech_atr 被移出技术维、单独成"波动"维
    b1 = AB.B1_ATRDim().fit(pan, y)
    assert "波动" in b1._groups and b1._groups["波动"] == ["tech_atr"]
    assert "tech_atr" not in b1._groups["技术"]
    b3 = AB.B3_ATR_BR_Dim().fit(pan, y)
    assert "波动" in b3._groups and "涨停" in b3._groups
    # B2:技术维内按 |IC| 加权 → tech_atr 权重远高于等权 1/10(它与标签强相关)
    b2 = AB.B2_ICWeighted().fit(pan, y)
    atr_w = b2._tech_w["tech_atr"]
    assert atr_w > 1.0 / len(_TECH_COLS), \
        f"B2 维内加权应给强相关的 tech_atr 高于等权的权重,实际 {atr_w:.3f}"

    # 变体产出的 raw 分数与 baseline 不同(实验确实改了合成,不是 no-op)
    xs = base.scaler.transform(pan[base.cols].to_numpy(dtype=float))
    raw_base = base._composite_raw(xs)
    xs2 = b2.scaler.transform(pan[b2.cols].to_numpy(dtype=float))
    raw_b2 = b2._composite_raw(xs2)
    assert not np.allclose(raw_base, raw_b2), "B2 与 baseline 合成分完全相同 → 实验无效(no-op)"

    # 生产类未被变体污染(隔离性):再 new 一个生产模型仍是四维
    assert set(P.CompositeModel()._groups.keys()) == {"技术", "广度", "消息面", "资金流"}


def test_variants_no_lookahead_scaler_is_train_only():
    """变体仍复用生产的'标准化/定向只用训练集'口径(防未来函数不被实验破坏)。"""
    pan = _synth_panel()
    y = (pan["fwd_ret"] > 0).astype(float).to_numpy()
    for cls in (AB.B1_ATRDim, AB.B3_ATR_BR_Dim, AB.B2_ICWeighted):
        m = cls().fit(pan, y)
        # 定向向量长度 = 特征数(在训练集拟合),概率落 [0,1]
        assert m.orient is not None and len(m.orient) == len(P.FEATURE_COLS)
        p = m.predict_proba(pan)
        assert np.all((p >= 0.0) & (p <= 1.0))
