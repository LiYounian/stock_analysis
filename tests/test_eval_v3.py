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


def test_hit_base_is_close_t_not_entry_end():
    """双基准语义锁死:命中基准=close[T],收益基准=T+1 入场价,二者不串。

    构造跳空低开:T 收盘 100,T+1 开盘 90 才买得到 → 买得便宜收益为正,但价格从未站回 100
    → 相对预测日收盘方向错。命中必须判 0(base=close[T]),收益必须为正(base=entry)。
    """
    book = _book({"X": [
        ("d0", 100, 100, 100, 100.0),   # 预测日 T:命中基准 close[T]=100
        ("d1", 90, 95, 88, 92.0),       # T+1 open=90 入场(收益基准);盘中最高 95<100 未触及
        ("d2", 92, 98, 90, 95.0),       # T+2 close=95<100 期末不命中;窗口最高 98<100 未触及
    ]})
    fr = prices.realized(book.get("X"), 0, +1, (2,))
    # 收益基准=T+1 入场 90:r = 95/90−1 > 0(低开买入确实赚)
    assert fr[2]["r"] > 0
    assert abs(fr[2]["r"] - (95.0 / 90.0 - 1) * 100) < 1e-9
    # 命中基准=close[T]=100:close[T+2]=95<100 → 期末不命中(即便收益为正,预测方向错)
    assert fr[2]["hit_end"] == 0
    # 期内触及基准=close[T]=100:窗口 [T+1,T+2] 最高 98 从未 >100 → 未触及
    assert fr[2]["hit_intra"] == 0
    # 隔夜跳空 = entry/close[T]−1 = 90/100−1 = −10%(单列)
    assert abs(fr[2]["gap"] - (-10.0)) < 1e-9


def test_intra_touch_base_is_close_t():
    """期内触及严格按'T 之后任意一天 high>close[T]':低开后盘中冲破 close[T] 即算触及,即便期末又跌回。"""
    book = _book({"X": [
        ("d0", 100, 100, 100, 100.0),
        ("d1", 90, 101, 88, 92.0),      # 盘中 high=101 > close[T]=100 → 触及
        ("d2", 92, 99, 90, 95.0),       # 期末 close=95<100 → 期末不命中
    ]})
    fr = prices.realized(book.get("X"), 0, +1, (2,))
    assert fr[2]["hit_intra"] == 1 and fr[2]["hit_end"] == 0


def test_short_hit_base_close_t():
    """看空:期末命中=close[T+h]<close[T];期内触及=窗口最低价<close[T]。基准均为 close[T]。"""
    book = _book({"X": [
        ("d0", 100, 100, 100, 100.0),
        ("d1", 105, 106, 99, 101.0),    # 盘中最低 99<100 → 看空触及;入场 open=105
        ("d2", 101, 103, 97, 98.0),     # 期末 close=98<100 → 看空期末命中
    ]})
    fr = prices.realized(book.get("X"), 0, -1, (2,))
    assert fr[2]["hit_end"] == 1 and fr[2]["hit_intra"] == 1
    # 收益基准仍是 T+1 入场 105:高开做空,r = 98/105−1 < 0(方向命中但按多头口径收益为负,只作收益列)
    assert abs(fr[2]["r"] - (98.0 / 105.0 - 1) * 100) < 1e-9


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
    """策略0/9 标不可回放;S01/S02/S03/S04 可回放且广筛型。"""
    assert schema.meta_for("策略0合议").replayable is False
    assert schema.meta_for("最强选股").replayable is False
    assert schema.meta_for("放量后缩量回踩").replayable is True
    assert schema.meta_for("放量后缩量回踩").stype == schema.DIRECTIONAL


