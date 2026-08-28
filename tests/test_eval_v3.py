"""锁 eval_v3 新口径语义(防未来 prompt/代码重写时无意删掉规则)。

覆盖:① T+1 入场口径(入场=open[T+1]、退出=close[T+h]、隔夜跳空单列剔除)②双口径命中方向感知
③收益质量(盈亏比/胜率)④超额+随机bootstrap ⑤按日聚类 CI/p(vs naive Wilson)⑥rank-IC/ICIR
以及双轨 source 区分、不可回放策略标注、schema 契约。合成数据,离线可跑。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.backtest.eval_v3 import (aggregate, live_source, prices, schema,
                                    scoring, stats)


def _book(prices_by_code):
    def loader(code):
        return pd.DataFrame(prices_by_code[code],
                            columns=["date", "open", "high", "low", "close"])
    return prices.PriceBook(loader=loader)


# ─────────────── ② T+1 入场口径 ───────────────
def test_entry_is_t_plus_1_open_not_close_t():
    """入场价=open[T+1](非 close[T]);r_h=close[T+h]/入场价−1;隔夜跳空=入场价/close[T]−1 单列。"""
    book = _book({"X": [
        ("d0", 10, 10, 10, 10.0),    # 信号日 T:close=10
        ("d1", 11, 12, 10.5, 11.5),  # T+1:open=11(入场价),close=11.5
        ("d2", 11.5, 13, 11, 12.0),  # T+2:close=12
    ]})
    rec = book.get("X")
    fr = prices.realized(rec, 0, +1, (1, 2))
    # h=1:close[T+1]/open[T+1]−1 = 11.5/11−1 = 4.545%
    assert abs(fr[1]["r"] - (11.5 / 11 - 1) * 100) < 1e-6
    # h=2:close[T+2]/open[T+1]−1 = 12/11−1
    assert abs(fr[2]["r"] - (12 / 11 - 1) * 100) < 1e-6
    # 隔夜跳空 = open[T+1]/close[T]−1 = 11/10−1 = 10%(不进 r)
    assert abs(fr[1]["gap"] - 10.0) < 1e-6
    assert fr[1]["entry_fallback"] is False


def test_entry_fallback_to_close_when_no_open():
    """open 缺失/为 0 → 用 close[T+1] 当入场价,并标 entry_fallback=True。"""
    book = _book({"X": [
        ("d0", 10, 10, 10, 10.0),
        ("d1", 0, 12, 10, 11.0),     # open=0 → 退化用 close[T+1]=11
        ("d2", 11, 13, 11, 12.0),
    ]})
    fr = prices.realized(book.get("X"), 0, +1, (1,))
    assert fr[1]["entry_fallback"] is True
    assert abs(fr[1]["r"] - (11.0 / 11.0 - 1) * 100) < 1e-9   # close/close


def test_no_future_cannot_enter_last_bar():
    """信号日=最后一根 → 无法 T+1 入场 → 全 pending(不用未来价)。"""
    book = _book({"X": [("d0", 10, 10, 10, 10.0), ("d1", 11, 11, 11, 11.0)]})
    fr = prices.realized(book.get("X"), 1, +1, (1, 5))   # idx=1 是最后一根
    assert fr[1]["matured"] is False and fr[1]["r"] is None


# ─────────────── 双口径命中(方向感知,T+1 入场) ───────────────
def test_dual_caliber_touch_vs_end_t1_entry():
    """看多:入场后触到更高(期内命中)但期末收在入场下(期末不命中)。"""
    book = _book({"X": [
        ("d0", 100, 100, 100, 100.0),
        ("d1", 100, 108, 99, 101.0),   # 入场 open=100;盘中 108>100 触及
        ("d2", 101, 102, 98, 99.0),
        ("d3", 99, 100, 97, 98.0),
        ("d4", 98, 99, 96, 97.0),
        ("d5", 97, 98, 95, 96.0),      # T+5 close=96<入场100
    ]})
    fr = prices.realized(book.get("X"), 0, +1, (5,))
    assert fr[5]["hit_end"] == 0 and fr[5]["hit_intra"] == 1


def test_neutral_direction_no_hit():
    book = _book({"X": [("d0", 100, 100, 100, 100.0), ("d1", 100, 100, 90, 95.0)]})
    fr = prices.realized(book.get("X"), 0, 0, (1,))
    assert fr[1]["matured"] is True and fr[1]["hit_end"] is None and fr[1]["hit_intra"] is None


# ─────────────── ③ 收益质量 ───────────────
def test_return_quality_profit_factor_winrate():
    q = aggregate.return_quality(np.array([2.0, 4.0, -1.0, -3.0]))
    assert q["胜率%"] == 50.0
    assert abs(q["盈亏比"] - (3.0 / 2.0)) < 1e-9    # 均盈3 / |均亏2|
    assert q["均值%"] == 0.5


def test_return_quality_no_losers_pf_none():
    q = aggregate.return_quality(np.array([1.0, 2.0]))
    assert q["盈亏比"] is None and q["胜率%"] == 100.0


# ─────────────── ⑤ 按日聚类显著性 vs naive Wilson ───────────────
def test_cluster_bootstrap_ci_uses_days_not_ticks():
    """同一天 100 票全命中 vs 另一天全不命中:聚类单元=2 天,CI 应很宽(不因 200 票而变窄)。"""
    day1 = np.ones(100)      # 全命中
    day2 = np.zeros(100)     # 全不命中
    ci = stats.cluster_bootstrap_ci([day1, day2], "mean", B=1000, seed=1)
    assert ci["n_days"] == 2 and ci["n_obs"] == 200
    # 两天重采样 → 均值可为 0 / 0.5 / 1,区间必跨 [0,1] 大部分,远宽于 naive
    assert (ci["hi"] - ci["lo"]) > 0.4


def test_wilson_narrower_than_cluster_when_clustered():
    """naive Wilson 用逐票 n=200 → 区间偏窄(高估独立性),证明二者口径不同。"""
    lo, hi = stats.wilson_ci(100, 200)   # 50% of 200
    assert (hi - lo) < 15   # naive 很窄


def test_cluster_excess_p_value():
    """每日超额恒为 +1(策略每日都比市场高 1%)→ 聚类超额显著为正,p 小。"""
    strat_day = [np.array([2.0, 2.0]), np.array([3.0]), np.array([1.0, 1.0, 1.0])]
    mkt_day = [1.0, 2.0, 0.0]   # 每日策略均 − 市场 = 1
    ex = stats.cluster_bootstrap_excess(strat_day, mkt_day, B=1000, seed=1)
    assert abs(ex["excess"] - 1.0) < 1e-9
    assert ex["lo"] > 0 and ex["p_value"] < 0.2


def test_random_pick_bootstrap_beats_random():
    """策略每日均收益远高于全市场 → 优于随机 p 应很小。"""
    strat_means = [5.0, 5.0, 5.0]
    day_uni = [np.array([0.0, 0.1, -0.1, 0.05])] * 3
    sizes = [2, 2, 2]
    rp = stats.bootstrap_random_pick(strat_means, day_uni, sizes, B=1000, seed=1)
    assert rp["p_value"] < 0.01 and rp["strat_mean"] == 5.0


# ─────────────── ⑥ rank-IC ───────────────
def test_rank_ic_positive_monotonic():
    """rank_score 与未来收益单调正相关 → 每日 IC≈1,mean-IC>0。"""
    pairs = [(np.array([1.0, 2, 3, 4]), np.array([0.5, 1.0, 1.5, 2.0])),
             (np.array([4.0, 3, 2, 1]), np.array([-2.0, -1, 0, 1]))]  # 第二天负相关
    r = stats.rank_ic(pairs)
    # 第一天 IC=+1,第二天 IC=−1 → mean≈0;换全正:
    pairs2 = [(np.array([1.0, 2, 3, 4]), np.array([0.1, 0.2, 0.3, 0.4])),
              (np.array([1.0, 2, 3, 4]), np.array([1.0, 2.0, 3.0, 4.0]))]
    r2 = stats.rank_ic(pairs2)
    assert r2["mean_ic"] > 0.9 and r2["n_days"] == 2
    assert r["n_days"] == 2


def test_rank_ic_skips_degenerate_days():
    """截面分数无方差的日跳过(避免 NaN 污染)。"""
    pairs = [(np.array([1.0, 1.0, 1.0]), np.array([1.0, 2.0, 3.0])),   # 分数常数→跳过
             (np.array([1.0, 2.0, 3.0]), np.array([1.0, 2.0, 3.0]))]
    r = stats.rank_ic(pairs)
    assert r["n_days"] == 1


# ─────────────── 双轨 source 区分 + schema ───────────────
def test_schema_meta_non_replayable_flags():
    """策略0/9 标不可回放;S01/S02/S03/S04 可回放且方向型。"""
    assert schema.meta_for("策略0合议").replayable is False
    assert schema.meta_for("最强选股").replayable is False
    assert schema.meta_for("放量后缩量回踩").replayable is True
    assert schema.meta_for("指标条件化状态排序").stype == schema.RANKING


def test_ranking_strategy_uses_rank_ic_not_hit():
    """排序型策略在聚合里走 ranking_cell(出 mean_ic),不出方向命中率。"""
    preds = schema.make_frame([
        {"strategy_id": "11", "strategy": "指标", "pred_date": "d0", "code": c,
         "direction": 0, "rank_score": s, "source": "live",
         "stype": schema.RANKING, "replayable": True}
        for c, s in [("A", 1.0), ("B", 2.0), ("C", 3.0), ("D", 4.0)]
    ])
    price_map = {"A": [("d0", 1, 1, 1, 1.0), ("d1", 1, 1, 1, 1.0), ("d2", 1, 1, 1, 1.1)],
                 "B": [("d0", 1, 1, 1, 1.0), ("d1", 1, 1, 1, 1.0), ("d2", 1, 1, 1, 1.2)],
                 "C": [("d0", 1, 1, 1, 1.0), ("d1", 1, 1, 1, 1.0), ("d2", 1, 1, 1, 1.3)],
                 "D": [("d0", 1, 1, 1, 1.0), ("d1", 1, 1, 1, 1.0), ("d2", 1, 1, 1, 1.4)]}
    book = _book(price_map)
    scored = scoring.score_predictions(preds, book, (2,))
    cal = ["d0", "d1", "d2"]
    agg = aggregate.aggregate(scored, {}, cal, horizons=(2,), track="live")
    cell = agg["窗口"]["全史"]["策略"]["11"]["2日"]
    assert "mean_ic" in cell and "命中率%_期末" not in cell
    assert cell["mean_ic"] > 0.9   # 分数越高未来收益越高


def test_live_source_source_tag_and_direction():
    """live_source 抽出的记录 source=live;综合方向文案 → ±1;缺省多头 → +1。"""
    recs = live_source.extract_records(
        {"top": [{"code": "A", "综合方向": "看多", "综合分": 0.6},
                 {"code": "B", "综合方向": "看空", "综合分": 0.2}]},
        schema.meta_for("策略0合议"), "2026-08-28")
    by = {r["code"]: r for r in recs}
    assert all(r["source"] == "live" for r in recs)
    assert by["A"]["direction"] == 1 and by["B"]["direction"] == -1
    assert by["A"]["rank_score"] == 0.6


# ─────────────── Student-t p 值自包含实现 ───────────────
def test_t_two_sided_p_sanity():
    """t=0 → p=1;大 t → p→0;与已知值量级一致。"""
    assert abs(stats.t_two_sided_p(0.0, 10) - 1.0) < 1e-9
    assert stats.t_two_sided_p(10.0, 10) < 0.001
    # df=10, t=2.228 ≈ 双边 0.05
    assert abs(stats.t_two_sided_p(2.228, 10) - 0.05) < 0.01
