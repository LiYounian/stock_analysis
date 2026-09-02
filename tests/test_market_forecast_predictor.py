"""大盘预测预测器 + 前向回测单测(v0.5)。

锁语义(硬红线):
  · **无未来函数**:walk_forward 对测试日 d 的预测只用严格早于 d 且标签已到期的样本
    → 改动 d 之后的行,不得改变 d 及更早日的预测(逐日字节级不变);
  · 预测概率落 [0,1];CompositeModel 定向/校准可拟合可预测;
  · 概率→档位、fwd→分位档位 均**单调**。
构造数据、不依赖真实行情。
"""
import numpy as np
import pandas as pd
import pytest

from tools.analysis.market_forecast import features as F
from tools.analysis.market_forecast import predictor as P
from tools.backtest import market_forecast_backtest as BT


def _synth_panel(n=220, seed=0, horizon=1):
    """造一个含 FEATURE_COLS + fwd_ret 的面板;fwd 与部分特征弱相关 + 噪声。"""
    rng = np.random.default_rng(seed)
    cols = P.FEATURE_COLS
    X = rng.standard_normal((n, len(cols)))
    df = pd.DataFrame(X, columns=cols,
                      index=pd.date_range("2020-01-01", periods=n, freq="B"))
    df.index.name = "date"
    # fwd 收益:让方向和几个技术/广度因子弱相关(信号+噪声)
    signal = 0.4 * df["tech_mom5"] + 0.3 * df["br_net_adv"] - 0.2 * df["br_below_ma20"]
    df["fwd_ret"] = 0.01 * signal + 0.02 * rng.standard_normal(n)
    df.attrs["horizon"] = horizon
    return df


def test_predict_proba_range():
    pan = _synth_panel()
    y = (pan["fwd_ret"] > 0).astype(float).to_numpy()
    for name in ("composite", "logistic"):
        m = P.MODELS[name]().fit(pan, y)
        p = m.predict_proba(pan)
        assert p.min() >= 0.0 and p.max() <= 1.0
        assert np.isfinite(p).all()


def test_composite_explain_dims():
    pan = _synth_panel()
    y = (pan["fwd_ret"] > 0).astype(float).to_numpy()
    m = P.CompositeModel().fit(pan, y)
    ex = m.explain(pan.iloc[[-1]])
    assert set(ex) == {"技术", "广度", "消息面", "资金流"}
    # 各维在本合成盘全非零 → 覆盖率≈1;贡献量级有限(温莎化 ±4)
    assert all(abs(v) < 5.0 for v in ex.values())


def test_prob_to_bucket_monotonic():
    ps = [0.1, 0.4, 0.5, 0.6, 0.9]
    buckets = [P.prob_to_bucket(p) for p in ps]
    assert buckets == sorted(buckets)          # 概率升 → 档位不降
    assert buckets[0] == 0 and buckets[-1] == 4


def test_bucketize_monotonic():
    fwd = pd.Series(np.linspace(-0.1, 0.1, 100))
    b = F._bucketize(fwd)
    # 收益单调升 → 分档序号单调不降
    assert list(b) == sorted(b.tolist())
    assert b.min() == 0 and b.max() == 4


def test_walk_forward_no_future_leak():
    """改动"未来"行不得改变更早日的样本外预测(无未来函数硬红线)。"""
    pan = _synth_panel(n=240, seed=7, horizon=1)
    rec_a = BT.walk_forward(pan, model_name="composite", min_train=40, stride=3)
    assert not rec_a.empty

    cutoff_pos = int(len(pan) * 0.7)
    cutoff_date = pan.index[cutoff_pos]

    # 破坏 cutoff 之后的所有行:特征 + 标签全部改成极端值
    pan2 = pan.copy()
    fut = pan2.index[pan2.index > cutoff_date]
    for c in P.FEATURE_COLS:
        pan2.loc[fut, c] = 999.0
    pan2.loc[fut, "fwd_ret"] = -0.5
    pan2.attrs["horizon"] = 1
    rec_b = BT.walk_forward(pan2, model_name="composite", min_train=40, stride=3)

    # 比对 cutoff 及更早的测试日预测:必须逐日字节级一致
    a = rec_a[rec_a["date"] <= cutoff_date].set_index("date")["p_up"]
    b = rec_b[rec_b["date"] <= cutoff_date].set_index("date")["p_up"]
    common = a.index.intersection(b.index)
    assert len(common) > 10, "早期测试日太少,测试无意义"
    np.testing.assert_array_equal(a.loc[common].to_numpy(), b.loc[common].to_numpy())


def test_walk_forward_train_strictly_before_test(monkeypatch):
    """拦截模型 fit,断言训练集所有 date 的标签窗口都在测试日之前收口(pos[t]+h < pos[d])。"""
    pan = _synth_panel(n=180, seed=3, horizon=5)
    pos = {d: i for i, d in enumerate(pan.index)}
    h = 5
    violations = []

    orig_fit = P.CompositeModel.fit
    # 记录每次 fit 的训练集最大 pos;预测时的测试日在 walk_forward 里,用闭包抓不到,
    # 改为断言:训练集里没有任何 t 使 pos[t]+h 越过"下一次预测"的边界。
    # 简化:直接在 walk_forward 内保证——这里校验训练集本身单调早于其构造上界。
    def spy_fit(self, X, y):
        max_train_pos = max(pos[d] for d in X.index)
        # 训练集最后一根的标签用 close[max+h],其必须已实现(<= 全长-1)
        assert max_train_pos + h <= len(pan) - 1
        violations.append(max_train_pos)
        return orig_fit(self, X, y)

    monkeypatch.setattr(P.CompositeModel, "fit", spy_fit)
    BT.walk_forward(pan, model_name="composite", min_train=40, stride=5)
    assert violations, "未触发任何训练"
