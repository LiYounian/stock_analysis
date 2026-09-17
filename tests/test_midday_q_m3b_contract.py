"""午盘 Q · M3.b 分时回测契约测试。

锁语义(约法第6条):
    · 防未来 —— `_cum_to` 绝不读 as_of 之后的 bar
    · 成交可行性 —— 一字涨停/跌停的识别与"强制顺延持有 + 累计双边成本"
    · 组合口径回撤 —— 修 M3.a 逐笔累加的错口径(这是本模块存在的主要理由之一)
    · Q3 不在 STRATEGIES 里(分时资金流无历史,不能假装能回测)
"""
from __future__ import annotations

import pandas as pd
import pytest

from tools.backtest import backtest_midday_q_m3b as M
from tools.backtest import fetch_midday_q_bars as F


# ────────────────────────────── 采集侧 ──────────────────────────────

def test_bs_code_mapping():
    assert F._bs_code("600519") == "sh.600519"
    assert F._bs_code("900001") == "sh.900001"
    assert F._bs_code("000021") == "sz.000021"
    assert F._bs_code("300308") == "sz.300308"
    assert F._bs_code("688008") == "sh.688008"


def test_keep_times_covers_decision_points():
    """1430/1450 是午盘Q的判定时点,必须在采集时刻里;0935 作当日 open 锚。"""
    for t in ("0935", "1430", "1450"):
        assert t in F.KEEP_TIMES, f"{t} 不在 KEEP_TIMES,回测拿不到判定价"


# ── 全A票池 / 北交所排除 ──

def test_fullA_excludes_beijing():
    """北交所(4/8/92 段)必须排除 —— 涨跌幅规则与流动性都不同,午盘Q口径不含。"""
    codes = F.fullA_codes()
    assert codes, "全A票池为空"
    bad = [c for c in codes if c.startswith(("4", "8", "92"))]
    assert not bad, f"北交所代码未排除:{bad[:5]}"
    assert all(len(c) == 6 for c in codes), "存在非6位代码"
    # 规模合理性:主板+创业板+科创板,应在数千量级
    assert 3000 < len(codes) < 8000, f"全A数量异常:{len(codes)}"


def test_fullA_is_sorted_and_unique():
    """排序+去重 → 断点续跑时顺序稳定,不会重复取。"""
    codes = F.fullA_codes()
    assert codes == sorted(codes)
    assert len(codes) == len(set(codes))


def test_resolve_codes_switches_universe():
    focus = F.resolve_codes("focus")
    full = F.resolve_codes("fullA")
    assert len(full) > len(focus), "fullA 应远多于 focus"
    assert len(F.resolve_codes("fullA", limit=7)) == 7


# ── 幂等判据必须校验内容,不只是存在性(坑②:并发会留半份文件) ──

def test_ok_on_disk_rejects_missing_file(tmp_path):
    assert not F._ok_on_disk(tmp_path / "nope.parquet", "2026-01-02", "2026-09-10")


def test_ok_on_disk_rejects_partial_range(tmp_path):
    """文件存在但区间没覆盖到 → 判不可信,要重取。"""
    p = tmp_path / "x.parquet"
    pd.DataFrame([{"date": "2026-05-01", "time": t, "open": 1.0, "high": 1.0,
                    "low": 1.0, "close": 1.0, "volume": 1.0, "amount": 1.0}
                   for t in F.KEEP_TIMES]).to_parquet(p, index=False)
    assert not F._ok_on_disk(p, "2026-01-02", "2026-09-10")


def test_ok_on_disk_rejects_too_few_bars_per_day(tmp_path):
    """区间齐但每日 bar 数太少(截断的半份文件)→ 判不可信。"""
    p = tmp_path / "y.parquet"
    rows = []
    for d in ("2026-01-02", "2026-09-10"):
        rows.append({"date": d, "time": "0935", "open": 1.0, "high": 1.0,
                      "low": 1.0, "close": 1.0, "volume": 1.0, "amount": 1.0})
    pd.DataFrame(rows).to_parquet(p, index=False)          # 每日只 1 根
    assert not F._ok_on_disk(p, "2026-01-02", "2026-09-10")


