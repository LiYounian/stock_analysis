"""午盘选股复盘(intraday_review)单测。

锁语义(为什么这么写,防未来重写误删规则):
  ① 下午涨跌% = (当日收盘 − 11:30 冻结价)/11:30 冻结价;防未来:收盘只取 date==as_of 的当日行,
     主档若含 as_of 之后的行**绝不使用**(不引入未来)。
  ② 下午等权基准复用唯一真源 breadth.equal_weight_mean_pct(不自算);取样率不足 → degraded。
  ③ α = 个股下午涨跌% − 全A下午等权基准;买入组是主评价、规避组是纠偏参照。
  ④ 切分复用 intraday_screen.split_buy_avoid(买入5/规避3 单一真源,不在复盘侧重定义)。
  ⑤ 端到端:产 复盘/午盘_<date>.md 两组记分表(α 列名兼容),inbox 追加一行;全程可注入 tmp、不碰生产。
"""
import json

import pandas as pd
import pytest

from tools.analysis.market_forecast import breadth as B
from tools.pipeline import intraday_review as rv
from tools.pipeline import intraday_screen as isr


def _kline(as_of_close: float | None, *, as_of="2026-09-08", with_future=False, as_of_open=None):
    """构造单票主档 K 线:含 as_of 当日行(收盘=as_of_close);with_future 时额外塞一根未来行(脏档)。

    as_of_open:当日开盘价(P0-1 降级口径用);默认回落 10.0。
    """
    rows = [{"date": pd.Timestamp("2026-09-07"), "open": 10.0, "close": 10.0},
            {"date": pd.Timestamp(as_of), "open": (10.0 if as_of_open is None else as_of_open),
             "close": as_of_close}]
    if with_future:
        rows.append({"date": pd.Timestamp("2026-09-09"), "open": 999.0, "close": 999.0})  # 未来行,绝不能被用
    return pd.DataFrame(rows)


# ————————————————————————————————————————————————
# ① 下午涨跌% 数学 + 防未来
# ————————————————————————————————————————————————
def test_afternoon_pct_math():
    assert rv.afternoon_pct(10.0, 11.0) == pytest.approx(10.0)     # +10%
    assert rv.afternoon_pct(10.0, 9.5) == pytest.approx(-5.0)
    assert rv.afternoon_pct(None, 11.0) is None                    # 缺冻结价
    assert rv.afternoon_pct(0.0, 11.0) is None                     # 冻结价≤0 不除


def test_close_of_takes_as_of_row_not_future():
    """防未来:主档含 as_of 之后的脏行时,close_of 只取 as_of 当日行,绝不取未来 999。"""
    lk = lambda code: _kline(11.0, with_future=True)
    assert rv.close_of("X", "2026-09-08", load_kline_fn=lk) == 11.0
    # as_of 行缺失 → None(不瞎补、不回落到相邻日)
    lk_missing = lambda code: _kline(None).iloc[[0]]               # 只有 09-07 行
    assert rv.close_of("X", "2026-09-08", load_kline_fn=lk_missing) is None


# ————————————————————————————————————————————————
# ② 下午等权基准:复用唯一真源 + 取样率降级
# ————————————————————————————————————————————————
def test_afternoon_benchmark_reuses_equal_weight():
    snapshot = {"A": 10.0, "B": 10.0, "C": 10.0}
    # A +10%, B 0%, C -4% → 等权 = 2.0
    closes = {"A": 11.0, "B": 10.0, "C": 9.6}
    lk = lambda code: _kline(closes[code])
    bench = rv.afternoon_benchmark(snapshot, "2026-09-08", load_kline_fn=lk)
    assert bench["样本"] == 3 and bench["全A"] == 3
    assert bench["下午等权基准"] == pytest.approx(
        B.equal_weight_mean_pct([10.0, 0.0, -4.0], total=3))
    assert bench["下午等权基准"] == pytest.approx(2.0)
    assert bench["degraded"] is False


def test_afternoon_benchmark_degrades_on_low_coverage():
    snapshot = {c: 10.0 for c in "ABCDE"}                          # 5 只,只有 1 只有收盘
    lk = lambda code: _kline(11.0 if code == "A" else None)
    bench = rv.afternoon_benchmark(snapshot, "2026-09-08", load_kline_fn=lk,
                                   min_coverage=0.6)
    assert bench["样本"] == 1 and bench["取样率"] == pytest.approx(0.2)
    assert bench["degraded"] is True                              # 取样率 20% < 60%


