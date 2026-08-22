"""反转低换手组合(候选策略)单测。

断言锁住"为什么这么设计"的语义,防未来重写误删规则:
  · 因子方向(反转:跌得多高分;低换手:冷门高分)
  · 有界回看 = 防未来函数(因子只读尾部窗口,窗口外/未来值不影响"截至 t"的取值)
  · 近端数据卫生(turnover 近端 NaN → 窗口有效值均值兜底;不足半窗 → None)
  · 横截面标准化 + 等权复合方向
  · 业务过滤(停牌/涨跌停/低流动性/剥离板块头/因子缺失)分类计数
  · 样本 <2 降级、空 records 不炸、top_k 生效
"""
from __future__ import annotations

import math

import pytest

from tools.strategy import registry as reg
from tools.strategy import reversal_turnover as rt

NAN = float("nan")


# ————————————————————————————— 纯因子函数 —————————————————————————————
def test_reversal_direction():
    """跌得多 → 反转因子更高(值越大越看好)。"""
    down = rt.reversal_factor([10, 10, 10, 10, 10, 8], n=5)     # 跌 20%
    up = rt.reversal_factor([10, 10, 10, 10, 10, 12], n=5)      # 涨 20%
    assert down > up
    assert math.isclose(down, 0.2, abs_tol=1e-9)                # -(0.8-1)= +0.2
    assert math.isclose(up, -0.2, abs_tol=1e-9)


def test_reversal_insufficient_or_illegal():
    """不足 N+1 根 / 基准价 ≤0 / 含 NaN → None。"""
    assert rt.reversal_factor([1, 2, 3], n=5) is None
    assert rt.reversal_factor([0, 1, 2, 3, 4, 5], n=5) is None  # 基准价=0
    assert rt.reversal_factor([1, 2, 3, 4, 5, NAN], n=5) is None


def test_reversal_bounded_lookback_is_anti_future():
    """防未来函数:反转只读最后 N+1 根;窗口外的历史值改变不影响结果。"""
    base = [1, 2, 3, 100, 100, 100, 100, 100, 105]             # n=5:只看最后 6 根
    v1 = rt.reversal_factor(base, n=5)
    mutated = [999, -50, 7] + base[3:]                          # 只改窗口外(前 3 根)
    v2 = rt.reversal_factor(mutated, n=5)
    assert v1 == v2


def test_reversal_slice_stable_against_future():
    """防未来函数:'截至 t'的取值 = factor(series[:t+1]),与 t 之后的未来值无关。"""
    full = [10, 11, 12, 13, 14, 15, 16, 17, 18]
    t = 5
    as_of_t = rt.reversal_factor(full[: t + 1], n=5)
    # 未来段(t 之后)无论怎么变,重算'截至 t'都不变
    future_a = full[: t + 1] + [999, 0.1, 500]
    future_b = full[: t + 1] + [-1, -1, -1]
    assert rt.reversal_factor(future_a[: t + 1], n=5) == as_of_t
    assert rt.reversal_factor(future_b[: t + 1], n=5) == as_of_t


def test_low_turnover_direction():
    """换手越低 → 低换手因子越高(取负均值)。"""
    cold = rt.low_turnover_factor([0.5] * 20, n=20)             # 冷门
    hot = rt.low_turnover_factor([8.0] * 20, n=20)             # 活跃
    assert cold > hot
    assert math.isclose(cold, -0.5, abs_tol=1e-9)


def test_low_turnover_nan_tail_fallback():
    """近端 NaN(采集滞后)→ 用窗口内有效值均值兜底。"""
    # 20 根:前 15 有效(均=2.0)、后 5 为 NaN → 均值=2.0 → 因子=-2.0
    tv = [2.0] * 15 + [NAN] * 5
    assert math.isclose(rt.low_turnover_factor(tv, n=20), -2.0, abs_tol=1e-9)


def test_low_turnover_too_few_valid_returns_none():
    """有效点 < 半窗(默认 max(1,N//2))→ None(不静默造假)。"""
    tv = [2.0] * 5 + [NAN] * 15                                 # n=20 需 ≥10 有效,仅 5
    assert rt.low_turnover_factor(tv, n=20) is None


def test_low_turnover_uses_only_last_n():
    """只用最后 N 根:更早的历史值不影响。"""
    v1 = rt.low_turnover_factor([1.0] * 20, n=20)
    v2 = rt.low_turnover_factor([99.0] * 30 + [1.0] * 20, n=20)
    assert v1 == v2


def test_avg_amount_wan_unit_and_nan():
    """成交额均值:元 → 万元(/1e4);近端 NaN 按有效值。"""
    amts = [5e7] * 15 + [NAN] * 5                               # 5e7 元 = 5000 万元
    assert math.isclose(rt.avg_amount_wan(amts, n=20), 5000.0, abs_tol=1e-6)


def test_pv_diverge_direction():
    """量价背离:价涨量缩(负相关)→ 取负后高分;价量同向 → 低分。"""
    closes = [10 + i for i in range(21)]                        # 单调涨
    vol_shrink = [1000 - 10 * i for i in range(21)]            # 单调缩 → 与价负相关
    vol_grow = [1000 + 10 * i for i in range(21)]             # 单调增 → 与价正相关
    div = rt.pv_diverge_factor(closes, vol_shrink, window=20)
    conf = rt.pv_diverge_factor(closes, vol_grow, window=20)
    assert div > conf


# ————————————————————————————— 选股策略 —————————————————————————————
def _rec(rev, turn, amount_wan=9999.0, pct_chg=0.5):
    """最小中心记录:预算好的原始因子 + snapshot 过业务过滤。"""
    return {
        "反转低换手": {"rev": rev, "turn": turn, "amount_wan": amount_wan},
        "snapshot": {"pct_chg": pct_chg, "close": 10.0},
    }