def test_ok_on_disk_accepts_complete(tmp_path):
    p = tmp_path / "z.parquet"
    rows = []
    for d in ("2026-01-02", "2026-05-06", "2026-09-10"):
        for t in F.KEEP_TIMES:
            rows.append({"date": d, "time": t, "open": 1.0, "high": 1.0,
                          "low": 1.0, "close": 1.0, "volume": 1.0, "amount": 1.0})
    pd.DataFrame(rows).to_parquet(p, index=False)
    assert F._ok_on_disk(p, "2026-01-02", "2026-09-10")


def test_retry_budget_is_positive():
    """重试上限必须 >1 —— baostock 并发瞬时失败靠重试救回(实测能救回相当比例)。"""
    assert F.MAX_ATTEMPTS > 1


# ── 坑④:单次约 2000 行上限 → 必须分段拉取 ──
#
# 实证:一次性请求 168 交易日时,658/3210 只票的交易日数精确聚集在 42/84/125
# (=168 的 1/4、1/2、3/4),起始日全对 → "从正确起点取到一部分就断"。
# 换算:请求的是整天 48 根,42 日 × 48 ≈ 2016 行 ≈ 压测见过的 n=2000 截断值。

def test_month_chunks_covers_range_without_gap():
    """分段必须无缝覆盖 [start, end]:段首尾相接、不漏日、不越界。"""
    segs = F._month_chunks("2026-01-02", "2026-09-10", months=2)
    assert segs[0][0] == "2026-01-02"
    assert segs[-1][1] == "2026-09-10"
    for (_, prev_end), (nxt_start, _) in zip(segs, segs[1:]):
        gap = pd.Timestamp(nxt_start) - pd.Timestamp(prev_end)
        assert gap == pd.Timedelta(days=1), f"段间有缝/重叠:{prev_end}→{nxt_start}"


def test_month_chunks_segment_stays_under_row_cap():
    """每段的交易日数须远低于 2000/48≈42 日上限,否则分段就没意义。"""
    segs = F._month_chunks("2026-01-02", "2026-09-10", months=2)
    for s, e in segs:
        cal_days = (pd.Timestamp(e) - pd.Timestamp(s)).days + 1
        # 2 个月自然日 ≈62 → 交易日 ≈40 → ×48 根 ≈1920 行 < 2000
        assert cal_days <= 62, f"段过长可能触发截断:{s}~{e} 共{cal_days}天"


def test_month_chunks_single_short_range():
    segs = F._month_chunks("2026-03-01", "2026-03-20", months=2)
    assert segs == [("2026-03-01", "2026-03-20")]


def test_ok_on_disk_tolerates_end_on_nontrading_day():
    """end 落在非交易日时,不能把正常票误判成不完整(容差 10 天)。"""
    import pathlib
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / "a.parquet"
        rows = []
        for d in ("2026-01-02", "2026-06-01", "2026-09-04"):   # 末日早 end 6 天
            for t in F.KEEP_TIMES:
                rows.append({"date": d, "time": t, "open": 1.0, "high": 1.0,
                              "low": 1.0, "close": 1.0, "volume": 1.0,
                              "amount": 1.0})
        pd.DataFrame(rows).to_parquet(path, index=False)
        assert F._ok_on_disk(path, "2026-01-02", "2026-09-10")


