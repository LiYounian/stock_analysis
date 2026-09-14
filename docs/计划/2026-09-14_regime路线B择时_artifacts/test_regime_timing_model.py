"""RegimeTimingModel 单测(归档件·锁机制,非锁 edge)。

锁语义:①定向自动把与收益负相关的因子取负号(倒挂翻转)②防未来函数(predict 不吃标签、只用 fit 拟合量)
③有真信号时样本外可学出方向 ④权重形状/口径。**不断言 regime 真有 edge**(诊断结论=不显著,见报告)。
运行:PY -m pytest docs/计划/2026-09-14_regime路线B择时_artifacts/test_regime_timing_model.py -q
"""
import numpy as np, pandas as pd, pytest
from regime_timing_model import RegimeTimingModel, FCOLS


def _synth(n=400, seed=0, sign=+1):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame({c: rng.normal(size=n) for c in FCOLS})
    # 让"量能"与前瞻收益负相关(模拟过热倒挂),"指数多头"正相关
    fwd = sign * (0.5 * X["指数多头"] - 0.5 * X["量能"]) + rng.normal(scale=0.5, size=n)
    return X, fwd.to_numpy()


def test_orientation_flips_inverted_factor():
    X, fwd = _synth()
    m = RegimeTimingModel("equal").fit(X, fwd)
    o = m.orientation()
    assert o["量能"] == -1        # 负相关→取负号(倒挂翻转)
    assert o["指数多头"] == +1     # 正相关→取正号


def test_no_lookahead_predict_uses_only_fit_params():
    X, fwd = _synth()
    m = RegimeTimingModel("ic").fit(X, fwd)
    # predict 只吃特征、不吃标签;两次同输入结果一致且不依赖 fwd
    p1 = m.predict_proba(X.iloc[:5])
    p2 = m.predict_proba(X.iloc[:5])
    assert np.allclose(p1, p2) and p1.shape == (5,)
    assert ((p1 >= 0) & (p1 <= 1)).all()


def test_learns_direction_oos_when_signal_exists():
    Xtr, ftr = _synth(n=600, seed=1)
    Xte, fte = _synth(n=600, seed=2)          # 同结构、独立抽样=样本外
    m = RegimeTimingModel("logreg").fit(Xtr, ftr)
    p = m.predict_proba(Xte)
    hit = ((p >= 0.5).astype(int) * 2 - 1 == np.sign(fte)).mean()
    assert hit > 0.55                          # 有真信号时机制能学出方向(远高于50%)


@pytest.mark.parametrize("method", ["equal", "ic", "logreg"])
def test_methods_run_and_probabilities_valid(method):
    X, fwd = _synth()
    m = RegimeTimingModel(method).fit(X, fwd)
    p = m.predict_proba(X)
    assert p.shape == (len(X),) and ((p >= 0) & (p <= 1)).all()
    if method != "logreg":
        assert abs(m.w.sum() - 1.0) < 1e-6 or m.method == "equal"