def test_schema_type_streaming_classification():
    """三分流归类锁死:可排序型=0/4/5/10/SEPA;广筛型=S01/S02/箱体/S03/S04/最强/形态;
    策略11 重归『参考·非alpha』(伪排序),**绝不再是排序型**。"""
    # 可排序型(有连续打分)
    for stem in ("策略0合议", "动量组合", "半导体多因子", "反转低换手组合", "SEPA合格池"):
        assert schema.meta_for(stem).stype == schema.RANKABLE, stem
    # 广筛型(布尔达标全上)
    for stem in ("趋势深跌反包", "放量后缩量回踩", "箱体形态", "最大范围选股", "量价放量",
                 "最强选股", "形态选股"):
        assert schema.meta_for(stem).stype == schema.DIRECTIONAL, stem
    # 策略11:参考·非alpha,既非排序型也不当广筛型 alpha 判据
    assert schema.meta_for("指标条件化状态排序").stype == schema.REFERENCE
    assert schema.meta_for("指标条件化状态排序").stype != schema.RANKABLE


def _monotonic_price_map():
    """4 票,d2 收益随字母(A<B<C<D)单调递增;供 rank-IC/Top-N 单调性测试。"""
    return {"A": [("d0", 1, 1, 1, 1.0), ("d1", 1, 1, 1, 1.0), ("d2", 1, 1.1, 1, 1.1)],
            "B": [("d0", 1, 1, 1, 1.0), ("d1", 1, 1, 1, 1.0), ("d2", 1, 1.2, 1, 1.2)],
            "C": [("d0", 1, 1, 1, 1.0), ("d1", 1, 1, 1, 1.0), ("d2", 1, 1.3, 1, 1.3)],
            "D": [("d0", 1, 1, 1, 1.0), ("d1", 1, 1, 1, 1.0), ("d2", 1, 1.4, 1, 1.4)]}


def test_rankable_strategy_has_full_metrics_topn_and_rank_ic():
    """可排序型(策略10):同一单元同时有 ①全量指标(命中率/收益质量)②Top-N精度 ③rank-IC。
    且分数与未来收益单调正相关时 rank-IC>0 且 Top-N 收益随档位收窄而更高(选择性)。"""
    preds = schema.make_frame([
        {"strategy_id": "10", "strategy": "反转低换手", "pred_date": "d0", "code": c,
         "direction": 1, "rank_score": s, "source": "live",
         "stype": schema.RANKABLE, "replayable": True}
        for c, s in [("A", 1.0), ("B", 2.0), ("C", 3.0), ("D", 4.0)]
    ])
    book = _book(_monotonic_price_map())
    scored = scoring.score_predictions(preds, book, (2,))
    agg = aggregate.aggregate(scored, {}, ["d0", "d1", "d2"], horizons=(2,), track="live")
    cell = agg["窗口"]["全史"]["策略"]["10"]["2日"]
    # ① 全量指标在(可排序型也评全部票等权)
    assert "命中率%_期末" in cell and "收益质量" in cell
    # ③ rank-IC 嵌套且正
    assert cell["rank_ic"]["mean_ic"] > 0.9
    # ② Top-N 精度:Top2 期望收益(池化 C,D=1.3,1.4→均值≈35%)> 全4只均值≈25%
    topn = cell["Top-N精度"]
    assert 2 not in topn  # 档位固定 5/10/20;当日仅4票→Top5=全部
    top5 = topn[5]
    assert top5["选中样本"] == 4 and top5["每日不足N占比%"] == 100.0
    assert abs(top5["期望收益%_池化"] - 25.0) < 1e-6


def test_topn_selectivity_top5_beats_all():
    """两日、每日 8 票、分数越高未来收益越高:Top5 池化期望收益应高于全 16 票均值(排序有用)。"""
    rows, price_map = [], {}
    for day in ("d0", "d1"):
        for i in range(8):        # 分数 i,未来收益 = i%(d2 相对入场)
            code = f"{day}_{i}"
            rows.append({"strategy_id": "0", "strategy": "合议", "pred_date": day,
                         "code": code, "direction": 1, "rank_score": float(i),
                         "source": "live", "stype": schema.RANKABLE, "replayable": True})
            close2 = 1.0 * (1 + i / 100.0)
            price_map[code] = [(day, 1, 1, 1, 1.0),
                               ("d_mid", 1, 1, 1, 1.0),
                               ("d_end", 1, max(1.0, close2), 1, close2)]
    # 需要 d0/d1 各自 idx 后有 +2 根;用统一日期轴(每票自带三根即可,pred_date=day 在 idx0)
    preds = schema.make_frame(rows)
    book = _book(price_map)
    scored = scoring.score_predictions(preds, book, (2,))
    agg = aggregate.aggregate(scored, {}, ["d0", "d1", "d_mid", "d_end"],
                              horizons=(2,), track="live")
    cell = agg["窗口"]["全史"]["策略"]["0"]["2日"]
    topn = cell["Top-N精度"]
    all_mean = cell["收益质量"]["均值%"]          # 全 16 票池化均值
    top5_mean = topn[5]["期望收益%_池化"]          # 每日取分数最高 5 只
    assert top5_mean > all_mean                    # Top5 更赚 → 分数有选择性
    assert topn[5]["选中样本"] == 10               # 两日各 5 只
    assert topn[5]["预测日数"] == 2