def test_ok_on_disk_tolerates_start_on_nontrading_day():
    """start 侧同样要容差 —— 回归锁。

    请求 start=2026-01-02(元旦后周五)时,首个有数据交易日常是 01-05(周一)。
    start 侧漏了容差会导致 **100% 的票被判需重取**(实测踩过,等于白跑 7 小时)。
    """
    import pathlib
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / "s.parquet"
        rows = []
        for d in ("2026-01-05", "2026-06-01", "2026-09-10"):   # 首日晚 start 3 天
            for t in F.KEEP_TIMES:
                rows.append({"date": d, "time": t, "open": 1.0, "high": 1.0,
                              "low": 1.0, "close": 1.0, "volume": 1.0,
                              "amount": 1.0})
        pd.DataFrame(rows).to_parquet(path, index=False)
        assert F._ok_on_disk(path, "2026-01-02", "2026-09-10"), \
            "start 侧容差缺失 → 正常票被误判为需重取"


def test_ok_on_disk_tolerates_few_short_days():
    """个别交易日 bar 不足(停牌半日)不该否定整票 —— 回归锁。

    实测:000002 有 166 天完整 7 根、仅末日 2 根,用 `.all()` 会被整票判废。
    改为要求 ≥95% 的交易日达标。
    """
    import pathlib
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / "p.parquet"
        rows = []
        days = [f"2026-{m:02d}-{d:02d}" for m in range(1, 10) for d in (1, 5, 10)]
        for i, d in enumerate(days):
            times = F.KEEP_TIMES if i < len(days) - 1 else F.KEEP_TIMES[:2]
            for t in times:
                rows.append({"date": d, "time": t, "open": 1.0, "high": 1.0,
                              "low": 1.0, "close": 1.0, "volume": 1.0,
                              "amount": 1.0})
        pd.DataFrame(rows).to_parquet(path, index=False)
        assert F._ok_on_disk(path, "2026-01-01", "2026-09-10"), \
            "单日停牌导致整票被判废(per_day 校验过严)"


def test_ok_on_disk_rejects_many_short_days():
    """但**大量**日 bar 不足仍要判不可信(那是真截断/坏数据)。"""
    import pathlib
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / "q.parquet"
        rows = []
        days = [f"2026-{m:02d}-{d:02d}" for m in range(1, 10) for d in (1, 5, 10)]
        for d in days:
            for t in F.KEEP_TIMES[:2]:            # 每天都只 2 根
                rows.append({"date": d, "time": t, "open": 1.0, "high": 1.0,
                              "low": 1.0, "close": 1.0, "volume": 1.0,
                              "amount": 1.0})
        pd.DataFrame(rows).to_parquet(path, index=False)
        assert not F._ok_on_disk(path, "2026-01-01", "2026-09-10")


def test_ok_on_disk_still_rejects_segment_truncation():
    """但 42/84/125 日那种整段尾部缺失(差几十天)必须仍被拦住。"""
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / "b.parquet"
        rows = []
        for d in ("2026-01-02", "2026-02-10", "2026-03-11"):   # 末日 = 坑④的 42 日档
            for t in F.KEEP_TIMES:
                rows.append({"date": d, "time": t, "open": 1.0, "high": 1.0,
                              "low": 1.0, "close": 1.0, "volume": 1.0,
                              "amount": 1.0})
        pd.DataFrame(rows).to_parquet(path, index=False)
        assert not F._ok_on_disk(path, "2026-01-02", "2026-09-10")



# ────────────────────────────── 防未来(核心红线) ──────────────────────────────

def _bars(rows):
    return pd.DataFrame(rows, columns=["date", "time", "open", "high",
                                        "low", "close", "volume", "amount"])


def test_cum_to_never_reads_future():
    """_cum_to(as_of) 只能累计 time<=as_of 的 bar —— 篡改之后的 bar 不改变结果。"""
    base = [
        ["2026-09-10", "0935", 10, 11, 9.5, 10.5, 100, 1000],
        ["2026-09-10", "1430", 10.5, 12, 10.4, 11.8, 200, 2000],
        ["2026-09-10", "1450", 11.8, 13, 11.7, 12.9, 300, 3000],
        ["2026-09-10", "1500", 12.9, 14, 12.8, 13.9, 400, 4000],
    ]
    real = M._cum_to(_bars(base), "2026-09-10", "1430")
    # 把 1450/1500 篡改成极端值(未来数据污染)
    tainted = [r[:] for r in base]
    tainted[2] = ["2026-09-10", "1450", 999, 9999, 1, 999, 99999, 999999]
    tainted[3] = ["2026-09-10", "1500", 999, 9999, 1, 999, 99999, 999999]
    assert M._cum_to(_bars(tainted), "2026-09-10", "1430") == real, "as_of 之后的 bar 泄漏进信号"


