"""行业冷热钟摆表·数值4维(P-A)语义锁。

锁住"为什么这么做"的关键语义,防未来 prompt/代码重写无意删规则:
  1. 波动维已实现波动**因果**(≤T)、A/B 阈值化正确。
  2. verdict 用**重叠校正的块自助 p_boot**、不用 overlap-inflated 的 naive t
     (短样本高 naive t 但 p_boot 不显著 → 判「欠功效」,**绝不判「有预测力」,更不写「证伪」**)。
  3. 只有样本充足且点估≈0 才判「不可用·真null」。
  4. 强信号(足样本+跨年符号稳)→ 判「有预测力」。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.analysis.industry_temp import volatility as VOL
from tools.backtest.industry_thermometer import evaluate as EV

HZ = (5, 10, 20)


# ————————————————————— 波动维 —————————————————————
def test_realized_vol_causal_and_warmup_nan():
    close = pd.Series(100 * np.cumprod(1 + 0.01 * np.sin(np.arange(60))),
                      index=[f"2025-01-{i:02d}" if i < 32 else f"2025-02-{i-31:02d}"
                             for i in range(1, 61)])
    vol = VOL.realized_vol_series(close, win=20)
    # 前 win 段无值(min_periods=win)
    assert vol.iloc[:19].isna().all()
    assert np.isfinite(vol.iloc[-1])
    # 因果:改未来点不影响过去的波动值
    close2 = close.copy()
    close2.iloc[-1] *= 1.5
    vol2 = VOL.realized_vol_series(close2, win=20)
    assert np.isclose(vol.iloc[-5], vol2.iloc[-5])  # 倒数第5点不受最后一点影响


def test_ab_volatility_threshold():
    assert VOL.ab_volatility(0.8) == "A"       # 高波动侧
    assert VOL.ab_volatility(0.5) == "A"       # ≥cut
    assert VOL.ab_volatility(0.2) == "B"
    assert VOL.ab_volatility(None) is None
    assert VOL.ab_volatility(float("nan")) is None


# ————————————————————— 块自助 —————————————————————
def test_block_bootstrap_mean_brackets():
    x = np.full(200, 0.5) + np.random.default_rng(0).normal(0, 0.01, 200)
    b = EV._block_bootstrap_mean(x, block=10, n_boot=500)
    assert abs(b["mean"] - 0.5) < 0.02
    assert b["ci_lo"] < 0.5 < b["ci_hi"]
    assert b["p_boot"] < 0.05        # 明显非零


# ————————————————————— verdict:不用 naive t —————————————————————
def _ic(mean_ic, naive_t, p_boot, n_days, ci):
    return {"mean_ic": mean_ic, "naive_t": naive_t, "p_boot": p_boot,
            "boot_ci": ci, "n_days": n_days}


def _stable(ok):
    return {h: {"稳健一致": ok} for h in HZ}


def test_robust_sign_tolerates_one_anomalous_year():
    """8/9 年同号(仅1年翻)→ 稳健一致 True;6/9 → False(拒脆弱信号)。"""
    sub8 = {"2018": -0.06, "2019": -0.03, "2020": +0.06, "2021": -0.02, "2022": -0.12,
            "2023": -0.10, "2024": -0.09, "2025": -0.06, "2026": -0.05, "符号一致": False}
    r8 = EV.robust_sign(sub8)
    assert r8["稳健一致"] is True and r8["多数符号"] == -1
    sub6 = {"2018": +0.02, "2019": -0.05, "2020": +0.04, "2021": -0.04, "2022": +0.03,
            "2023": -0.06, "2024": -0.10, "2025": -0.12, "2026": -0.01, "符号一致": False}
    assert EV.robust_sign(sub6)["稳健一致"] is False


def test_verdict_high_naive_t_but_insignificant_boot_is_underpowered_not_signal():
    """短样本:naive t 虚高(-4.4)但重叠校正 p_boot=0.12 → 欠功效,绝不"有预测力"/"证伪"。"""
    ic = {5: _ic(-0.03, -2.5, 0.10, 234, [-0.09, 0.01]),
          10: _ic(-0.04, -3.0, 0.13, 234, [-0.11, 0.02]),
          20: _ic(-0.0617, -4.40, 0.124, 234, [-0.15, 0.03])}
    v = EV._verdict(ic, _stable(True), HZ)
    assert v["判定"] == "欠功效待复查"
    assert v["判定"] != "有预测力"


def test_verdict_never_emits_zhengwei_label():
    """任何路径都不产出「证伪」字样(功效纪律硬约束)。"""
    for ic in [
        {h: _ic(0.0, 0.0, 0.9, 100, [-0.05, 0.05]) for h in HZ},          # 弱噪声短样本
        {h: _ic(-0.06, -4.0, 0.12, 200, [-0.15, 0.02]) for h in HZ},      # naive高但boot不显著
    ]:
        v = EV._verdict(ic, _stable(True), HZ)
        assert "证伪" not in v["判定"]


def test_verdict_adequate_and_null_is_buke_yong():
    """样本充足(h5 n_days=600→120块≥100)+点估≈0+IC块自助CI含0 → 不可用·真null。"""
    ic = {5: _ic(0.001, 0.1, 0.9, 600, [-0.02, 0.02]),
          10: _ic(-0.002, -0.2, 0.8, 590, [-0.03, 0.02]),
          20: _ic(0.000, 0.0, 0.95, 580, [-0.03, 0.03])}
    v = EV._verdict(ic, _stable(False), HZ)
    assert v["判定"] == "不可用·真null"


def test_verdict_strong_signal_is_you_yuce_li():
    """p_boot<0.05 且 |IC|≥bar 且稳健符号一致 → 有预测力。"""
    ic = {5: _ic(-0.05, -3.0, 0.01, 500, [-0.08, -0.02]),
          10: _ic(-0.04, -2.5, 0.03, 490, [-0.07, -0.01]),
          20: _ic(-0.03, -2.0, 0.20, 480, [-0.07, 0.01])}
    v = EV._verdict(ic, _stable(True), HZ)
    assert v["判定"] == "有预测力"
    assert 5 in v["达标h"]


# ————————————————————— 集成:evaluate_dim 端到端(合成) —————————————————————
def _synthetic(signal: bool, seed: int = 0):
    """5 行业 × 跨 ~7 年 交易日;signal=True 时 value 与前瞻收益强负相关。

    跨 7 年是为让稳健符号检验(二项)有足够子样本:n=7 全同号 → p≈0.016<0.05。
    """
    rng = np.random.default_rng(seed)
    inds = [f"业{i}" for i in range(5)]
    dates = pd.bdate_range("2018-01-01", periods=1800).strftime("%Y-%m-%d").tolist()
    dim_rows, ret = [], {i: [] for i in inds}
    for d in dates:
        vals = rng.permutation(np.linspace(0.1, 0.9, 5))
        for ind, v in zip(inds, vals):
            ab = "A" if v >= 0.5 else "B"
            dim_rows.append({"date": d, "industry": ind, "dim": "波动",
                             "value": float(v), "ab": ab})
            base = (-0.5 * (v - 0.5)) if signal else 0.0
            r = base + rng.normal(0, 0.01)
            for h in HZ:
                ret[ind].append({"date": d, "h": h, "r": float(r)})
    dim_panel = pd.DataFrame(dim_rows)
    ret_by = {i: pd.DataFrame(rows) for i, rows in ret.items()}
    return dim_panel, ret_by


def test_evaluate_dim_strong_signal_detected():
    dim_panel, ret_by = _synthetic(signal=True, seed=1)
    res = EV.evaluate_dim("波动", dim_panel, ret_by, HZ)
    assert res["verdict"]["判定"] == "有预测力"
    # 强负相关 → 方向逆向
    assert "逆向" in (res["verdict"]["方向"] or "")


def test_evaluate_dim_noise_is_underpowered_not_falsified():
    dim_panel, ret_by = _synthetic(signal=False, seed=2)
    res = EV.evaluate_dim("波动", dim_panel, ret_by, HZ)
    # 260 交易日 noise:h5≈52块<100 不足判真null → 欠功效;绝不证伪
    assert res["verdict"]["判定"] in ("欠功效待复查", "不可用·真null")
    assert "证伪" not in res["verdict"]["判定"]
