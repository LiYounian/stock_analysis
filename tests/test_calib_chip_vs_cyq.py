"""S05 切本地筹码 · 阶段一校准脚本单测(hermetic,不触网、不读盘)。

锁住校准口径的语义(防未来重写踩坑),而非具体数值:
  1. 方案A量纲:本地 `获利比例`(0~1)在配对里映射为 winner_rate 百分数 = 获利比例×100
     (直接喂 0.x 进 95.0 阈值会恒 False,是方案 §2/§7 明确要防的坑)。
  2. 无未来函数:collect_pairs 用 df.iloc[:t+1] 切片喂 chip.summarize,追加"未来极端 bar"后
     同一 as_of 的本地因子不变(≤t 才参与推演)。
  3. cost95 映射:配对里 cost95_local 取自本地「成本区间上沿」。
  4. gate 判定:evaluate_gates 按 §3.4(高获利区 Spearman≥0.6 / 一致率≥0.80 / 前向≥旧×0.9)
     正确分档,达标/不达标显式区分。
  5. 前向收益统计:_fwd_stats 的胜率/均值按符号正确聚合。
"""
import numpy as np
import pandas as pd

from tools.collectors import chip, tushare_daily
from tools.backtest import calib_chip_vs_cyq as calib
from tools.store import repo as store


def _frame(n=280, seed=0):
    """稳步上升 + 末段大涨的合成主档 K 线(带 turnover,可推演筹码)。"""
    rng = np.random.default_rng(seed)
    closes = [10.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1.003 + rng.normal(0, 0.001)))
    closes = np.array(closes, dtype=float)
    closes[-8:] *= 1.05
    closes[-3:] *= 1.05
    dates = pd.bdate_range(end="2026-08-14", periods=n)
    return pd.DataFrame({
        "date": dates, "open": closes, "high": closes * 1.01, "low": closes * 0.99,
        "close": closes, "volume": np.full(n, 1e6), "amount": closes * 1e6,
        "turnover": np.full(n, 1.5), "pct_chg": np.zeros(n),
    })


# --------------------------------------------------------------------------- 量纲(方案A)
def test_wr_local_is_percent_not_fraction(monkeypatch):
    """配对里 wr_local = 本地获利比例×100(方案A);绝不把 0~1 直接当百分数。"""
    df = _frame()
    d = df["date"].iloc[-1].strftime("%Y-%m-%d")
    monkeypatch.setattr(store, "get_master_kline", lambda c: df.copy())
    # 真值 chip 给一个占位(本测只看本地量纲)
    monkeypatch.setattr(tushare_daily, "fetch_chip",
                        lambda day: pd.DataFrame({"code": ["000001"],
                                                  "winner_rate": [80.0], "cost_95pct": [9.0]}))
    pairs, _ = calib.collect_pairs(["000001"], [d], [95.0])
    assert len(pairs) == 1
    row = pairs.iloc[0]
    # 本地直接推演的获利比例(0~1)
    lo = max(0, len(df) - chip._WINDOW)
    rec = chip.summarize(df.iloc[lo:])
    assert rec["获利比例"] is not None and 0.0 <= rec["获利比例"] <= 1.0
    assert abs(row["wr_local"] - rec["获利比例"] * 100.0) < 1e-6      # ×100 已施加
    assert row["wr_local"] > 1.5                                     # 百分数量纲(非 0.x)
    assert abs(row["cost95_local"] - rec["成本区间上沿"]) < 1e-6      # cost95 映射自成本区间上沿


# --------------------------------------------------------------------------- 无未来函数
def test_point_in_time_no_future(monkeypatch):
    """在 as_of=t 处的本地因子,不受 t 之后追加的极端 bar 影响(只用 ≤t)。"""
    df = _frame()
    t = len(df) - 30                       # 取中间某根为 as_of
    d = df["date"].iloc[t].strftime("%Y-%m-%d")

    monkeypatch.setattr(tushare_daily, "fetch_chip",
                        lambda day: pd.DataFrame({"code": ["000001"],
                                                  "winner_rate": [80.0], "cost_95pct": [9.0]}))
    # 版本 A:原 df
    monkeypatch.setattr(store, "get_master_kline", lambda c: df.copy())
    pa, _ = calib.collect_pairs(["000001"], [d], [95.0])
    # 版本 B:把 t 之后的 bar 全部改成极端暴涨(若有前视会污染 as_of=t 的因子)
    df2 = df.copy()
    df2.loc[df2.index[t + 1:], ["open", "high", "low", "close"]] *= 5.0
    monkeypatch.setattr(store, "get_master_kline", lambda c: df2.copy())
    pb, _ = calib.collect_pairs(["000001"], [d], [95.0])
    assert abs(pa.iloc[0]["wr_local"] - pb.iloc[0]["wr_local"]) < 1e-9
    assert abs(pa.iloc[0]["cost95_local"] - pb.iloc[0]["cost95_local"]) < 1e-9


# --------------------------------------------------------------------------- gate 判定
def _factor(spearman):
    return {"high_profit(wr_true>=90)": {"spearman": spearman, "n": 100}}


def _signal(jaccard, ratio5):
    return {"by_threshold": {"95.0": {"jaccard_mean": jaccard,
                                      "new_vs_old_mean_ratio": {"T+5_mean_ratio": ratio5}}}}


def test_gate_pass_all_met():
    g = calib.evaluate_gates(_factor(0.72), _signal(0.85, 0.95), [95.0])
    assert g["gate1_因子相关(高获利区Spearman>=0.6)"]["pass"] is True
    assert g["gate2_3_by_threshold"]["95.0"]["pass"] is True
    assert g["overall_pass"] is True


def test_gate_fail_low_corr():
    g = calib.evaluate_gates(_factor(0.50), _signal(0.85, 0.95), [95.0])
    assert g["gate1_因子相关(高获利区Spearman>=0.6)"]["pass"] is False
    assert g["overall_pass"] is False


def test_gate_fail_low_forward():
    g = calib.evaluate_gates(_factor(0.72), _signal(0.85, 0.80), [95.0])
    assert g["gate2_3_by_threshold"]["95.0"]["gate3_前向>=0.9旧"] is False
    assert g["overall_pass"] is False


def test_gate_fail_low_agreement():
    g = calib.evaluate_gates(_factor(0.72), _signal(0.60, 0.95), [95.0])
    assert g["gate2_3_by_threshold"]["95.0"]["gate2_一致率>=0.80"] is False
    assert g["overall_pass"] is False


# --------------------------------------------------------------------------- 前向收益聚合
def test_fwd_stats_winrate_and_mean():
    codes_by_date = {"2026-08-01": {"A", "B", "C", "D"}}
    fwd_by_date = {"2026-08-01": {
        "A": {1: 0.02, 3: None, 5: 0.10, 10: None},
        "B": {1: -0.01, 3: None, 5: -0.05, 10: None},
        "C": {1: 0.03, 3: None, 5: 0.20, 10: None},
        "D": {1: 0.00, 3: None, 5: 0.00, 10: None},
    }}
    out = calib._fwd_stats(codes_by_date, fwd_by_date)
    # T+5:四个收益 [0.10,-0.05,0.20,0.00] → >0 的两个 → 胜率 0.5
    assert out["T+5"]["n"] == 4
    assert abs(out["T+5"]["winrate"] - 0.5) < 1e-9
    assert abs(out["T+5"]["mean"] - (0.10 - 0.05 + 0.20 + 0.0) / 4 * 100) < 1e-6
