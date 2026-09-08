"""午盘全A选股(intraday_screen)单测。

锁语义(为什么这么写,防未来重写误删规则):
  ① 午盘 bar 注入正确 + **不污染 master 收盘档**:注入只作用于返回的 DataFrame 副本,
     全程不调任何主档写入(put_master_kline/fetch_kline);传入的历史 df 对象不被 inplace 改。
  ② 防未来:午盘 bar 的 close=11:30 冻结价、date=as_of,注入后**最后一根=as_of 且无 as_of 之后的行**;
     主档若已含当日/未来行 → 被覆盖(不产生双 bar、不引入未来)。
  ③ 裁策略11:午盘阶段1 传 skip_strategies={策略11},run_screen_all 的 _apply_skip 按 label 精确剔除;
     且该 label 字符串确实存在于 run_screen_all(锁「常量不漂移」)。阶段1 恒 no_llm+no_fetch。
  ④ shortlist ≤ 上限:阶段2 复用 candidate_message,候选 ≤ 策略数×10(有界,绝不全A)。
  ⑤ 消息面只作用 shortlist:阶段2 走 candidate_message.run_candidate_message_enrich(有界节点),
     阶段1/阶段2 两段分离(阶段1 数据 no_llm、阶段2 消息面独立跑)。
"""
import inspect

import pandas as pd
import pytest

from tools import run
from tools.collectors import market
from tools.pipeline import candidate_message as cm
from tools.pipeline import intraday_screen as isr


def _hist():
    """一段假收盘主档 K 线(截至 09-07)。"""
    return pd.DataFrame({
        "date": pd.to_datetime(["2026-09-03", "2026-09-04", "2026-09-07"]),
        "open": [9.0, 9.5, 10.0], "high": [9.2, 9.8, 10.3], "low": [8.9, 9.4, 9.9],
        "close": [9.1, 9.6, 10.1], "volume": [1e6, 1.1e6, 1.2e6],
        "amount": [9e6, 1e7, 1.2e7], "turnover": [1.0, 1.1, 1.2], "pct_chg": [0.5, 0.6, 0.7],
    })


_Q = {"price": 11.0, "open": 10.5, "high": 11.3, "low": 10.4,
      "volume": 2000.0, "amount_wan": 2200.0, "turnover": 2.5, "pct_chg": 8.9}


# ————————————————————————————————————————————————
# ① 午盘 bar 注入正确 + 不污染 master
# ————————————————————————————————————————————————
def test_midday_bar_row_units_and_close():
    bar = isr.midday_bar_row("000001", _Q, "2026-09-08")
    r = bar.iloc[0]
    assert list(bar.columns) == isr._MASTER_COLS          # schema 对齐主档
    assert r["date"] == pd.Timestamp("2026-09-08")
    assert r["close"] == 11.0                              # close = 11:30 冻结价
    assert r["volume"] == 2000.0 * 100                     # 手→股
    assert r["amount"] == 2200.0 * 10000                   # 万元→元
    assert r["turnover"] == 2.5 and r["pct_chg"] == 8.9    # 百分数同口径直取


def test_inject_appends_without_mutating_source():
    hist = _hist()
    before_rows = len(hist)
    before_last_close = hist.iloc[-1]["close"]
    out = isr.inject_midday_bar(hist, "000001", {"000001": _Q}, "2026-09-08")
    assert len(out) == before_rows + 1                     # 追加一根午盘 bar
    assert out.iloc[-1]["date"] == pd.Timestamp("2026-09-08")
    assert out.iloc[-1]["close"] == 11.0
    # 传入的历史 df 未被 inplace 改(不污染调用方持有的对象)
    assert len(hist) == before_rows
    assert hist.iloc[-1]["close"] == before_last_close


