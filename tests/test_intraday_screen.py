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

    def _fake_screenall(codes, as_of, no_llm=False, no_fetch=False, skip_strategies=None,
                        lean=False):
        calls["stage1"] = {"no_llm": no_llm, "no_fetch": no_fetch, "skip": skip_strategies,
                           "lean": lean, "n": len(codes)}
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
    assert calls["stage1"]["lean"] is True   # 午盘阶段1 必走 lean 精简模式(跳收盘重活)
    # 阶段2:消息面默认跑 LLM(no_llm=False),对同一 as_of
    assert calls["stage2"]["as_of"] == "2026-09-08"
    assert calls["stage2"]["no_llm"] is False
    assert rep["阶段2"]["候选池规模"] == 4
    # run_screen_all 内置消息面节点开关已还原(未泄漏 CANDIDATE_MSG_CONFIRM=0)
    import os
    assert os.environ.get("CANDIDATE_MSG_CONFIRM") in (None, "1")


# ————————————————————————————————————————————————
# ⑥ lean 精简:午盘阶段1 跳过收盘才需要的重活;收盘默认 lean=False 行为不变
# ————————————————————————————————————————————————
def test_run_screen_all_lean_default_off():
    """收盘默认路径不变:run_screen_all 的 lean 默认必须是 False(锁「默认不瘦身」)。"""
    import inspect as _inspect
    assert _inspect.signature(run.run_screen_all).parameters["lean"].default is False


def test_run_screen_all_lean_skips_heavy(monkeypatch):
    """lean=True 时,收盘才需要的重活(数值面深采/事件/因子/合议/panel/前瞻回测/龙虎榜)一个都不跑。

    做法:把 step② 各 screener 桩成返回空 view(不触网),把 step⑤ 之后的重活桩成「一被调用就记名」,
    再跑 run_screen_all(lean=True),断言:返回 lean=True 且重活列表全空(证明确实早退、没进 step⑤)。
    """
    from tools.pipeline import (screen_conditional_rank, screen_council,
                                screen_deduct_quality, screen_max_range, screen_momentum,
                                screen_reversal_turnover, screen_s02, screen_semi_factor,
                                screen_strong, screen_volume)
    # step②:各 screener 桩成返回空 view(不触网、不读主档)。
    for mod, fn in [(screen_council, "run_council_screen"), (screen_s02, "run_s02_screen"),
                    (screen_momentum, "run_momentum_screen"),
                    (screen_semi_factor, "run_semi_factor_screen"),
                    (screen_max_range, "run_max_range_screen"),
                    (screen_volume, "run_volume_screen"), (screen_strong, "run_strong_screen"),
                    (screen_reversal_turnover, "run_reversal_turnover_screen"),
                    (screen_conditional_rank, "run_conditional_rank_screen"),
                    (screen_deduct_quality, "run_deduct_quality_screen")]:
        monkeypatch.setattr(mod, fn, lambda *a, **k: {}, raising=False)
    # step⑤ 之后的重活:一被调用就记名(lean 应一个都不碰)。
    heavy = ["collect_values_missing", "collect_market_context", "enrich_candidates",
             "collect_ticks", "run_serialize", "run_events", "run_factor", "run_council",
             "run_panel", "run_screen", "run_multi_gate", "run_backtest", "collect_lhb",
             "_update_lhb_scorecard"]
    called: list[str] = []
    for name in heavy:
        monkeypatch.setattr(run, name, (lambda n: lambda *a, **k: called.append(n))(name),
                            raising=False)
    rep = run.run_screen_all([], "2026-09-08", no_llm=True, no_fetch=True, lean=True)
    assert rep.get("lean") is True
    assert called == [], f"lean 模式不应跑任何重活,却跑了:{called}"


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


# ————————————————————————————————————————————————
# ⑦ 侧重点重构:买入5(主评价)/规避3(纠偏)切分——单一真源(输出与复盘共用)
# ————————————————————————————————————————————————
def _mk(code, score, direction):
    return {"code": code, "name": code, "完整分": score, "数据面综合分": score,
            "消息面方向": direction, "消息面分": 0.0, "候选来源": [], "理由": []}


def test_split_buy_top5_excludes_bearish():
    """买入 = 排除看空后完整分 Top5;看空票绝不进买入(即便分高)。"""
    reranked = [_mk("B0", 9.0, "看空")] + [_mk(f"P{i}", 8.0 - i, "看多") for i in range(8)]
    买入, 规避 = isr.split_buy_avoid(reranked)
    assert len(买入) == 5
    codes = [x["code"] for x in 买入]
    assert "B0" not in codes                                  # 看空不进买入(即便完整分最高)
    assert codes == ["P0", "P1", "P2", "P3", "P4"]            # 非看空按完整分降序 Top5