def test_cum_to_includes_asof_bar_itself():
    """as_of 那一根本身要算进去(<=,不是 <)。"""
    b = _bars([
        ["2026-09-10", "0935", 10, 11, 9, 10, 100, 1000],
        ["2026-09-10", "1430", 10, 12, 10, 11, 200, 2000],
    ])
    vol, amt, hi, lo = M._cum_to(b, "2026-09-10", "1430")
    assert vol == 300 and amt == 3000
    assert hi == 12 and lo == 9


def test_daily_ctx_excludes_today():
    """日线上下文只能用 T-1 及以前 —— 当日行绝不能进 MA/昨收。"""
    rows = [{"date": f"2026-08-{d:02d}", "close": 10.0, "volume": 100}
            for d in range(1, 26)]
    rows.append({"date": "2026-08-26", "close": 999.0, "volume": 99999})   # "当日"
    df = pd.DataFrame(rows)
    ctx = M._daily_ctx(df, "2026-08-26")
    assert ctx is not None
    assert ctx["prev_close"] == 10.0, "昨收读到了当日行"
    assert ctx["ma5"] == 10.0, "MA5 被当日行污染"


def test_daily_ctx_insufficient_history_returns_none():
    df = pd.DataFrame([{"date": f"2026-08-{d:02d}", "close": 10.0, "volume": 100}
                        for d in range(1, 10)])
    assert M._daily_ctx(df, "2026-08-20") is None


# ────────────────────────────── 成交可行性 ──────────────────────────────

def _daily_with_prev_close(pc: float, date: str = "2026-09-10"):
    return pd.DataFrame([{"date": "2026-09-09", "close": pc, "volume": 100}])


def test_detects_one_word_limit_up():
    """四价合一 + 涨幅达涨停线 → 一字板(实盘买不进卖不掉)。"""
    b = _bars([
        ["2026-09-10", "1430", 11.0, 11.0, 11.0, 11.0, 100, 1000],
        ["2026-09-10", "1450", 11.0, 11.0, 11.0, 11.0, 100, 1000],
        ["2026-09-10", "1500", 11.0, 11.0, 11.0, 11.0, 100, 1000],
    ])
    # 主板 10% 涨停:昨收 10 → 11.0 正好涨停
    got = M._is_one_word_limit(b, _daily_with_prev_close(10.0), "600001", "2026-09-10")
    assert got == "涨停一字"


def test_normal_limit_up_with_intraday_swing_is_tradeable():
    """涨停但**盘中有波动**(开过板)→ 不算一字板,可成交。"""
    b = _bars([
        ["2026-09-10", "1430", 10.5, 11.0, 10.3, 10.8, 100, 1000],
        ["2026-09-10", "1500", 10.9, 11.0, 10.8, 11.0, 100, 1000],
    ])
    assert M._is_one_word_limit(b, _daily_with_prev_close(10.0),
                                 "600001", "2026-09-10") is None


def test_detects_down_limit():
    """跌停 → 卖不掉。"""
    b = _bars([["2026-09-10", "1500", 9.0, 9.0, 9.0, 9.0, 100, 1000]])
    assert M._is_one_word_limit(b, _daily_with_prev_close(10.0),
                                 "600001", "2026-09-10") == "跌停"


