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


# ────────────────── 滚动时间窗 + base-rate 超额 ──────────────────
def _mk_rows(recs):
    """recs: list of (sid, date, code, dir, h, matured, hit_end, hit_intra) → rows DataFrame(对齐 build_rows)。"""
    cols = ["strategy_id", "strategy", "file", "date", "code", "dir", "h",
            "matured", "r", "hit_end", "hit_intra"]
    out = []
    for sid, date, code, d, h, matured, he, hi in recs:
        out.append({"strategy_id": sid, "strategy": f"名{sid}", "file": sid,
                    "date": date, "code": code, "dir": d, "h": h, "matured": matured,
                    "r": 1.0 if he else -1.0, "hit_end": he, "hit_intra": hi})
    return pd.DataFrame(out, columns=cols)


def test_window_bucketing_by_trading_days():
    """窗口按交易日历最近 N 天分桶:近3只含 D08/D09/D10,更早的 D07 被排除。"""
    cal = [f"D{i:02d}" for i in range(1, 11)]          # D01..D10 十个交易日
    rows = _mk_rows([
        ("A", "D07", "x", 1, 1, True, 1, 1),           # 在窗外
        ("A", "D08", "x", 1, 1, True, 1, 1),
        ("A", "D09", "x", 1, 1, True, 0, 1),
        ("A", "D10", "x", 1, 1, True, 1, 1),
    ])
    mb = {(d, 1): [5, 10] for d in ["D07", "D08", "D09", "D10"]}
    agg = ss.aggregate_windows(rows, cal, mb, horizons=(1,), windows={"近3": 3})
    w = agg["窗口"]["近3"]
    assert w["数据充足"] is True                         # span(D07→D10)=4 ≥ 3
    a1 = w["策略"]["A"]["1日"]
    assert a1["已到期样本"] == 3                          # D08/D09/D10,不含 D07
    assert a1["预测日数"] == 3


def test_base_rate_and_excess():
    """基准率池化同预测日集合的全市场 up/total;超额=策略命中−基准。"""
    cal = [f"D{i:02d}" for i in range(1, 6)]            # D01..D05
    rows = _mk_rows([
        ("A", "D04", "x", 1, 1, True, 1, 1),
        ("A", "D04", "y", 1, 1, True, 1, 1),
        ("A", "D05", "z", 1, 1, True, 1, 1),
        ("A", "D05", "w", 1, 1, True, 0, 1),           # 命中 3/4 = 75%
    ])
    mb = {("D04", 1): [6, 10], ("D05", 1): [4, 10]}    # 池化 up=10 tot=20 → base=50%
    agg = ss.aggregate_windows(rows, cal, mb, horizons=(1,), windows={"近5": 5})
    c = agg["窗口"]["近5"]["策略"]["A"]["1日"]
    assert c["命中率%_期末"] == 75.0
    assert c["基准率%"] == 50.0
    assert c["超额命中%"] == 25.0


def test_window_data_insufficient_flag():
    """窗口 N 大于观测跨度 → 数据充足=False 且说明含'数据不足',实际=跨度而非 N。"""
    cal = [f"D{i:02d}" for i in range(1, 11)]           # 十个交易日
    rows = _mk_rows([                                   # 最早预测日 D08 → span=3
        ("A", "D08", "x", 1, 1, True, 1, 1),
        ("A", "D10", "x", 1, 1, True, 1, 1),
    ])
    mb = {(d, 1): [5, 10] for d in ["D08", "D10"]}
    agg = ss.aggregate_windows(rows, cal, mb, horizons=(1,), windows={"近一年": 250})
    w = agg["窗口"]["近一年"]
    assert w["数据充足"] is False
    assert w["实际覆盖交易日"] == 3                       # min(250, span=3)
    assert "数据不足" in w["说明"] and "250" in w["说明"]


def test_first_pred_on_non_trading_day_span():
    """最早预测日落在非交易日(周末批处理)时,span 用 bisect 不退化成整段历史。"""
    cal = [f"D{i:02d}" for i in range(1, 11)]           # 无 'D08x'
    rows = _mk_rows([
        ("A", "D08x", "x", 1, 1, True, 1, 1),          # 非交易日(排在 D08 与 D09 之间)
        ("A", "D09", "x", 1, 1, True, 1, 1),
        ("A", "D10", "x", 1, 1, True, 1, 1),
    ])
    mb = {(d, 1): [5, 10] for d in ["D08x", "D09", "D10"]}
    agg = ss.aggregate_windows(rows, cal, mb, horizons=(1,), windows={"近一年": 250})
    # bisect_left(cal,'D08x') = 8 (D08 之后) → span = 10-8 = 2,远小于 250 → 数据不足
    assert agg["窗口"]["近一年"]["数据充足"] is False
    assert agg["窗口"]["近一年"]["实际覆盖交易日"] == 2


def test_market_baseline_no_future_function():
    """base-rate 只用已到期收益:idx+h 越界的票不进 total;不同票缺日各自排除。"""
    book = _mk_book({
        "A": [("D01", 10, 10, 10, 10.0), ("D02", 11, 11, 11, 11.0), ("D03", 9, 9, 9, 9.0)],
        "B": [("D01", 10, 10, 10, 10.0), ("D02", 9, 9, 9, 9.0)],
    })
    base = ss.build_market_baseline(["D01", "D02", "D03"], (1,), ["A", "B"], book)
    # D01,h1: A 10→11 涨, B 10→9 跌 → up=1 tot=2
    assert base[("D01", 1)] == [1, 2]
    # D02,h1: A 11→9 跌(tot1 up0); B 无 D03 → idx+1 越界排除
    assert base[("D02", 1)] == [0, 1]
    # D03,h1: A idx+1 越界排除;B 无 D03 → 全空(防未来函数)
    assert base[("D03", 1)] == [0, 0]


def test_base_rate_pooling_helper():
    """_base_rate 对给定日集合池化 up/total;全 0 或空 → None(基准不可算)。"""
    mb = {("D01", 1): [3, 10], ("D02", 1): [7, 10], ("D03", 1): [0, 0]}
    assert ss._base_rate(["D01", "D02"], 1, mb) == 50.0    # (3+7)/(10+10)
    assert ss._base_rate(["D03"], 1, mb) is None           # tot=0 → 不可算
    assert ss._base_rate(["ZZ"], 1, mb) is None            # 缺失 → 不可算


# ────────────────── helper ──────────────────
def book_forward(book, code, date, direction, horizons):
    return ss.forward_returns(code, date, direction, horizons, book)
