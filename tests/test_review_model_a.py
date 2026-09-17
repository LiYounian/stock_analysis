"""tools/review/model_a：Model-A 撮合 + r_exit。**锁死两条语义**（统筹要求）：
① 未触发不进胜率分母（close_positive=None）；② 成交价=min(限价, D+1 open)。
外加 r_exit 分支（持到 D2 / 当日离场 / 破 MA5）+ α 源缺失不炸。

不碰生产 data：预置 sector_news_forward._kline 的 cache（synthetic bars），撮合走真实复用代码。
"""
from __future__ import annotations

import math

from tools.review.model_a import compute_labels, ensure_ew, parse_self_entry
from tools.review.types import Pick

D = "2026-09-16"


def _cache(bars: list[tuple[str, float, float, float]]) -> dict:
    """bars=[(date,open,low,close)] → sector_news_forward._kline 的 cache 形态 {code:(rows,d2i)}。"""
    d2i = {b[0]: i for i, b in enumerate(bars)}
    return {"TEST": (bars, d2i)}


def _pick(ma5=None, 入场=None) -> Pick:
    return Pick(date=D, code="TEST", name="测试", 形态=({"ma5": ma5} if ma5 else {}), 入场_text=入场)


def test_filled_min_limit_open_and_hold_to_d2():
    """成交·低开取开盘价(min撮合)·D1 为正未破 MA5 → 持到 D2。"""
    bars = [(D, 10.0, 9.8, 10.0), ("D1", 9.5, 9.4, 10.5), ("D2", 10.6, 10.4, 11.0)]
    lab = compute_labels(_pick(ma5=9.0), _cache(bars), ew={})
    assert lab.filled is True
    assert lab.entry_price == 9.5                      # ② 成交价 = min(限价10.0, 次开9.5) = 9.5
    assert math.isclose(lab.r_d1, (10.5 / 9.5 - 1) * 100, rel_tol=1e-9)
    assert lab.close_positive is True
    assert lab.stop_flag is False and lab.hold_to_d2 is True
    assert lab.exit_horizon == "d2"
    assert math.isclose(lab.r_exit, (11.0 / 9.5 - 1) * 100, rel_tol=1e-9)   # r_exit=r_d2


def test_untriggered_not_in_denominator():
    """① 高开未回踩(low>限价)→未触发：close_positive=None（剔出分母）、r_exit=None。"""
    bars = [(D, 10.0, 9.8, 10.0), ("D1", 10.6, 10.2, 10.8)]   # D1 low 10.2 > 限价 10.0
    lab = compute_labels(_pick(ma5=9.0), _cache(bars), ew={})
    assert lab.filled is False
    assert lab.untriggered is True
    assert lab.close_positive is None                  # ① 未触发不进胜率分母
    assert lab.r_d1 is None and lab.r_exit is None
    assert lab.status == "not_entered"


def test_filled_close_negative_exits_d1():
    """成交·D1 收盘为负 → 当日 D1 离场，r_exit=r_d1（负）。"""
    bars = [(D, 10.0, 8.0, 10.0), ("D1", 9.9, 9.0, 9.5), ("D2", 9.4, 9.0, 9.2)]
    lab = compute_labels(_pick(ma5=8.0), _cache(bars), ew={})
    assert lab.filled is True and lab.entry_price == 9.9
    assert lab.r_d1 < 0 and lab.close_positive is False
    assert lab.hold_to_d2 is False and lab.exit_horizon == "d1"
    assert math.isclose(lab.r_exit, lab.r_d1, rel_tol=1e-9)


def test_break_ma5_exits_d1_even_if_positive():
    """成交·D1 收盘为正但盘中破 MA5 → 当日离场（不持到 D2）。"""
    bars = [(D, 10.0, 9.8, 10.0), ("D1", 9.9, 9.5, 10.3), ("D2", 10.4, 10.2, 11.0)]
    lab = compute_labels(_pick(ma5=9.8), _cache(bars), ew={})   # d1_low 9.5 < ma5 9.8
    assert lab.close_positive is True                  # 收盘为正
    assert lab.stop_flag is True                       # 但盘中破 MA5
    assert lab.hold_to_d2 is False and lab.exit_horizon == "d1"
    assert math.isclose(lab.r_exit, lab.r_d1, rel_tol=1e-9)     # 当日离场取 r_d1


def test_hold_to_d2_pending_when_d2_immature():
    """持到 D2 但 D+2 未到期 → r_exit=None（pending），不臆造。"""
    bars = [(D, 10.0, 9.8, 10.0), ("D1", 9.5, 9.4, 10.5)]       # 无 D+2 bar
    lab = compute_labels(_pick(ma5=9.0), _cache(bars), ew={})
    assert lab.hold_to_d2 is True and lab.r_d2 is None
    assert lab.r_exit is None and lab.status == "partial"


def test_alpha_null_when_ew_missing():
    """α 源缺失（ew={}）→ α 列 null 有声缺失，不炸。"""
    bars = [(D, 10.0, 9.8, 10.0), ("D1", 9.5, 9.4, 10.5), ("D2", 10.6, 10.4, 11.0)]
    lab = compute_labels(_pick(ma5=9.0), _cache(bars), ew={})
    assert lab.alpha_d1 is None and lab.alpha_exit is None      # 绝不顶替/编


def test_alpha_computed_when_ew_present():
    """ew 有值 → excess 正常算（超额=个股−全A等权同期）。"""
    bars = [(D, 10.0, 9.8, 10.0), ("D1", 9.5, 9.4, 10.5), ("D2", 10.6, 10.4, 11.0)]
    ew = {D: 100.0, "D1": 101.0, "D2": 102.0}                  # 全A等权 D→D1 +1%、D→D2 +2%
    lab = compute_labels(_pick(ma5=9.0), _cache(bars), ew=ew)
    assert lab.alpha_d1 is not None
    assert math.isclose(lab.alpha_d1, lab.r_d1 - 1.0, rel_tol=1e-9)      # r_d1 − bench_d1(+1%)
    assert math.isclose(lab.alpha_exit, lab.r_exit - 2.0, rel_tol=1e-9)  # 持到 D2：r_exit − bench_d2(+2%)


def test_ensure_ew_missing_source_graceful(tmp_path, monkeypatch):
    """market_ew.parquet 缺 + 构建不可得 → 返回 {}，不炸。"""
    import tools.research.finval.build_market_index as bmi
    monkeypatch.setattr(bmi, "main", lambda: (_ for _ in ()).throw(RuntimeError("no breadth")))
    assert ensure_ew(str(tmp_path), build_if_missing=True) == {}
    assert ensure_ew(str(tmp_path), build_if_missing=False) == {}


def test_parse_self_entry():
    assert parse_self_entry("缩量回踩MA5(39.88)不破可轻仓") == 39.88
    assert parse_self_entry("回踩启动均线限价") is None
    assert parse_self_entry(None) is None