def test_chinext_uses_20pct_limit():
    """创业板/科创板涨跌停是 20%,不能套主板 10%。"""
    b = _bars([["2026-09-10", "1500", 11.0, 11.0, 11.0, 11.0, 100, 1000]])
    # 昨收 10 → 11.0 = +10%,对创业板(20%)**不是**涨停
    assert M._is_one_word_limit(b, _daily_with_prev_close(10.0),
                                 "300001", "2026-09-10") is None
    b2 = _bars([["2026-09-10", "1500", 12.0, 12.0, 12.0, 12.0, 100, 1000]])
    assert M._is_one_word_limit(b2, _daily_with_prev_close(10.0),
                                 "300001", "2026-09-10") == "涨停一字"


def test_sell_defers_when_blocked_and_charges_extra_cost():
    """T+1 卖出日一字涨停 → 按评估口径 §八.2 顺延持有,hold_days 增加(成本按天累计)。"""
    dates = ["2026-09-10", "2026-09-11", "2026-09-14"]
    bars = {"600001": _bars([
        ["2026-09-10", "1450", 10.0, 10.1, 9.9, 10.0, 100, 1000],
        # T+1:一字涨停,卖不掉
        ["2026-09-11", "1450", 11.0, 11.0, 11.0, 11.0, 100, 1000],
        ["2026-09-11", "1500", 11.0, 11.0, 11.0, 11.0, 100, 1000],
        # T+2:正常,可卖
        ["2026-09-14", "1450", 11.5, 11.8, 11.2, 11.6, 100, 1000],
        ["2026-09-14", "1500", 11.6, 11.9, 11.3, 11.7, 100, 1000],
    ])}
    daily = {"600001": pd.DataFrame([
        {"date": "2026-09-09", "close": 10.0, "volume": 100},
        {"date": "2026-09-10", "close": 10.0, "volume": 100},
        {"date": "2026-09-11", "close": 11.0, "volume": 100},
    ])}
    got = M._sell_with_feasibility(bars, daily, "600001", "2026-09-10", dates)
    assert got is not None
    sell, hold_days, note = got
    assert hold_days == 2, "一字板那天应顺延,不该按收盘价成交"
    assert sell == 11.6, "应取顺延日的 14:50 价"
    assert "顺延" in note


def test_sell_normal_case_is_next_day_1450():
    dates = ["2026-09-10", "2026-09-11"]
    bars = {"600001": _bars([
        ["2026-09-10", "1450", 10.0, 10.1, 9.9, 10.0, 100, 1000],
        ["2026-09-11", "1450", 10.3, 10.5, 10.2, 10.4, 100, 1000],
        ["2026-09-11", "1500", 10.4, 10.6, 10.3, 10.5, 100, 1000],
    ])}
    daily = {"600001": pd.DataFrame([
        {"date": "2026-09-09", "close": 10.0, "volume": 100},
        {"date": "2026-09-10", "close": 10.0, "volume": 100},
    ])}
    sell, hold_days, _ = M._sell_with_feasibility(bars, daily, "600001",
                                                    "2026-09-10", dates)
    assert hold_days == 1 and sell == 10.4


# ────────────────────────────── 组合口径回撤(修 M3.a 的核心) ──────────────────────────────

def test_drawdown_is_portfolio_not_per_trade_cumsum():
    """同日多笔应先等权成"组合日收益",再算回撤 —— 不是逐笔累加。

    构造:两天,每天 2 笔。逐笔累加口径会把 4 笔串成序列得出更深的假回撤;
    组合口径下第 1 天 +10%/-10% 抵消为 0%,只有第 2 天的 -20% 是真回撤。
    """
    trades = [
        {"date": "2026-09-10", "ret": +0.10}, {"date": "2026-09-10", "ret": -0.10},
        {"date": "2026-09-11", "ret": -0.20}, {"date": "2026-09-11", "ret": -0.20},
    ]
    m = M.portfolio_metrics(trades)
    assert m["n"] == 4
    assert m["n_days"] == 2
    # 组合日收益 = [0.0, -0.20] → 净值 1.0 → 0.8,回撤 -20%
    assert m["max_dd"] == pytest.approx(-0.20, abs=1e-9)
    assert m["total_ret"] == pytest.approx(-0.20, abs=1e-9)