def test_registered():
    meta = reg.get("反转低换手组合")
    assert meta.kind == "选股"
    assert callable(meta.fn)


def test_composite_ranking_direction():
    """等权复合:反转高 + 换手低(两因子都好)的票综合分最高。"""
    recs = {
        "BEST": _rec(rev=0.30, turn=-0.5),      # 跌得多 + 换手低
        "MID": _rec(rev=0.00, turn=-3.0),
        "WORST": _rec(rev=-0.30, turn=-8.0),    # 涨得多 + 换手高
    }
    out = reg.run("反转低换手组合", recs, top_k=3)
    assert out["codes"][0] == "BEST"
    assert out["codes"][-1] == "WORST"


def test_weights_applied():
    """综合分 = w_rev·z(rev) + w_turn·z(turn),验一次加权等式。"""
    recs = {"A": _rec(0.2, -1.0), "B": _rec(-0.1, -5.0), "C": _rec(0.05, -2.0)}
    out = reg.run("反转低换手组合", recs, top_k=3)
    w = out["权重"]
    d = out["因子明细"][0]
    assert math.isclose(d["综合分"], d["rev_z"] * w["反转"] + d["turn_z"] * w["低换手"],
                        abs_tol=1e-4)


def test_filter_limit_up_down_and_paused():
    """涨跌停(|pct_chg|≥9.7)/ 停牌(无 snapshot)→ 剔除并计数。"""
    recs = {
        "UP": _rec(0.1, -1.0, pct_chg=9.9),
        "DN": _rec(0.1, -1.0, pct_chg=-9.8),
        "PAUSED": {"反转低换手": {"rev": 0.1, "turn": -1.0, "amount_wan": 9999.0},
                   "snapshot": None},
        "OK1": _rec(0.1, -1.0),
        "OK2": _rec(0.05, -2.0),
    }
    out = reg.run("反转低换手组合", recs, top_k=5)
    assert set(out["codes"]) == {"OK1", "OK2"}
    assert out["跳过"].get("涨跌停") == 2
    assert out["跳过"].get("停牌或无快照") == 1


def test_filter_low_liquidity():
    """低流动性(成交额均值 < 阈值)→ 剔除并计数。"""
    recs = {
        "ILLIQ": _rec(0.2, -1.0, amount_wan=100.0),    # 100 万元 < 5000 默认阈值
        "OK1": _rec(0.1, -1.0, amount_wan=9999.0),
        "OK2": _rec(0.05, -2.0, amount_wan=9999.0),
    }
    out = reg.run("反转低换手组合", recs, top_k=5)
    assert "ILLIQ" not in out["codes"]
    assert out["跳过"].get("低流动性") == 1


def test_filter_missing_factor():
    """因子缺失(rev 或 turn 为 None/NaN)→ 剔除并计数。"""
    recs = {
        "NO_REV": _rec(None, -1.0),
        "NO_TURN": _rec(0.1, None),
        "OK1": _rec(0.1, -1.0),
        "OK2": _rec(0.05, -2.0),
    }
    out = reg.run("反转低换手组合", recs, top_k=5)
    assert set(out["codes"]) == {"OK1", "OK2"}
    assert out["跳过"].get("因子缺失") == 2


def test_exclude_board_head_toggle():
    """剥离开关=True:创业/科创/北交/退 头被剔;默认(False)纳入。"""
    recs = {
        "600001": _rec(0.1, -1.0),
        "000002": _rec(0.05, -2.0),     # 深主板(保留)
        "300001": _rec(0.2, -0.5),      # 创业
        "688001": _rec(0.15, -0.8),     # 科创
    }
    off = reg.run("反转低换手组合", recs, top_k=5)          # 默认不剥离
    assert set(off["codes"]) == {"600001", "000002", "300001", "688001"}
    on = reg.run("反转低换手组合", recs, top_k=5, exclude_board_head=True)
    assert set(on["codes"]) == {"600001", "000002"}         # 剥离后仍 ≥2 只可标准化
    assert on["跳过"].get("剥离板块头") == 2


def test_top_k_and_ordering():
    """top_k 生效 + 综合分降序。"""
    recs = {f"S{i:02d}": _rec(rev=i * 0.01, turn=-float(i)) for i in range(1, 9)}
    out = reg.run("反转低换手组合", recs, top_k=3)
    scores = [d["综合分"] for d in out["因子明细"]]
    assert scores == sorted(scores, reverse=True)
    assert out["codes"] == [d["code"] for d in out["因子明细"][:3]]
    assert len(out["codes"]) == 3


def test_less_than_2_samples_returns_note():
    """样本 <2 无法横截面标准化 → 空 + note。"""
    out = reg.run("反转低换手组合", {"A": _rec(0.1, -1.0)}, top_k=5)
    assert out["codes"] == []
    assert "note" in out


def test_empty_records():
    """空 / None records 不炸。"""
    for r in ({}, None):
        out = reg.run("反转低换手组合", r, top_k=3)
        assert out["codes"] == []


def test_detail_exposes_raw_and_zscored():
    """因子明细同时暴露原始值与标准化值(供 web 展示)。"""
    out = reg.run("反转低换手组合",
                  {"A": _rec(0.2, -1.0), "B": _rec(-0.1, -5.0)}, top_k=2)
    d0 = out["因子明细"][0]
    for k in ("code", "综合分", "rev", "turn", "amount_wan", "rev_z", "turn_z"):
        assert k in d0