# ————————————————————————————————————————————————
# ③ 逐票记分 + ④ 组小结
# ————————————————————————————————————————————————
def _cand(code, score, direction):
    return {"code": code, "name": code, "完整分": score, "消息面方向": direction}


def test_score_group_alpha():
    snapshot = {"A": 10.0, "B": 10.0}
    lk = lambda code: _kline({"A": 11.0, "B": 9.5}[code])
    rows = rv.score_group([_cand("A", 5, "看多"), _cand("B", 4, "看多")],
                          snapshot, "2026-09-08", benchmark=2.0, group="买入", load_kline_fn=lk)
    a, b = rows
    assert a["下午涨跌%"] == pytest.approx(10.0) and a["下午α"] == pytest.approx(8.0)
    assert b["下午涨跌%"] == pytest.approx(-5.0) and b["下午α"] == pytest.approx(-7.0)
    assert "正 α" in a["判定"] and "负 α" in b["判定"]


def test_summarize_hit_and_separation():
    买入 = [{"下午α": 3.0}, {"下午α": -1.0}, {"下午α": 2.0}]        # 2/3 正 → 命中 0.6667
    规避 = [{"下午α": -2.0}, {"下午α": 1.0}]                        # 1/2 负 → 命中 0.5
    s = rv.summarize(买入, 规避)
    assert s["买入均值α"] == pytest.approx(4.0 / 3, abs=1e-3)      # 记分小结按 4 位取整
    assert s["买入命中率"] == pytest.approx(2 / 3, abs=1e-3)
    assert s["规避命中率"] == pytest.approx(0.5)
    assert s["分离度"] == pytest.approx((4.0 / 3) - (-0.5), abs=1e-3)  # 买入均值 − 规避均值


# ————————————————————————————————————————————————
# ⑤ 端到端:两组记分表 + α 列名兼容 + inbox 追加,全程 tmp、不碰生产
# ————————————————————————————————————————————————
def test_run_intraday_review_e2e(tmp_path):
    as_of = "2026-09-08"
    # 快照:买入候选 P0..P6(看多)+ 看空 S1;写到 tmp(复用 isr 落盘,单一真源路径)。
    # 池 8 只 → 买入 5(P0..P4)、规避 3(看空 S1 + 补最低分 P6,P5)。
    quotes = {f"P{i}": {"price": 10.0} for i in range(7)}
    quotes["S1"] = {"price": 10.0}
    isr.persist_noon_snapshot(quotes, as_of, out_root=tmp_path)

    # view:重排(完整分降序);P0 涨、其余按序;S1 看空
    reranked = ([_cand(f"P{i}", 8.0 - i, "看多") for i in range(7)]
                + [_cand("S1", 1.0, "看空")])
    view = {"重排": reranked}

    # 收盘:P0 +10%、P1 +2%、其余 0；S1 -3%
    closes = {**{f"P{i}": 10.0 for i in range(7)}, "P0": 11.0, "P1": 10.2, "S1": 9.7}
    lk = lambda code: _kline(closes[code])

    inbox = tmp_path / "_待并入.md"
    inbox.write_text("| 投递日 | 待并入 | 证据 | 投递者 |\n|---|---|---|---|\n", encoding="utf-8")

    rep = rv.run_intraday_review(as_of, snapshot_root=tmp_path, out_dir=tmp_path,
                                 load_kline_fn=lk, view_fn=lambda: view, inbox=inbox)
    # 切分:买入=前5非看空(P0..P4),规避=看空优先(S1)+补最低分(P5)
    assert rep["买入"] == 5 and rep["规避"] == 3
    text = (tmp_path / "午盘_2026-09-08.md").read_text(encoding="utf-8")
    assert "买入组 · 下午 α 记分(主评价对象)" in text
    assert "规避组 · 下午 α 记分(纠偏参照)" in text
    assert "α vs 全A等权" in text                                  # 列名兼容 web _alpha_col
    assert "P0" in text and "S1" in text
    assert "下午涨跌%" in text and "分离度" in text
    # 不与盘后复盘表竞争:节标题不含「逐票收盘记分」
    assert "逐票收盘记分" not in text
    # inbox 追加了一行(表头 2 行 + 1 行)
    assert len(inbox.read_text(encoding="utf-8").strip().splitlines()) == 3