def test_drawdown_compounding_not_additive():
    """回撤按复利净值算(连续 -10% 两日 = -19%,不是 -20%)。"""
    trades = [{"date": "2026-09-10", "ret": -0.10},
              {"date": "2026-09-11", "ret": -0.10}]
    m = M.portfolio_metrics(trades)
    assert m["max_dd"] == pytest.approx(-0.19, abs=1e-9)


def test_max_consecutive_loss_days():
    trades = [{"date": "2026-09-0%d" % d, "ret": r} for d, r in
              zip(range(1, 7), [-0.01, -0.01, +0.02, -0.01, -0.01, -0.01])]
    assert M.portfolio_metrics(trades)["max_consec_loss"] == 3


def test_metrics_empty_is_safe():
    m = M.portfolio_metrics([])
    assert m["n"] == 0 and m["mean_ret"] is None


# ────────────────────────────── 裁决 / 显著性 ──────────────────────────────

def test_verdict_red_on_deep_drawdown():
    """评估口径 §五:回撤 ≤ -10% 直接红灯(哪怕均值为正)。"""
    m = {"n": 50, "mean_ret": 0.01, "max_dd": -0.15, "sharpe": 2.0}
    assert M.verdict(m) == "🔴红灯"


def test_verdict_red_on_nonpositive_mean():
    m = {"n": 50, "mean_ret": -0.001, "max_dd": -0.02, "sharpe": 1.0}
    assert M.verdict(m) == "🔴红灯"


def test_verdict_green_needs_all_three():
    m = {"n": 50, "mean_ret": 0.005, "max_dd": -0.05, "sharpe": 1.2}
    assert M.verdict(m) == "🟢绿灯"
    # 夏普不够 → 只能黄灯
    assert M.verdict({**m, "sharpe": 0.6}) == "🟡黄灯"


def test_bootstrap_ci_detects_insignificance():
    """均值虽正但波动极大 → CI 下界应 <0(不显著)。"""
    rets = [+0.50, -0.45, +0.48, -0.44, +0.46, -0.42, +0.02, -0.01, +0.03, -0.02]
    ci = M.bootstrap_ci(rets)
    assert ci is not None and ci[0] < 0


def test_bootstrap_ci_detects_significance():
    rets = [0.02] * 40 + [0.018] * 40
    ci = M.bootstrap_ci(rets)
    assert ci is not None and ci[0] > 0


def test_bootstrap_ci_small_sample_returns_none():
    assert M.bootstrap_ci([0.01, 0.02]) is None


# ────────────────────────────── Q3 诚实性 ──────────────────────────────

def test_q3_not_backtestable():
    """东财分时资金流只返当天 → Q3 不能假装能回测,不得出现在 STRATEGIES。"""
    assert "Q3" not in M.STRATEGIES
    assert set(M.STRATEGIES) == {"Q1", "Q2"}


# ────────── 采集截断防护:覆盖率门槛 + 从 bar 聚合日线(2026-09-17 加) ──────────
#
# 背景:全A 采集实测约 27% 的票末日聚集在 03-11/05-14/07-13(交易日 42/84/125,
# 完整票 168),抽查都是正常在市公司 → baostock 分段限流截断,不是退市。
# 这种票混进回测会让"同日截面"名不副实、等权基准被扭曲。

def _write_bar_file(dirpath, code: str, dates: list[str]):
    rows = []
    for d in dates:
        for t in M_KEEP:
            rows.append({"date": d, "time": t, "open": 10.0, "high": 10.5,
                          "low": 9.5, "close": 10.2, "volume": 100.0,
                          "amount": 1000.0})
    pd.DataFrame(rows).to_parquet(dirpath / f"{code}.parquet", index=False)


