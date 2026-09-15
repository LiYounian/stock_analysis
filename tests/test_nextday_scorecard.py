"""锁：次日实盘口径累积胜率牌（2026-09-15 整改设计 §4，阶段2A）。

为什么改（守则#6）：
  · 绝对收益口径 = D+1收盘 / 入场价 − 1（防未来：只用两价）；是否为正 = 严格 >0；
  · **未触发不计入胜率分母**（诚实，不拿没买的票充数），单列统计未触发数；
  · 胜率牌 **append-only + 幂等**：同 date 重跑=覆盖当日行，绝不重复累加、绝不回改历史。
"""
from __future__ import annotations

import pandas as pd
import pytest

from tools.analysis import nextday_scorecard as nsc


# ———————————————————— 绝对收益口径 + 是否为正 ————————————————————
def test_abs_return_formula():
    # (12.30/12.00 − 1)×100 = +2.5%
    assert nsc.abs_return_pct(12.00, 12.30) == 2.5
    # (8.14/8.30 − 1)×100 ≈ −1.9277%
    assert nsc.abs_return_pct(8.30, 8.14) == pytest.approx(-1.9277, abs=1e-3)


def test_abs_return_missing_or_bad_entry_none():
    assert nsc.abs_return_pct(None, 12.3) is None       # 未触发/无入场价 → None
    assert nsc.abs_return_pct(12.0, None) is None
    assert nsc.abs_return_pct(0, 12.3) is None           # 入场价≤0 → None（不臆造）
    assert nsc.abs_return_pct(True, 12.3) is None        # bool 不算数字


def test_is_close_positive_strict():
    assert nsc.is_close_positive(2.5) is True
    assert nsc.is_close_positive(-1.0) is False
    assert nsc.is_close_positive(0.0) is False           # "为正"严格 >0
    assert nsc.is_close_positive(None) is None           # 未触发 → None


# ———————————————————— 逐日聚合 ————————————————————
def _picks_mixed():
    # 3 触发（2 正 1 负）+ 1 未触发；负单由 β 拖累
    return [
        {"abs_return_pct": 2.5, "close_positive": True, "alpha": 1.2,
         "sell_line_hit": True},
        {"abs_return_pct": 1.0, "close_positive": True, "alpha": -0.3,
         "sell_line_hit": False},
        {"abs_return_pct": -1.93, "close_positive": False, "alpha": -0.8,
         "beta_drag": True, "sell_line_hit": None},
        {"abs_return_pct": None, "close_positive": None},        # 未触发
    ]


def test_untriggered_excluded_from_denominator():
    row = nsc.compute_daily_row("2026-09-15", _picks_mixed())
    assert row["n_buy"] == 3                 # 未触发不进分母
    assert row["n_untriggered"] == 1
    assert row["abs_win_rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_aggregate_values():
    row = nsc.compute_daily_row("2026-09-15", _picks_mixed())
    assert row["avg_abs_return"] == pytest.approx((2.5 + 1.0 - 1.93) / 3, abs=1e-4)
    assert row["avg_alpha"] == pytest.approx((1.2 - 0.3 - 0.8) / 3, abs=1e-4)
    # 亏损单 1 只、其中 β 拖累 1 只 → 占比 1.0
    assert row["beta_drag_loss_ratio"] == 1.0
    # D+2 卖出线有跟踪的 2 只（True/False），达成 1 → 0.5
    assert row["sell_line_hit_rate"] == 0.5


def test_all_untriggered_rates_none_denominator_zero():
    row = nsc.compute_daily_row("2026-09-15",
                                [{"close_positive": None}, {"close_positive": None}])
    assert row["n_buy"] == 0 and row["n_untriggered"] == 2
    assert row["abs_win_rate"] is None          # 分母 0 → None（不臆造 0%）
    assert row["avg_abs_return"] is None and row["avg_alpha"] is None
    assert row["beta_drag_loss_ratio"] is None and row["sell_line_hit_rate"] is None


def test_no_losses_beta_ratio_none():
    row = nsc.compute_daily_row("2026-09-15",
                                [{"abs_return_pct": 2.5, "close_positive": True}])
    assert row["beta_drag_loss_ratio"] is None  # 无亏损单 → 占比 None


def test_row_has_frozen_columns():
    row = nsc.compute_daily_row("2026-09-15", _picks_mixed())
    assert list(row.keys()) == nsc.COLUMNS


# ———————————————————— append 幂等 ————————————————————
def test_append_creates_file(tmp_path):
    p = tmp_path / "nextday_scorecard.csv"
    nsc.append_daily_row(nsc.compute_daily_row("2026-09-15", _picks_mixed()), csv_path=p)
    df = pd.read_csv(p, dtype={"date": str})
    assert list(df.columns) == nsc.COLUMNS
    assert len(df) == 1 and df.iloc[0]["date"] == "2026-09-15"


def test_append_same_date_idempotent_overwrite(tmp_path):
    """同 date 重跑=覆盖当日行，绝不重复累加。"""
    p = tmp_path / "nextday_scorecard.csv"
    nsc.append_daily_row(nsc.compute_daily_row("2026-09-15", _picks_mixed()), csv_path=p)
    # 重跑同日、但值变了（多 1 只未触发）
    picks2 = _picks_mixed() + [{"close_positive": None}]
    nsc.append_daily_row(nsc.compute_daily_row("2026-09-15", picks2), csv_path=p)
    df = pd.read_csv(p, dtype={"date": str})
    assert len(df) == 1                                   # 只一行（幂等覆盖）
    assert int(df.iloc[0]["n_untriggered"]) == 2         # 取的是最新值（覆盖非累加）


def test_append_different_dates_accumulate_sorted(tmp_path):
    p = tmp_path / "nextday_scorecard.csv"
    nsc.append_daily_row(nsc.compute_daily_row("2026-09-16", _picks_mixed()), csv_path=p)
    nsc.append_daily_row(nsc.compute_daily_row("2026-09-15", _picks_mixed()), csv_path=p)
    df = pd.read_csv(p, dtype={"date": str})
    assert list(df["date"]) == ["2026-09-15", "2026-09-16"]   # append-only 累积、按日升序


def test_update_scorecard_convenience(tmp_path):
    p = tmp_path / "nextday_scorecard.csv"
    row, path = nsc.update_scorecard("2026-09-15", _picks_mixed(), csv_path=p)
    assert row["n_buy"] == 3 and path == str(p)
    assert (tmp_path / "nextday_scorecard.csv").is_file()