def test_review_split_matches_screen_single_source():
    """复盘侧切分必须与输出侧同源:run 内部走 isr.split_buy_avoid,不另立切分。"""
    import inspect
    src = inspect.getsource(rv.run_intraday_review)
    assert "isr.split_buy_avoid" in src or "split_buy_avoid" in src


def test_open_of_takes_as_of_row_not_future():
    """P0-1 降级基价:open_of 只取 as_of 当日开盘,脏档未来行绝不使用。"""
    lk = lambda code: _kline(11.0, as_of_open=10.2, with_future=True)
    assert rv.open_of("X", "2026-09-08", load_kline_fn=lk) == 10.2
    lk_missing = lambda code: _kline(None).iloc[[0]]              # 只有 09-07 行
    assert rv.open_of("X", "2026-09-08", load_kline_fn=lk_missing) is None


def test_missing_snapshot_fallback_keeps_sample(tmp_path):
    """P0-1 核心语义:11:30 快照缺失 → 走降级回退(主档当日开盘价近似),**不丢样本**——
    买入组 α 可算(非全 None)、report 标 degraded_reason、md 打降级横幅、口径降为『全日开→收』。
    """
    # 候选:买入 P0/P1(看多),规避 S1(看空)。快照缺失(tmp_path 无快照文件)。
    view = {"重排": [_cand("P0", 5, "看多"), _cand("P1", 4, "看多"), _cand("S1", 1, "看空")]}
    # 降级 universe = 候选 3 只 + 一只填充 F0(仅进基准);当日 open→close:P0 +10%、P1 −5%、S1 0、F0 0。
    closes = {"P0": 11.0, "P1": 9.5, "S1": 10.0, "F0": 10.0}
    lk = lambda code: _kline(closes[code], as_of_open=10.0)
    rep = rv.run_intraday_review("2026-09-08", snapshot_root=tmp_path, out_dir=tmp_path,
                                 load_kline_fn=lk, view_fn=lambda: view, write_inbox=False,
                                 universe_fn=lambda: ["P0", "P1", "S1", "F0"])
    # 降级但不丢样本:基准有样本、买入组 α 非全 None
    assert rep["degraded_reason"] is not None
    assert rep["口径"].startswith("降级")
    assert rep["基准"]["样本"] == 4                               # 全 4 只都用 open→close 记进基准
    买入α = [r for r in [rep["小结"]["买入均值α"]] if r is not None]
    assert 买入α, "降级口径下买入组均值 α 必须可算(不丢样本)"
    text = (tmp_path / "午盘_2026-09-08.md").read_text(encoding="utf-8")
    assert "降级口径" in text and "全日" in text                  # md 显式打降级横幅
    assert (tmp_path / "午盘_2026-09-08.md").exists()


def test_missing_snapshot_no_universe_still_scores_candidates(tmp_path):
    """降级时即便 universe 取不到,候选票仍并入兜底基准、可记分(不崩、不丢候选)。"""
    view = {"重排": [_cand("P0", 5, "看多"), _cand("S1", 1, "看空")]}
    closes = {"P0": 11.0, "S1": 9.8}
    lk = lambda code: _kline(closes[code], as_of_open=10.0)
    rep = rv.run_intraday_review("2026-09-08", snapshot_root=tmp_path, out_dir=tmp_path,
                                 load_kline_fn=lk, view_fn=lambda: view, write_inbox=False,
                                 universe_fn=lambda: (_ for _ in ()).throw(RuntimeError("no net")))
    assert rep["degraded_reason"] is not None
    assert rep["基准"]["样本"] >= 1                               # 候选票兜底,基准非空
    assert (tmp_path / "午盘_2026-09-08.md").exists()


def test_snapshot_present_no_degradation(tmp_path):
    """快照在 → 正常口径(下午),不打降级横幅。"""
    isr.persist_noon_snapshot({"P0": {"price": 10.0}, "S1": {"price": 10.0}}, "2026-09-08",
                              out_root=tmp_path)
    view = {"重排": [_cand("P0", 5, "看多"), _cand("S1", 1, "看空")]}
    lk = lambda code: _kline({"P0": 11.0, "S1": 9.7}[code])
    rep = rv.run_intraday_review("2026-09-08", snapshot_root=tmp_path, out_dir=tmp_path,
                                 load_kline_fn=lk, view_fn=lambda: view, write_inbox=False)
    assert rep["degraded_reason"] is None
    assert not rep["口径"].startswith("降级")
    assert "降级口径" not in (tmp_path / "午盘_2026-09-08.md").read_text(encoding="utf-8")