M_KEEP = ("0935", "1030", "1425", "1430", "1445", "1450", "1500")


def test_load_bars_drops_low_coverage(tmp_path, monkeypatch):
    """交易日数明显少于全样本最大值的票 → 剔除(截断残缺票)。"""
    monkeypatch.setattr(M, "BARS_DIR", tmp_path)
    full = [f"2026-0{m}-0{d}" for m in (1, 2, 3, 4, 5) for d in (1, 2)]   # 10 日
    _write_bar_file(tmp_path, "600001", full)
    _write_bar_file(tmp_path, "600002", full)
    _write_bar_file(tmp_path, "600003", full[:3])                          # 仅 3 日
    got = M.load_bars(min_coverage=0.8)
    assert set(got) == {"600001", "600002"}, "低覆盖票未被剔除"


def test_load_bars_drops_stale_tail(tmp_path, monkeypatch):
    """覆盖率够但**尾部整段缺失**(限流截断的典型形态)→ 也要剔除。"""
    monkeypatch.setattr(M, "BARS_DIR", tmp_path)
    late = [f"2026-06-{d:02d}" for d in range(1, 21)]
    early = [f"2026-01-{d:02d}" for d in range(1, 20)]     # 19 日,覆盖率够但全在早期
    _write_bar_file(tmp_path, "600001", late)
    _write_bar_file(tmp_path, "600002", late)
    _write_bar_file(tmp_path, "600003", early)
    got = M.load_bars(min_coverage=0.8, require_end_within=3)
    assert "600003" not in got, "尾部整段缺失的票未被剔除"
    assert set(got) == {"600001", "600002"}


def test_load_bars_keeps_all_when_uniform(tmp_path, monkeypatch):
    """都完整时不该误杀。"""
    monkeypatch.setattr(M, "BARS_DIR", tmp_path)
    ds = [f"2026-03-{d:02d}" for d in range(1, 11)]
    for c in ("600001", "600002", "600003"):
        _write_bar_file(tmp_path, c, ds)
    assert len(M.load_bars(min_coverage=0.8)) == 3


def test_daily_from_bars_is_self_sufficient(tmp_path, monkeypatch):
    """全A 主档只有 126 只 → 日线必须能从分时自足聚合,否则 5000+ 只被静默跳过。"""
    monkeypatch.setattr(M, "BARS_DIR", tmp_path)
    ds = [f"2026-03-{d:02d}" for d in range(1, 26)]
    _write_bar_file(tmp_path, "600001", ds)
    bars = M.load_bars(min_coverage=0.5)
    daily = M.daily_from_bars(bars)
    assert set(daily) == set(bars), "聚合日线的票集应与分时一致"
    df = daily["600001"]
    assert list(df.columns) == ["date", "open", "high", "low", "close",
                                "volume", "amount"]
    assert len(df) == len(ds), "每个交易日应聚合成一行"
    # open 取当日最早时刻、close 取最晚时刻
    assert df["open"].iloc[0] == 10.0 and df["close"].iloc[0] == 10.2
    # volume/amount 是采样点求和(7 个时刻)
    assert df["volume"].iloc[0] == 100.0 * len(M_KEEP)


def test_daily_from_bars_feeds_daily_ctx(tmp_path, monkeypatch):
    """端到端:聚合出的日线要能喂 _daily_ctx(否则策略全被跳过)。"""
    monkeypatch.setattr(M, "BARS_DIR", tmp_path)
    ds = [f"2026-03-{d:02d}" for d in range(1, 26)]        # 25 日 ≥20,够算 MA20
    _write_bar_file(tmp_path, "600001", ds)
    daily = M.daily_from_bars(M.load_bars(min_coverage=0.5))
    ctx = M._daily_ctx(daily["600001"], ds[-1])
    assert ctx is not None, "聚合日线喂不动 _daily_ctx → 全A 回测会 0 笔"
    assert ctx["prev_close"] == 10.2
    assert ctx["vol_ma20"] > 0