def test_injection_never_writes_master(monkeypatch):
    """注入上下文全程不得触发任何主档写入/网络采集(不污染 master 收盘档)。"""
    def _boom(*a, **k):
        raise AssertionError("午盘注入不得写主档/触网")

    monkeypatch.setattr(market, "load_kline", lambda code: _hist())
    monkeypatch.setattr(market, "fetch_kline", _boom, raising=False)
    from tools.store import repo as store
    monkeypatch.setattr(store, "put_master_kline", _boom, raising=False)
    monkeypatch.setattr(store, "append_master_kline", _boom, raising=False)

    with isr.midday_injection({"X": _Q}, "2026-09-08"):
        df = market.load_kline("X")                        # 走注入
        rec = market.load_kline_recent("X")                # recent 内部走同一 module 全局
    assert df.iloc[-1]["date"] == pd.Timestamp("2026-09-08")
    assert rec.iloc[-1]["date"] == pd.Timestamp("2026-09-08")
    # 上下文退出后还原
    assert market.load_kline("X").iloc[-1]["date"] == pd.Timestamp("2026-09-07")


# ————————————————————————————————————————————————
# ② 防未来:覆盖当日/未来行,最后一根=as_of,无 as_of 之后的行
# ————————————————————————————————————————————————
def test_injection_overwrites_and_no_future_rows():
    hist = _hist()
    # 主档异常含当日+未来行(模拟脏档),注入应覆盖当日、剔除未来
    dirty = pd.concat([hist, pd.DataFrame({
        "date": pd.to_datetime(["2026-09-08", "2026-09-09"]),
        "open": [99, 99], "high": [99, 99], "low": [99, 99], "close": [99, 99],
        "volume": [9, 9], "amount": [9, 9], "turnover": [9, 9], "pct_chg": [9, 9]})],
        ignore_index=True)
    out = isr.inject_midday_bar(dirty, "000001", {"000001": _Q}, "2026-09-08")
    dates = pd.to_datetime(out["date"])
    assert dates.max() == pd.Timestamp("2026-09-08")       # 无 as_of 之后的行
    assert (dates == pd.Timestamp("2026-09-08")).sum() == 1  # 当日只一根(覆盖,不双 bar)
    assert out.iloc[-1]["close"] == 11.0                   # 用午盘价,不是脏档的 99


def test_no_quote_returns_history_unchanged():
    hist = _hist()
    out = isr.inject_midday_bar(hist, "SUSPEND", {}, "2026-09-08")  # 停牌/无快照
    assert out.iloc[-1]["date"] == pd.Timestamp("2026-09-07")       # 用收盘历史,不瞎补


# ————————————————————————————————————————————————
# ③ 裁策略11
# ————————————————————————————————————————————————
def test_apply_skip_removes_strategy11():
    screeners = [("策略0·多专家合议", 1), ("策略11·指标条件化状态排序", 2), ("策略4·动量组合", 3)]
    kept = [l for l, _ in run._apply_skip(screeners, isr.INTRADAY_SKIP_STRATEGIES)]
    assert "策略11·指标条件化状态排序" not in kept
    assert "策略0·多专家合议" in kept and "策略4·动量组合" in kept
    assert run._apply_skip(screeners, None) == screeners            # 空 skip 原样


def test_skip_label_matches_run_source():
    """裁剪的 label 常量必须与 run_screen_all 里真实 label 一致(锁不漂移)。"""
    src = inspect.getsource(run.run_screen_all)
    for label in isr.INTRADAY_SKIP_STRATEGIES:
        assert label in src


# ————————————————————————————————————————————————
# ④ shortlist ≤ 上限(复用 candidate_message 有界候选)
# ————————————————————————————————————————————————
def test_shortlist_capped(monkeypatch):
    # 每个策略 view 都塞 20 只不重叠票 → 若无上限会爆;上限=策略数×倍数
    def _get_view(name, date="latest"):
        return {"top": [{"code": f"{name[:2]}{i:03d}"} for i in range(20)]}
    monkeypatch.setattr(cm.store, "get_view", _get_view)
    monkeypatch.setattr(cm.stock_pool, "get_codes", lambda: [])
    # 默认上限 = 策略数×10:候选恒 ≤ 上限(有界,绝不全A)
    built = cm.build_candidate_pool("2026-09-08", top_k=10)
    assert len(built["pool"]) <= built["硬上限"]
    assert built["硬上限"] == built["策略数"] * 10
    # 显式收紧上限 → 截断生效、命中标记为真(锁截断语义)
    capped = cm.build_candidate_pool("2026-09-08", top_k=10, hard_cap=15)
    assert len(capped["pool"]) == 15
    assert capped["上限命中"] is True


