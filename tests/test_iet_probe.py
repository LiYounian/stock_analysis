"""IET 探针回测核心 单测(合成数据锁语义,守则6)。

锁定:
  - forward_returns:T+1 入场 close→close,未到期(尾部 h 日)剔除(防未来)。
  - rank_ic_table:k 与远期收益正相关 → mean_ic>0;完美单调 → IC≈1。
  - layer_stats:四档单调递增/递减判定 + 多空方向随 IC 符号翻转。
  - gate_verdict:|IC|≥bar 且 |t|≥bar 且符号稳定 → 达标;否则停探针。
  - subsample_sign_stability:跨年份符号一致性。
"""
import numpy as np
import pandas as pd
import pytest

from tools.backtest.iet_probe import run_probe as R


def test_forward_returns_causal():
    # close: 100,110,121,133.1,... 每日+10%
    close = pd.Series([100.0, 110.0, 121.0, 133.1, 146.41],
                      index=["d0", "d1", "d2", "d3", "d4"])
    fr = R.forward_returns(close, horizons=(1,))
    # d0: entry=close[d1]=110, exit h=1 → close[d2]=121 → r=121/110-1=0.1
    r_d0 = fr[(fr["date"] == "d0") & (fr["h"] == 1)]["r"].iloc[0]
    assert r_d0 == pytest.approx(0.1)
    # 最后能出信号的日:entry_i=i+1, exit_i=i+2 < 5 → i≤2 → d0,d1,d2;d3/d4 未到期被剔除
    assert set(fr["date"]) == {"d0", "d1", "d2"}
    # h=1 全部 +10% (等比)
    assert np.allclose(fr["r"].to_numpy(), 0.1)


def _synth_frame(n_days=40, sign=1.0, h=1, noise=0.0, seed=0):
    """构造 k 与 r 单调相关的合成长表:每日 4 行业 k=0..3, r = sign*k*0.01 (+噪声)。"""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        for k in range(4):
            r = sign * k * 0.01 + (rng.normal(0, noise) if noise else 0.0)
            rows.append({"date": f"2024-{d:03d}", "industry": f"ind{k}", "k": k, "h": h, "r": r})
    return pd.DataFrame(rows)


def test_rank_ic_positive_and_perfect():
    fr = _synth_frame(sign=1.0, noise=0.0)
    tbl = R.rank_ic_table(fr, horizons=(1,))
    s = tbl[1]
    assert s["mean_ic"] == pytest.approx(1.0)     # 完美单调 → IC=1
    assert s["n_days"] == 40
    # 反相关 → IC=-1
    fr2 = _synth_frame(sign=-1.0, noise=0.0)
    assert R.rank_ic_table(fr2, horizons=(1,))[1]["mean_ic"] == pytest.approx(-1.0)


def test_layer_stats_monotone_and_direction():
    fr = _synth_frame(sign=1.0, noise=0.0)
    ls = R.layer_stats(fr, h=1)
    # 收益随 k 递增
    means = [ls["layer_mean"][k] for k in range(4)]
    assert means == sorted(means)
    assert ls["单调递增"] is True and ls["单调递减"] is False
    assert "动量延续" in ls["方向"]        # IC>0 → 多高k
    # 反向:IC<0 → 过热反转方向
    ls2 = R.layer_stats(_synth_frame(sign=-1.0, noise=0.0), h=1)
    assert ls2["单调递减"] is True
    assert "过热反转" in ls2["方向"]


def test_gate_verdict():
    # 强信号(强正相关但有噪 → IC 有方差、t 有定义且很大) + 符号稳定 → 达标
    # (完美无噪 IC≡1 会使日IC方差为0、t无定义,那是合成假象;真实数据必有噪声)
    fr = _synth_frame(sign=1.0, noise=0.004, seed=7)
    tbl = R.rank_ic_table(fr, horizons=(1,))
    assert tbl[1]["mean_ic"] > 0.5 and abs(tbl[1]["t_stat"]) >= R.T_BAR
    sub = R.subsample_sign_stability(fr, h=1, by="year")
    v = R.gate_verdict(tbl, sub)
    assert v["达标"] is True and v["各h达标"][1] is True
    # 无信号(k 与 r 无关) → 不达标
    rng = np.random.default_rng(1)
    rows = [{"date": f"2024-{d:03d}", "industry": f"ind{k}", "k": k, "h": 1,
             "r": float(rng.normal(0, 0.02))} for d in range(40) for k in range(4)]
    frnull = pd.DataFrame(rows)
    vnull = R.gate_verdict(R.rank_ic_table(frnull, (1,)),
                           R.subsample_sign_stability(frnull, 1))
    assert vnull["达标"] is False


def test_subsample_sign_stability():
    # 两年都正相关 → 符号一致
    a = _synth_frame(n_days=20, sign=1.0, seed=1)
    a["date"] = ["2023-" + x.split("-")[1] for x in a["date"]]
    b = _synth_frame(n_days=20, sign=1.0, seed=2)
    b["date"] = ["2024-" + x.split("-")[1] for x in b["date"]]
    fr = pd.concat([a, b], ignore_index=True)
    ss = R.subsample_sign_stability(fr, h=1, by="year")
    assert ss["符号一致"] is True
    assert set(k for k in ss if k != "符号一致") == {"2023", "2024"}


def test_build_eval_frame_aligns():
    temp = pd.DataFrame({
        "date": ["d0", "d1", "d0"], "industry": ["电子", "电子", "银行"], "k": [3, 2, 0],
    })
    ret = {
        "电子": pd.DataFrame({"date": ["d0", "d1"], "h": [1, 1], "r": [0.05, 0.03]}),
        "银行": pd.DataFrame({"date": ["d0"], "h": [1], "r": [-0.01]}),
    }
    fr = R.build_eval_frame(temp, ret, horizons=(1,))
    assert len(fr) == 3
    assert set(fr.columns) >= {"date", "industry", "k", "h", "r"}
    row = fr[(fr["industry"] == "电子") & (fr["date"] == "d0")].iloc[0]
    assert row["k"] == 3 and row["r"] == pytest.approx(0.05)
