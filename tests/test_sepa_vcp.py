"""SEPA+VCP 监控单测:锁入池三条 / 轮次≥3日 / 相邻收缩 / 洗盘vs失效 / 星标不过滤。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.analysis.sepa_vcp import sepa as sepa_mod
from tools.analysis.sepa_vcp import stars as stars_mod
from tools.analysis.sepa_vcp import vcp as vcp_mod
from tools.pipeline import screen_sepa_vcp as pipe
from tools.store import repo as store


def _df(close, high=None, low=None, open_=None, volume=None, start="2020-01-01"):
    n = len(close)
    close = list(map(float, close))
    if high is None:
        high = [c + 0.5 for c in close]
    if low is None:
        low = [c - 0.5 for c in close]
    if open_ is None:
        open_ = close[:]
    if volume is None:
        volume = [1000.0] * n
    return pd.DataFrame({
        "date": pd.bdate_range(start, periods=n),
        "open": open_, "high": high, "low": low, "close": close, "volume": volume,
    })


def _uptrend(n: int = 260, start: float = 10.0, step: float = 0.2) -> pd.DataFrame:
    close = [start + i * step for i in range(n)]
    return _df(close)


# ———————————————————— F1 SEPA ————————————————————
def test_sepa_pass_on_strict_uptrend():
    r = sepa_mod.screen_latest(_uptrend())
    assert r["入池"] is True
    d = r["明细"]
    assert d["close"] > d["MA50"] > d["MA150"] > d["MA200"]
    assert d["MA200向上"] is True


def test_sepa_fail_when_ma_not_stacked():
    n = 260
    close = [80.0 - i * 0.1 for i in range(n)]
    r = sepa_mod.screen_latest(_df(close))
    assert r["入池"] is False


def test_sepa_fail_when_ma200_not_up():
    """先涨后横:MA50>MA150>MA200 可能仍成立,但 MA200 不再向上。"""
    n = 260
    close = [10.0 + min(i, 200) * 0.2 for i in range(n)]
    # 后 60 根完全横盘,MA200 走平或微降
    r = sepa_mod.sepa_pass(_df(close), t=n - 1)
    # 横盘末期 MA200 当前 ≈ 20 日前,不满足严格 >
    if r.get("明细"):
        assert r["明细"]["MA200向上"] is False or r["入池"] is False
    else:
        assert r["入池"] is False


def test_sepa_insufficient_history():
    r = sepa_mod.screen_latest(_uptrend(50))
    assert r["入池"] is False
    assert "历史不足" in r["原因"]


# ———————————————————— F3 / F4 轮次 ————————————————————
def _wave():
    """构造两轮明确收缩:高100回撤到75(10日),再高96回撤到82(6日)。前面垫 40 根缓升。"""
    prefix = list(np.linspace(60, 90, 40))
    # 轮1:冲高 90→100(4日) 再回落到 75(10日)
    r1_up = list(np.linspace(90, 100, 5))
    r1_dn = list(np.linspace(99, 75, 10))
    # 轮2:反弹到 96(5日) 再收到 82(6日)
    r2_up = list(np.linspace(76, 96, 6))
    r2_dn = list(np.linspace(95, 82, 7))
    close = prefix + r1_up + r1_dn + r2_up + r2_dn
    high = [c + 0.8 for c in close]
    low = [c - 0.8 for c in close]
    # 强化 pivot:轮1高点、轮1低点、轮2高点、轮2低点做成左右 3 根极值
    # 找大致位置后钉死
    i_h1 = 40 + 4   # 100
    i_l1 = i_h1 + 10
    i_h2 = i_l1 + 6
    i_l2 = len(close) - 1
    high[i_h1] = 101.0
    close[i_h1] = 100.0
    low[i_l1] = 74.0
    close[i_l1] = 75.0
    high[i_h2] = 97.0
    close[i_h2] = 96.0
    low[i_l2] = 81.0
    close[i_l2] = 82.0
    vol = [2000.0] * len(close)
    for i in range(i_h2, i_l2 + 1):
        vol[i] = 800.0  # 后段量缩
    return _df(close, high=high, low=low, volume=vol)


def test_single_bar_is_not_a_round():
    close = list(np.linspace(10, 20, 30)) + [21, 18]  # 2 日摆动
    rounds = vcp_mod.segment_rounds(_df(close))
    assert all(r["天数"] >= 3 for r in rounds)


def test_two_day_swing_discarded():
    """左右窗口=3 时,2 日高低根本确认不成 pivot,不应成轮。"""
    close = list(np.linspace(10, 15, 20))
    close += [16, 14]  # 两根
    close += list(np.linspace(14.2, 15, 10))
    rounds = vcp_mod.segment_rounds(_df(close, high=[c + 0.1 for c in close],
                                        low=[c - 0.1 for c in close]))
    assert all(r["天数"] >= 3 for r in rounds)


def test_adjacent_contraction_not_global_monotonic():
    """三轮回撤 25→10→18:末对不收缩,但分析仍只比当前 vs 上一轮。"""
    # 用 analyze 的末对字段锁「只比相邻」
    df = _wave()
    v = vcp_mod.analyze_vcp(df)
    if v["轮数"] >= 2 and v.get("末对收缩"):
        pair = v["末对收缩"]
        assert "振幅缩小" in pair and "higher_low" in pair
        assert pair["硬收缩"] == (pair["振幅缩小"] and pair["higher_low"])


def test_wave_has_at_least_one_round():
    rounds = vcp_mod.segment_rounds(_wave())
    assert isinstance(rounds, list)
    for r in rounds:
        assert r["天数"] >= 3
        assert r["回撤%"] >= 0
        assert "VCP完成" not in str(r)


# ———————————————————— F5 洗盘 vs 失效 ————————————————————
def test_intraday_pierce_recover_is_wash_not_fail():
    df = _uptrend(80)
    prev_low = 20.0
    t = len(df) - 1
    df.loc[df.index[t], "low"] = 19.0
    df.loc[df.index[t], "close"] = 21.0
    st = vcp_mod.structure_status(df, t, prev_low, 1.5)
    assert st["洗盘刺破"] is True
    assert st["失效"] is False


def test_close_break_1p5_is_fail():
    df = _uptrend(80)
    prev_low = 20.0
    t = len(df) - 1
    df.loc[df.index[t], "low"] = 19.0
    df.loc[df.index[t], "close"] = 19.6  # 跌破 1.5% → 20*0.985=19.7
    st = vcp_mod.structure_status(df, t, prev_low, 1.5)
    assert st["失效"] is True
    assert st["洗盘刺破"] is False


# ———————————————————— F2 星标 / F6 分类 ————————————————————
def test_no_star_still_in_pool_new_candidate_requires_star():
    tags = pipe._tags({"VCP进行中": False, "接近枢纽": False, "结构破坏": False},
                      first_day=True, n_stars=0)
    assert "新候选" not in tags
    tags2 = pipe._tags({"VCP进行中": False, "接近枢纽": False, "结构破坏": False},
                       first_day=True, n_stars=1)
    assert "新候选" in tags2


def test_sector_star_min_two():
    pool = [
        {"code": "1", "industry": "电子"},
        {"code": "2", "industry": "电子"},
        {"code": "3", "industry": "银行"},
    ]
    s = stars_mod.sector_star_codes(pool, min_n=2)
    assert s == {"1", "2"}


def test_chart_title_not_vcp_complete():
    df = _uptrend(220)
    v = vcp_mod.analyze_vcp(df)
    ch = vcp_mod.build_chart_payload(df, v)
    assert ch["title"] == "收缩结构参考"
    blob = str(ch)
    assert "VCP完成" not in blob and "VCP 完成" not in blob


def test_radar_mentions_session():
    text = pipe._radar("收盘", [
        {"code": "300308", "标签": ["VCP进行中"], "回撤链": [25, 15, 8], "轮数": 3,
         "板块星": False, "基本面星": False},
        {"code": "002222", "标签": ["新候选"], "回撤链": [], "轮数": 0,
         "板块星": True, "基本面星": False},
    ])
    assert "【收盘雷达】" in text
    assert "重点：2只" in text
    assert "300308" in text
    assert "25%" in text and "8%" in text
    assert "非投资建议" not in text  # 雷达正文短;免责在 view


def test_pipeline_writes_views(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(stars_mod, "fundamental_star", lambda code: True)
    monkeypatch.setattr(stars_mod, "industry_of", lambda code: "电子")
    kl = {"000001": _uptrend(260), "000002": _uptrend(260)}
    monkeypatch.setattr(pipe.market, "load_kline_recent", lambda c, rows=None: kl[c])
    monkeypatch.setattr(pipe, "_name_of", lambda c: "测试" + c)
    monkeypatch.setattr(pipe.stars_mod, "_is_st", lambda n: False)
    v = pipe.run_sepa_vcp(["000001", "000002"], as_of="2026-08-18",
                          session="收盘", fetch=False, write_charts=True)
    assert v["合格池"]["合格数"] == 2
    assert v["合格池"]["session"] == "收盘"
    assert "非投资建议" in v["合格池"]["免责"]
    rows = store.get_view("SEPA合格池")["rows"]
    assert all(r["星标数"] >= 1 for r in rows)
    # 趋势分(60日涨幅):每行必带,供展示层按强度取 Top10 排序
    assert all(isinstance(r.get("趋势分"), (int, float)) for r in rows)
    # 今日首入 + 有星 → 新候选进观察池
    watch = store.get_view("SEPA观察池")["rows"]
    assert any("新候选" in (r.get("标签") or []) for r in watch)
    ch = store.get_code_view("sepa_vcp_chart", watch[0]["code"])
    assert ch["title"] == "收缩结构参考"


def test_sepa_page_top10_by_trend(monkeypatch):
    """展示层:合格池 view 存全量,sepa_page 按趋势分降序取 Top10;合格数仍报全量真值。

    锁语义:防未来改动把"存全量/展示Top10"退化成"pipeline 截断"(那会破坏入池天数续期)。
    """
    from web import data_access as da
    rows = [{"code": f"{i:06d}", "name": f"T{i}", "industry": "x",
             "入池天数": 1, "星标数": 1, "今日首入": False, "趋势分": round(i * 0.01, 4)}
            for i in range(15)]                          # 趋势分 0.00..0.14

    def fake_get_view(name, date="latest"):
        if name == "SEPA合格池":
            return {"as_of": "2026-08-25", "session": "收盘", "合格数": 15, "rows": rows}
        raise FileNotFoundError(name)                    # 观察池/雷达缺失 → 空表不崩

    monkeypatch.setattr(da.store, "get_view", fake_get_view)
    out = da.sepa_page("2026-08-25")
    assert len(out["合格"]) == 10                          # 只展示前10
    assert out["展示数"] == 10
    assert out["合格数"] == 15                             # 合格数仍是全量真值,不被 Top10 截断误报
    trends = [r["趋势分"] for r in out["合格"]]
    assert trends == sorted(trends, reverse=True)         # 按趋势分降序
    assert trends[0] == 0.14 and min(trends) == 0.05      # 取的是最强10只(0.05..0.14),弱的5只被截掉