# ————————————————————————————————————————————————
# ⑤ 编排:阶段1 数据(no_llm+no_fetch+裁策略11)/阶段2 消息面(有界),两段分离
# ————————————————————————————————————————————————
def test_orchestration_two_phase(monkeypatch, tmp_path):
    calls = {}

    def _fake_screenall(codes, as_of, no_llm=False, no_fetch=False, skip_strategies=None):
        calls["stage1"] = {"no_llm": no_llm, "no_fetch": no_fetch, "skip": skip_strategies,
                           "n": len(codes)}
        return {"union": 3, "llm_subset": 5, "各策略入选": {}}

    def _fake_candmsg(as_of, no_llm=False):
        calls["stage2"] = {"as_of": as_of, "no_llm": no_llm}
        return {"候选池规模": 4, "统计": {"看多": 1}}

    rep = isr.run_intraday_screen(
        "2026-09-08", quotes={"000001": _Q}, codes=["000001", "000002"],
        run_screen_all_fn=_fake_screenall, cand_msg_fn=_fake_candmsg, write_md=False)

    # 阶段1:数据初筛必须 no_llm + no_fetch + 裁策略11
    assert calls["stage1"]["no_llm"] is True
    assert calls["stage1"]["no_fetch"] is True
    assert calls["stage1"]["skip"] == isr.INTRADAY_SKIP_STRATEGIES
    # 阶段2:消息面默认跑 LLM(no_llm=False),对同一 as_of
    assert calls["stage2"]["as_of"] == "2026-09-08"
    assert calls["stage2"]["no_llm"] is False
    assert rep["阶段2"]["候选池规模"] == 4
    # run_screen_all 内置消息面节点开关已还原(未泄漏 CANDIDATE_MSG_CONFIRM=0)
    import os
    assert os.environ.get("CANDIDATE_MSG_CONFIRM") in (None, "1")


def test_render_md_from_view(monkeypatch, tmp_path):
    view = {"候选池规模": 2, "上限命中": False,
            "统计": {"看多": 1, "看空": 0, "中性": 1, "全弃权": 0, "有发声": 1},
            "回灌参数": {"启用": True, "回灌权重": 0.5},
            "重排": [
                {"候选排名": 1, "code": "000001", "name": "甲", "完整分": 1.2,
                 "数据面综合分": 0.7, "消息面方向": "看多", "消息面分": 0.5,
                 "候选来源": ["动量组合"], "理由": ["情绪三层:偏多"]},
                {"候选排名": 2, "code": "000002", "name": "乙", "完整分": 0.3,
                 "数据面综合分": 0.3, "消息面方向": "中性", "消息面分": 0.0,
                 "候选来源": ["量价放量"], "理由": []},
            ]}
    monkeypatch.setattr(isr.store, "get_view", lambda name, date=None: view)
    path = isr.render_intraday_md("2026-09-08", breadth={"上涨": 100, "下跌": 50, "平盘": 3,
                                                          "样本": 153, "中位涨幅": 0.4},
                                  out_dir=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert path.name == "日内全A_2026-09-08.md"   # 刻意避开既有「午盘研判」的 日内_<date>.md
    assert "全A午盘选股" in text and "11:30 午休冻结" in text and "非投资建议" in text
    assert "未收盘·午盘价" in text
    assert "策略11·指标条件化状态排序" in text          # 标注裁了策略11
    assert "000001" in text and "甲" in text and "看多" in text


def test_render_md_empty_view(monkeypatch, tmp_path):
    monkeypatch.setattr(isr.store, "get_view",
                        lambda name, date=None: (_ for _ in ()).throw(FileNotFoundError()))
    path = isr.render_intraday_md("2026-09-08", out_dir=tmp_path)
    assert "降级空跑" in path.read_text(encoding="utf-8")
