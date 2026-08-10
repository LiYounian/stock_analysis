"""策略层动量算子 + 组合选股单测(移植自聚宽脚本)。

覆盖:
  · 4 个算子(加权对数动量/拉普拉斯/BBI/N日动量)注册与签名
  · 边界:数据不足降级不抛错
  · 正常路径:上升/下跌序列分别产出对应信号或分数方向
  · 2 个组合选股用 fake records + monkeypatch _load_closes_from_record 跑通
    (不触实盘缓存,单测可离线跑)
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.strategy import registry as reg
from tools.strategy import momentum as mm


# ————————————————————————————————————————————————
# 注册:导入 tools.strategy 即触发,四个算子 + 两个选股全部在册
# ————————————————————————————————————————————————
def test_all_registered():
    for name, kind in [
        ("加权对数动量", "评分"),
        ("拉普拉斯低通趋势", "信号"),
        ("BBI站上", "信号"),
        ("N日绝对动量", "评分"),
        ("策略A_动量组合", "选股"),
        ("策略B_红利动量组合", "选股"),
    ]:
        meta = reg.get(name)
        assert meta.kind == kind
        assert callable(meta.fn)


# ————————————————————————————————————————————————
# 加权对数动量:上升序列打正分,下跌打负分,数据不足降级
# ————————————————————————————————————————————————
def test_weighted_log_momentum_uptrend_positive():
    closes = [10.0 * (1.01 ** i) for i in range(30)]     # 每日 +1%
    out = reg.run("加权对数动量", closes, lookback_days=25)
    assert out["score"] > 0
    assert out["r_squared"] > 0.99                        # 纯线性 log,R² 接近 1
    assert out["annualized"] > 1.0                        # 每日 1% 复利,年化 >>100%


def test_weighted_log_momentum_downtrend_negative():
    closes = [10.0 * (0.99 ** i) for i in range(30)]
    out = reg.run("加权对数动量", closes, lookback_days=25)
    assert out["score"] < 0


def test_weighted_log_momentum_insufficient_data():
    out = reg.run("加权对数动量", [1.0] * 5, lookback_days=25)
    assert out["score"] == 0.0
    assert isinstance(out["依据"], list) and out["依据"]


def test_weighted_log_momentum_flat_zero_variance():
    """全平价格 → 斜率≈0,分数应≈0(不抛 divide-by-zero)。"""
    out = reg.run("加权对数动量", [10.0] * 30, lookback_days=25)
    # log(10) 全相同 → var_x 正常但 var_y=0 → r²=0 → score=0
    assert abs(out["score"]) < 1e-9


# ————————————————————————————————————————————————
# 拉普拉斯低通趋势:上升趋势末尾应出"买",下跌末尾应出"卖",前 2 根"持"
# ————————————————————————————————————————————————
def test_laplace_uptrend_ends_with_buy():
    closes = [10.0 + 0.5 * i for i in range(30)]         # 稳定上升
    sig = reg.run("拉普拉斯低通趋势", closes, s=0.07, min_slope=0.002)
    assert len(sig) == len(closes)
    assert sig[0] == "持" and sig[1] == "持"
    assert sig[-1] == "买"


def test_laplace_downtrend_sells():
    closes = [30.0 - 0.5 * i for i in range(30)]
    sig = reg.run("拉普拉斯低通趋势", closes, s=0.07, min_slope=0.002)
    assert sig[-1] == "卖"


def test_laplace_insufficient_data():
    sig = reg.run("拉普拉斯低通趋势", [10.0, 10.5])
    assert sig == ["持", "持"]


def test_laplace_accepts_dataframe():
    df = pd.DataFrame({"close": [10.0 + 0.5 * i for i in range(30)]})
    sig = reg.run("拉普拉斯低通趋势", df)
    assert len(sig) == 30 and sig[-1] == "买"


# ————————————————————————————————————————————————
# BBI 站上:第 24 根起才有 BBI,前 23 根一律"持"
# ————————————————————————————————————————————————
def test_bbi_below_24_bars_all_hold():
    sig = reg.run("BBI站上", [10.0] * 20)
    assert sig == ["持"] * 20


def test_bbi_uptrend_stands_above():
    closes = [10.0 + 0.2 * i for i in range(40)]
    sig = reg.run("BBI站上", closes)
    assert len(sig) == 40
    assert all(s == "持" for s in sig[:23])
    assert sig[-1] == "买"                                # 上升趋势尾部价高于 BBI


def test_bbi_downtrend_breaks():
    closes = [20.0] * 24 + [15.0, 14.0, 13.0, 12.0]      # 前平后跌破
    sig = reg.run("BBI站上", closes)
    assert sig[-1] == "卖"


# ————————————————————————————————————————————————
# N 日绝对动量:精确等式验证
# ————————————————————————————————————————————————
def test_n_day_momentum_exact():
    closes = [10.0] * 30 + [12.0]                        # 第 31 根从 10→12
    out = reg.run("N日绝对动量", closes, n=30)
    assert out["score"] == pytest.approx(0.2)


def test_n_day_momentum_insufficient():
    out = reg.run("N日绝对动量", [10.0] * 5, n=30)
    assert out["score"] == 0.0


# ————————————————————————————————————————————————
# 组合选股 A:动量 + R² + 拉普拉斯闸门
# 用 monkeypatch 替换 _load_closes_from_record,不碰真实 store
# ————————————————————————————————————————————————
def _fake_uptrend(n: int, rate: float) -> np.ndarray:
    return np.array([10.0 * (rate ** i) for i in range(n)], dtype=float)


def test_combo_A_selects_top_by_score(monkeypatch):
    series = {
        "A": _fake_uptrend(40, 1.02),          # 强上升 → 分数最高
        "B": _fake_uptrend(40, 1.005),         # 温和上升
        "C": _fake_uptrend(40, 0.99),          # 下跌 → 拉普拉斯拒
        "D": None,                              # 缺 K 线 → 跳过
    }
    monkeypatch.setattr(mm, "_load_closes_from_record",
                        lambda code: series.get(code))
    records = {c: {"meta": {"code": c}} for c in series}
    picked = reg.run("策略A_动量组合", records, top_k=2)
    assert picked == ["A", "B"]


def test_combo_A_r2_filter_kicks_low_r2(monkeypatch):
    """噪声大的序列 R² 低 → 被 r2_min 过滤。"""
    rng = np.random.default_rng(42)
    noise = rng.normal(0, 5, 40).cumsum() + 100          # 随机游走
    monkeypatch.setattr(mm, "_load_closes_from_record",
                        lambda code: noise if code == "N" else _fake_uptrend(40, 1.01))
    records = {"N": {}, "U": {}}
    picked = reg.run("策略A_动量组合", records, top_k=2, r2_min=0.9)
    assert "N" not in picked                              # 噪声票被 R² 门槛拒
    assert "U" in picked


def test_combo_A_empty_input():
    assert reg.run("策略A_动量组合", {}) == []


# ————————————————————————————————————————————————
# 组合选股 B:质地过滤 + BBI 闸门 + N 日动量排序
# ————————————————————————————————————————————————
def _rec(roe=None, rev=None, prof=None):
    return {
        "meta": {"code": "X"},
        "fundamental": {"ROE": roe, "营收增速": rev, "净利增速": prof},
    }


def test_combo_B_quality_filter_blocks_missing_fundamental(monkeypatch):
    """缺 fundamental → 不通过质地(不假设优质)。"""
    monkeypatch.setattr(mm, "_load_closes_from_record",
                        lambda code: _fake_uptrend(40, 1.02))
    records = {
        "OK": _rec(roe=5.0, rev=20.0, prof=30.0),
        "MISS": _rec(),                                   # ROE/增速全 None
        "LOW": _rec(roe=0.5, rev=20.0, prof=30.0),        # ROE 不够
    }
    picked = reg.run("策略B_红利动量组合", records, top_k=5)
    assert picked == ["OK"]


def test_combo_B_bbi_gate(monkeypatch):
    """BBI 下方的票不入选,即使质地过关。"""
    series = {
        "UP": _fake_uptrend(30, 1.02),                    # 站上 BBI
        "DN": _fake_uptrend(30, 0.98),                    # 跌破 BBI
    }
    monkeypatch.setattr(mm, "_load_closes_from_record",
                        lambda code: series[code])
    records = {c: _rec(roe=5.0, rev=20.0, prof=30.0) for c in series}
    picked = reg.run("策略B_红利动量组合", records, top_k=5)
    assert picked == ["UP"]


def test_combo_B_ranked_by_momentum(monkeypatch):
    """两只都过闸,按 N 日动量排序 → 高动量在前。"""
    series = {
        "HI": _fake_uptrend(30, 1.03),
        "LO": _fake_uptrend(30, 1.005),
    }
    monkeypatch.setattr(mm, "_load_closes_from_record",
                        lambda code: series[code])
    records = {c: _rec(roe=5.0, rev=20.0, prof=30.0) for c in series}
    picked = reg.run("策略B_红利动量组合", records, top_k=5)
    assert picked == ["HI", "LO"]


def test_combo_B_empty_input():
    assert reg.run("策略B_红利动量组合", {}) == []


# ————————————————————————————————————————————————
# BBI 数学正确性(与手算对齐)
# ————————————————————————————————————————————————
def test_bbi_value_matches_manual_average():
    closes = np.arange(1, 25, dtype=float)               # 1..24
    b = mm._bbi(closes)
    ma3 = np.mean(closes[-3:])                            # 22+23+24 = 23
    ma6 = np.mean(closes[-6:])                            # 21.5
    ma12 = np.mean(closes[-12:])                          # 18.5
    ma24 = np.mean(closes[-24:])                          # 12.5
    assert b[-1] == pytest.approx((ma3 + ma6 + ma12 + ma24) / 4)
