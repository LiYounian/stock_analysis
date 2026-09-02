"""大盘预测 v1 资金流维单测——锁防未来函数 + 特征接入语义。

硬红线:两融为**盘后披露**,as_of=T 的资金流特征只能用 date ≤ T−1 的两融(严格早于 T)。
本测锁:
  · _attach_fundflow_lagged:面板日 T 取到的资金流,来自 T 的**前一个交易日**(≥1交易日滞后);
    破坏 T 当日及之后的两融值,不得改变 T 及更早日拼到的资金流(逐日字节级不变);
  · 资金流特征列并入 FEATURE_COLS,且 CompositeModel 把「资金流」列为一个可解释维;
  · include_fundflow=False → 资金流列全 0(A/B 的 A 组,该维在 composite 里被覆盖率归零)。
构造数据,不触网、不依赖真实缓存。
"""
import numpy as np
import pandas as pd

from tools.analysis.market_forecast import features as F
from tools.analysis.market_forecast import fundflow as FF
from tools.analysis.market_forecast import predictor as P


def _synth_margin(n=60, seed=1):
    """造市场级两融日序列(index=交易日),含单调余额 + 波动买入额。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    bal = 1e12 * (1.0 + 0.001 * np.arange(n) + 0.0005 * rng.standard_normal(n))
    buy = 8e10 * (1.0 + 0.05 * rng.standard_normal(n))
    return pd.DataFrame({"rz_bal": bal, "rz_buy": buy, "rzrq_bal": bal * 1.01}, index=idx)


def test_compute_features_no_future_in_series():
    """特征全为 ≤该两融日 的滚动量:改动某日之后的两融,不改变该日及更早的特征。"""
    m = _synth_margin()
    f_full = FF.compute_features(margin_df=m)
    cut = 40
    m2 = m.copy()
    m2.iloc[cut + 1:] = m2.iloc[cut + 1:] * 5.0        # 破坏 cut 之后的所有两融
    f2 = FF.compute_features(margin_df=m2)
    # cut 及更早的特征必须逐行不变(滚动量只看过去)
    a = f_full.iloc[:cut + 1]
    b = f2.iloc[:cut + 1]
    np.testing.assert_array_equal(a.to_numpy(), b.to_numpy())


def test_attach_lag_is_previous_trading_day():
    """面板日 T 拼到的资金流 = T 的前一个两融日(严格早于 T,≥1 交易日滞后)。"""
    m = _synth_margin(n=30)
    ff = FF.compute_features(margin_df=m)
    # 面板日历 = 两融日历(同交易日),T+1 语义下 T 取 T-1 的两融
    panel_idx = pd.DatetimeIndex(m.index)
    attached = F._attach_fundflow_lagged(panel_idx, ff, lag_days=1)
    # 对第 k(k>=1)个面板日,拼到的应等于两融序第 k-1 行的特征
    for k in range(5, 15):
        for c in FF.FUNDFLOW_COLS:
            got = attached.iloc[k][c]
            want = ff.iloc[k - 1][c]
            if np.isnan(want):
                assert np.isnan(got)
            else:
                assert got == want, f"面板日 {panel_idx[k].date()} 列 {c}:拼到非前一交易日"
    # 首个面板日无更早两融 → NaN(无泄漏)
    assert attached.iloc[0].isna().all()


def test_attach_lag_no_future_leak():
    """破坏面板日 T 当日及之后的两融,不得改变 T 及更早日拼到的资金流。"""
    m = _synth_margin(n=40)
    ff = FF.compute_features(margin_df=m)
    panel_idx = pd.DatetimeIndex(m.index)
    a = F._attach_fundflow_lagged(panel_idx, ff, lag_days=1)
    cut = 25
    ff2 = ff.copy()
    ff2.iloc[cut:] = 999.0                              # 破坏 cut 当日及之后
    b = F._attach_fundflow_lagged(panel_idx, ff2, lag_days=1)
    # 面板日 <= cut(其资金流来自 <= cut-1 的两融)必须不变
    np.testing.assert_array_equal(a.iloc[:cut + 1].to_numpy(),
                                  b.iloc[:cut + 1].to_numpy())


def test_fundflow_cols_in_feature_cols():
    assert set(FF.FUNDFLOW_COLS).issubset(set(F.FEATURE_COLS))
    assert F._FUNDFLOW_COLS == FF.FUNDFLOW_COLS


def test_composite_has_fundflow_dim():
    """CompositeModel 把资金流列为一个可解释维,且覆盖率降权可用。"""
    m = P.CompositeModel()
    assert "资金流" in m._groups
    assert m._groups["资金流"] == F._FUNDFLOW_COLS


def test_zero_fundflow_downweighted():
    """资金流全 0(A 组)→ 训练后该维有效权重≈0(覆盖率归零),不参与合成。"""
    rng = np.random.default_rng(0)
    cols = P.FEATURE_COLS
    n = 200
    X = pd.DataFrame(rng.standard_normal((n, len(cols))), columns=cols,
                     index=pd.bdate_range("2020-01-01", periods=n))
    for c in F._FUNDFLOW_COLS:                          # A 组:资金流列全 0
        X[c] = 0.0
    y = (rng.standard_normal(n) > 0).astype(float)
    m = P.CompositeModel().fit(X, y)
    assert m.ff_coverage == 0.0
    assert m.eff_group_w["资金流"] == 0.0