def test_reference_strategy_11_no_rank_ic_no_topn():
    """策略11 重归『参考·非alpha』:走广筛全量指标(有命中率/收益质量),
    **绝不出 rank_ic / Top-N精度**(纠正 v3 旧版把它当排序型 rank-IC 的误导)。"""
    preds = schema.make_frame([
        {"strategy_id": "11", "strategy": "指标条件化", "pred_date": "d0", "code": c,
         "direction": 1, "rank_score": s, "source": "live",
         "stype": schema.REFERENCE, "replayable": True}
        for c, s in [("A", 1.0), ("B", 2.0), ("C", 3.0), ("D", 4.0)]
    ])
    book = _book(_monotonic_price_map())
    scored = scoring.score_predictions(preds, book, (2,))
    agg = aggregate.aggregate(scored, {}, ["d0", "d1", "d2"], horizons=(2,), track="live")
    cell = agg["窗口"]["全史"]["策略"]["11"]["2日"]
    assert "命中率%_期末" in cell and "收益质量" in cell   # 广筛口径全量指标在
    assert "rank_ic" not in cell                          # 不跑 rank-IC
    assert "Top-N精度" not in cell                         # 不跑 Top-N
    assert "参考收益分布_全部已到期" in cell               # 附纯参考收益分布


def test_reference_neutral_direction_still_has_return_dist():
    """策略11 方向中性时:命中率天然不适用(directional_cell 过滤掉),
    但『参考收益分布(全部已到期)』仍应有样本 → 参考行不至全空。"""
    preds = schema.make_frame([
        {"strategy_id": "11", "strategy": "指标条件化", "pred_date": "d0", "code": c,
         "direction": 0, "rank_score": s, "source": "live",
         "stype": schema.REFERENCE, "replayable": True}
        for c, s in [("A", 1.0), ("B", 2.0), ("C", 3.0), ("D", 4.0)]
    ])
    book = _book(_monotonic_price_map())
    scored = scoring.score_predictions(preds, book, (2,))
    agg = aggregate.aggregate(scored, {}, ["d0", "d1", "d2"], horizons=(2,), track="live")
    cell = agg["窗口"]["全史"]["策略"]["11"]["2日"]
    assert cell["命中率%_期末"] is None            # 中性方向命中不适用
    assert cell["参考已到期样本"] == 4              # 但收益分布覆盖全部已到期票
    assert cell["参考收益分布_全部已到期"]["n"] == 4
    assert "rank_ic" not in cell and "Top-N精度" not in cell


def test_directional_strategy_no_topn_no_rank_ic():
    """广筛型(S02)只走全量等权指标,不产 Top-N / rank-IC。"""
    preds = schema.make_frame([
        {"strategy_id": "S02", "strategy": "放量回踩", "pred_date": "d0", "code": c,
         "direction": 1, "rank_score": float("nan"), "source": "replay",
         "stype": schema.DIRECTIONAL, "replayable": True}
        for c in ("A", "B", "C", "D")
    ])
    book = _book(_monotonic_price_map())
    scored = scoring.score_predictions(preds, book, (2,))
    agg = aggregate.aggregate(scored, {}, ["d0", "d1", "d2"], horizons=(2,), track="replay")
    cell = agg["窗口"]["全史"]["策略"]["S02"]["2日"]
    assert "命中率%_期末" in cell
    assert "rank_ic" not in cell and "Top-N精度" not in cell


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
