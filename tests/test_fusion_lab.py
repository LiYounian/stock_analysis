"""融合基线实验室(tools/backtest/fusion_lab.py)语义锁测试。

锁住"为什么这么写"的语义,防未来重写无意破坏:
  ① 前向收益防未来函数(只用 idx 之后价、idx+h 越界返回 None);
  ② 逐日横截面 zscore 正确(逐日均0方差1、跨日独立、std=0 的日给 NaN);
  ③ 融合复合的符号约定(反用信号 sign=−1:原始值越大→融合分越低);
  ④ net 超额 ≤ gross 超额(扣成本后不高于扣前);
  ⑤ 复合分全成分 NaN → NaN(该票无有效发声,不进排序)。
非投资建议。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.backtest import fusion_lab as fl


# ───────── ① 前向收益防未来函数 ─────────
def test_fwd_ret_no_lookahead_and_bounds():
    close = np.array([10.0, 11.0, 12.0, 9.0], float)
    # idx=0, h=1 → 11/10-1 = +10%
    assert abs(fl._fwd_ret(close, 0, 1) - 10.0) < 1e-9
    # idx=1, h=2 → close[3]/close[1]-1 = 9/11-1
    assert abs(fl._fwd_ret(close, 1, 2) - (9.0 / 11.0 - 1) * 100) < 1e-9
    # 越界:idx+h >= len → None(不偷看不存在的未来)
    assert fl._fwd_ret(close, 3, 1) is None
    assert fl._fwd_ret(close, 2, 5) is None
    # 基准价<=0 → None
    assert fl._fwd_ret(np.array([0.0, 5.0]), 0, 1) is None


# ───────── ② 逐日横截面 zscore ─────────
def test_xs_zscore_per_day_and_degenerate():
    pdf = pd.DataFrame({
        "date": ["d1", "d1", "d1", "d2", "d2"],
        "x":    [1.0, 2.0, 3.0, 5.0, 5.0],   # d1 有方差;d2 全相同(std=0)
    })
    fl.add_xs_zscore(pdf, "x", "z_x")
    d1 = pdf.loc[pdf.date == "d1", "z_x"].to_numpy()
    # 逐日:均值≈0,总体标准差(ddof=0)≈1
    assert abs(d1.mean()) < 1e-9
    assert abs(d1.std(ddof=0) - 1.0) < 1e-9
    # 单调:x 越大 z 越大
    assert d1[0] < d1[1] < d1[2]
    # d2 std=0 → NaN(不参与排序,避免除0造 inf)
    d2 = pdf.loc[pdf.date == "d2", "z_x"].to_numpy()
    assert np.all(~np.isfinite(d2))


def test_xs_zscore_days_independent():
    # 两日尺度差 100 倍,zscore 后应各自标准化、互不影响
    pdf = pd.DataFrame({
        "date": ["d1", "d1", "d2", "d2"],
        "x":    [1.0, 2.0, 100.0, 200.0],
    })
    fl.add_xs_zscore(pdf, "x", "z_x")
    z = pdf["z_x"].to_numpy()
    # d1 与 d2 的 z 分布同形(各 ±1/√ ... 对称),d2 的大数值不会主导 d1
    assert abs(z[0] + z[1]) < 1e-9 and abs(z[2] + z[3]) < 1e-9
    assert abs(abs(z[0]) - abs(z[2])) < 1e-9


# ───────── ③ 融合复合符号约定(反用) ─────────
def test_compute_fusion_reverse_sign():
    # 单信号 trend(sign=−1 反用):原始 trend_score 越大 → 融合分越低
    pdf = pd.DataFrame({
        "date": ["d1", "d1", "d1"],
        "str_技术趋势": [-0.5, 0.0, 0.8],   # 递增
        # 其余列存在以防 KeyError(本测只用 trend)
    })
    fl.compute_fusion(pdf, ["trend"], weights=None, score_col="_f")
    f = pdf["_f"].to_numpy()
    # 反用:原始越大 → 分越小,单调递减
    assert f[0] > f[1] > f[2]


def test_compute_fusion_positive_sign():
    pdf = pd.DataFrame({
        "date": ["d1", "d1", "d1"],
        "str_超买超卖": [-0.5, 0.0, 0.8],
    })
    fl.compute_fusion(pdf, ["os"], weights=None, score_col="_f")
    f = pdf["_f"].to_numpy()
    # 正用:原始越大(越超卖为正)→ 分越大,单调递增
    assert f[0] < f[1] < f[2]


# ───────── ④ 复合全 NaN → NaN ─────────
def test_compute_fusion_all_nan_row_is_nan():
    pdf = pd.DataFrame({
        "date": ["d1", "d1", "d1"],
        "str_超买超卖": [1.0, 2.0, np.nan],
        "str_拐点":     [1.0, 2.0, np.nan],
    })
    fl.compute_fusion(pdf, ["os", "rev"], weights=None, score_col="_f")
    f = pdf["_f"].to_numpy()
    # 前两行有分,第三行两成分皆 NaN → NaN(不进排序)
    assert np.isfinite(f[0]) and np.isfinite(f[1])
    assert not np.isfinite(f[2])


# ───────── ⑤ net 超额 ≤ gross 超额 ─────────
def test_net_excess_not_above_gross():
    # 构造一个有正超额的排序分,net(扣成本)必 ≤ gross
    rng = np.random.default_rng(0)
    rows = []
    for di in range(40):                       # 40 个交易日,足够聚类
        for j in range(12):                    # 每日 12 票 ≥ Top10
            score = rng.normal()
            fwd = 0.5 * score + rng.normal(0, 1)   # 分与收益正相关
            rows.append({"date": f"d{di}", "_s": score, "fwd5": fwd})
    pdf = pd.DataFrame(rows)
    r = fl.eval_ranker(pdf, "_s", topns=(10,))
    t10 = r["topn"]["Top10"]
    g = t10["gross超额%"]
    n20 = t10["net_20bp超额%"]
    n10 = t10["net_10bp超额%"]
    assert g is not None and n20 is not None
    # 扣成本后严格下降,且 20bp 比 10bp 扣得更多
    assert n20 <= n10 <= g
    assert abs((g - n20) - 0.20) < 1e-6        # 恰好扣 0.2%
