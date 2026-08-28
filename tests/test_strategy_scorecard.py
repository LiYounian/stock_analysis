"""锁 strategy_scorecard 三条硬规则 + 预测提取。

用**合成 kline**(注入 KlineBook.loader),不依赖真实行情,单测可离线跑。
锁死语义:① 防未来函数(未到期排除)② 中途翻转不反算 ③ 5日双口径 + 方向感知,
以及 extract_picks 对三种 view 结构(默认多头/综合方向/排行按期)的解析。
"""
from __future__ import annotations

import pandas as pd

from tools.backtest import strategy_scorecard as ss


def _mk_book(prices_by_code: dict):
    """prices_by_code: {code: [(date, open, high, low, close), ...]} → KlineBook(注入 loader)。"""
    def loader(code):
        rows = prices_by_code[code]
        return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"])
    return ss.KlineBook(loader=loader)


# ────────────────── 规则①:防未来函数 ──────────────────
def test_pending_excluded_no_future():
    """预测日在 kline 末尾,idx+h 越界 → matured=False,r/hit 全 None(不用未到期收益)。"""
    book = _mk_book({"X": [
        ("2026-08-25", 10, 10, 10, 10.0),
        ("2026-08-26", 10, 11, 10, 11.0),   # 最后一根 = 预测日,h=1/5 都越界
    ]})
    fr = book_forward(book, "X", "2026-08-26", +1, (1, 5))
    assert fr[1]["matured"] is False and fr[1]["r"] is None and fr[1]["hit_end"] is None
    assert fr[5]["matured"] is False and fr[5]["r"] is None

    # 有一根未来 bar 时,h=1 到期、h=5 仍 pending
    book2 = _mk_book({"X": [
        ("2026-08-25", 10, 10, 10, 10.0),   # idx0 = 预测日
        ("2026-08-26", 10, 12, 10, 11.0),   # +1
    ]})
    fr2 = book_forward(book2, "X", "2026-08-25", +1, (1, 5))
    assert fr2[1]["matured"] is True and abs(fr2[1]["r"] - 10.0) < 1e-9
    assert fr2[5]["matured"] is False


# ────────────────── 规则②:中途翻转不反算 ──────────────────
def test_midway_flip_independent():
    """T 预测+1、T+2 预测−1,互不影响:各自只对自己 T→T+h 窗口打分。"""
    # 价格:T=100 → T+1=105(涨)→ T+2=104 → T+3=99(跌)
    book = _mk_book({"X": [
        ("2026-08-20", 100, 100, 100, 100.0),   # idx0 = T
        ("2026-08-21", 105, 106, 99, 105.0),    # T+1
        ("2026-08-24", 104, 104, 103, 104.0),   # idx2 = T+2
        ("2026-08-25", 99, 100, 98, 99.0),      # T+3
    ]})
    # T 那次(+1,h=1):T→T+1 涨 → 命中(与 T+2 后来翻空无关)
    frT = book_forward(book, "X", "2026-08-20", +1, (1,))
    assert frT[1]["hit_end"] == 1
    # T+2 那次(−1,h=1):T+2→T+3 跌 → 看空命中,且不回溯改 T
    frT2 = book_forward(book, "X", "2026-08-24", -1, (1,))
    assert frT2[1]["hit_end"] == 1
    # 再确认 T 那次没被 T+2 影响
    assert book_forward(book, "X", "2026-08-20", +1, (1,))[1]["hit_end"] == 1


# ────────────────── 规则③:5日双口径 + 方向感知 ──────────────────
def test_dual_caliber_touched_but_closed_down():
    """看多:5日内触到更高(期内命中)但期末收跌(期末不命中)→ hit_intra=1, hit_end=0。"""
    book = _mk_book({"X": [
        ("d0", 100, 100, 100, 100.0),   # idx0 = 预测日,入场 close=100
        ("d1", 100, 108, 99, 101.0),    # 盘中冲到 108 > 100 → 期内触及
        ("d2", 101, 102, 98, 99.0),
        ("d3", 99, 100, 97, 98.0),
        ("d4", 98, 99, 96, 97.0),
        ("d5", 97, 98, 95, 96.0),       # T+5 收 96 < 100 → 期末收跌
    ]})
    fr = book_forward(book, "X", "d0", +1, (5,))
    assert fr[5]["matured"] is True
    assert fr[5]["hit_end"] == 0        # 期末:收跌,看多未命中
    assert fr[5]["hit_intra"] == 1      # 期内:触到过更高


def test_direction_aware_bear_and_neutral():
    """看空价跌→期末命中;中性(dir=0)→ hit_end/intra 均 None(不计命中)。"""
    book = _mk_book({"X": [
        ("d0", 100, 100, 100, 100.0),
        ("d1", 99, 100, 90, 95.0),      # 收跌
    ]})
    fr_bear = book_forward(book, "X", "d0", -1, (1,))
    assert fr_bear[1]["hit_end"] == 1
    fr_neu = book_forward(book, "X", "d0", 0, (1,))
    assert fr_neu[1]["matured"] is True and fr_neu[1]["hit_end"] is None and fr_neu[1]["hit_intra"] is None


# ────────────────── 预测提取 ──────────────────
def test_extract_default_long():
    picks = ss.extract_picks({"as_of": "2026-08-27", "方向": "看多",
                              "入选清单": [{"code": "600000"}, {"code": "000001"}]})
    assert len(picks) == 2 and all(p["dir"] == 1 and p["horizons"] is None for p in picks)


def test_extract_council_direction():
    picks = ss.extract_picks({"top": [
        {"code": "A", "综合方向": "看多"},
        {"code": "B", "综合方向": "看空"},
        {"code": "C", "综合方向": "中性"}]})
    assert {p["code"]: p["dir"] for p in picks} == {"A": 1, "B": -1, "C": 0}


def test_extract_rank_per_horizon():
    picks = ss.extract_picks({"排行": {
        "1日": [{"code": "A", "方向": "看多"}],
        "5日": [{"code": "A", "方向": "中性"}],
        "10日": [{"code": "A", "方向": "看空"}]}})
    by = {(p["code"], p["horizons"][0]): p["dir"] for p in picks}
    assert by[("A", 1)] == 1 and by[("A", 5)] == 0 and by[("A", 10)] == -1


# ────────────────── helper ──────────────────
def book_forward(book, code, date, direction, horizons):
    return ss.forward_returns(code, date, direction, horizons, book)