def test_split_avoid_prefers_bearish_then_lowest():
    """规避 = 看空优先(最弱在前),不足补最低分;买入/规避不相交。"""
    reranked = ([_mk(f"P{i}", 8.0 - i, "看多") for i in range(6)]
                + [_mk("S1", 2.0, "看空"), _mk("S2", 1.0, "看空")])
    买入, 规避 = isr.split_buy_avoid(reranked)
    codes_avoid = [x["code"] for x in 规避]
    assert len(规避) == 3
    assert codes_avoid[:2] == ["S2", "S1"]                    # 看空优先、完整分升序(最弱在前)
    assert codes_avoid[2] == "P5"                             # 看空不足 → 补最低分非看空票
    assert set(x["code"] for x in 买入).isdisjoint(codes_avoid)  # 买入/规避不相交


def test_split_small_pool_disjoint():
    """池子小于 8 只时买入/规避仍不相交、不重复取同一只。"""
    reranked = [_mk(f"P{i}", 5.0 - i, "看多") for i in range(4)]  # 只有 4 只非看空
    买入, 规避 = isr.split_buy_avoid(reranked)
    assert len(买入) == 4                                     # 不足 5 只全进买入
    assert set(x["code"] for x in 买入).isdisjoint(x["code"] for x in 规避)


def test_action_tag_rules():
    assert isr.action_tag(_mk("A", 1, "看多"), "买入") == "尾盘可买"
    assert isr.action_tag(_mk("A", 1, "中性"), "买入") == "次日观察"
    assert isr.action_tag(_mk("A", 1, "看多"), "规避") == "仅规避"


def test_render_two_columns_and_ledger(monkeypatch, tmp_path):
    """render 产出【今日可买入】+【今日规避】两栏 + 折叠完整台账;看空票不进买入栏。"""
    reranked = ([_mk(f"P{i}", 8.0 - i, "看多") for i in range(6)]
                + [_mk("S1", 1.0, "看空")])
    view = {"候选池规模": 7, "上限命中": False, "统计": {}, "回灌参数": {},
            "重排": reranked}
    monkeypatch.setattr(isr.store, "get_view", lambda name, date=None: view)
    text = isr.render_intraday_md("2026-09-08", out_dir=tmp_path).read_text(encoding="utf-8")
    assert "今日可买入" in text and "今日规避" in text
    assert "主评价对象" in text and "纠偏参照" in text
    assert "完整候选台账" in text and "<details>" in text     # 全序台账保留(折叠)
    assert "行动/时效" in text and "尾盘可买" in text
    # 看空票 S1 出现在规避/台账,但买入栏只应有 P0..P4(前 5 非看空)
    买入段 = text.split("今日规避")[0]
    assert "S1" not in 买入段


# ————————————————————————————————————————————————
# ⑧ D1:11:30 快照落盘(供当日午盘复盘算下午口径)——只存 ≤11:30、不碰主档
# ————————————————————————————————————————————————
def test_persist_noon_snapshot(tmp_path):
    quotes = {"000001": dict(_Q), "SUSPEND": {"price": None}}   # price 缺失的票不落盘
    path = isr.persist_noon_snapshot(quotes, "2026-09-08", out_root=tmp_path)
    assert path == isr.noon_snapshot_path("2026-09-08", out_root=tmp_path)
    import json as _json
    payload = _json.loads(path.read_text(encoding="utf-8"))
    assert payload["as_of"] == "2026-09-08" and payload["count"] == 1
    assert "000001" in payload["quotes"] and "SUSPEND" not in payload["quotes"]
    assert payload["quotes"]["000001"]["price"] == 11.0        # 11:30 冻结价
    # 只存 ≤11:30 冻结口径:不得混入收盘/未来字段
    assert set(payload["quotes"]["000001"]) == {"price", "open", "pct_chg", "turnover"}


def test_run_intraday_screen_persists_snapshot(monkeypatch, tmp_path):
    """编排默认落盘快照;可用 snapshot_root 重定向到 tmp(不碰生产 data/)。"""
    rep = isr.run_intraday_screen(
        "2026-09-08", quotes={"000001": _Q}, codes=["000001"],
        run_screen_all_fn=lambda *a, **k: {"union": 1, "llm_subset": 1, "各策略入选": {}},
        cand_msg_fn=lambda *a, **k: {"候选池规模": 1, "统计": {}},
        write_md=False, snapshot_root=tmp_path)
    assert rep["快照落盘"] == str(isr.noon_snapshot_path("2026-09-08", out_root=tmp_path))
    assert isr.noon_snapshot_path("2026-09-08", out_root=tmp_path).exists()
