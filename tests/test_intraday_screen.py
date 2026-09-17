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
      "volume": 2000.0, "amount_wan": 2200.0, "turnover": 2.5, "pct_chg": 8.9}   # volume 已是 gtimg_quote 归一后的「股」


# ————————————————————————————————————————————————
# ① 午盘 bar 注入正确 + 不污染 master
# ————————————————————————————————————————————————
def test_midday_bar_row_units_and_close():
    bar = isr.midday_bar_row("000001", _Q, "2026-09-08")
    r = bar.iloc[0]
    assert list(bar.columns) == isr._MASTER_COLS          # schema 对齐主档
    assert r["date"] == pd.Timestamp("2026-09-08")
    assert r["close"] == 11.0                              # close = 11:30 冻结价
    assert r["volume"] == 2000.0                           # gtimg_quote 已归一「股」,midday_bar 不再 ×100
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


def test_counts_sourced_from_config():
    """买入/规避条数是配置真源 THRESHOLDS['午盘选股'] 的投影(改一处生效,不散落硬编码)。"""
    from tools.config import strategy
    cfg = strategy.THRESHOLDS["午盘选股"]
    assert isr.N_BUY == cfg["买入条数"] == 5
    assert isr.N_AVOID == cfg["规避条数"] == 3
    assert isr.noon_cfg()["规避条数"] == 3


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


# ————————————————————————————————————————————————
# ⑨ P0-2:D-0 交易计划——买入票必含止损/止盈/收盘了结;止损随波动锚缩放且夹在上下限
# ————————————————————————————————————————————————
def _atr_hist(atr_pct):
    """构造一段日线,使 ATR≈close×atr_pct%(每根 high-low=close×atr_pct%,close 平)。"""
    close = 10.0
    rng = close * atr_pct / 100.0
    dates = pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07"])
    return pd.DataFrame({"date": dates, "open": [close] * 5,
                         "high": [close + rng / 2] * 5, "low": [close - rng / 2] * 5,
                         "close": [close] * 5, "volume": [1e6] * 5, "amount": [1e7] * 5,
                         "turnover": [1.0] * 5, "pct_chg": [0.0] * 5})


def test_trade_plan_has_stop_target_and_eod_exit():
    """买入票交易计划必含止损位/止盈位/收盘强制了结(补 1b 缺口)。"""
    q = {"price": 10.0, "open": 10.0, "high": 10.2, "low": 9.8}
    tp = isr.compute_trade_plan("X", q, "2026-09-08", load_kline_fn=lambda c: _atr_hist(4.0))
    assert tp is not None
    assert tp["止损位"] < tp["现价11:30"] < tp["止盈位"]          # 止损在下、止盈在上
    assert "收盘无条件平仓" in tp["了结"]                          # 当日了结纪律
    assert "跳空" in tp["了结"]                                    # #20 跳空保护
    assert "ATR" in tp["波动锚"]                                   # 波动锚锚定 ATR(非拍脑袋)


def test_trade_plan_stop_scales_with_volatility_and_clamped():
    """止损距离随波动锚缩放;且夹在 [止损下限, 止损上限] 内(高波不无限放、低波不贴太近)。"""
    q = {"price": 10.0, "open": 10.0, "high": 10.1, "low": 9.9}
    cfg = isr.trade_plan_cfg()
    lo, hi = cfg["止损下限pct"], cfg["止损上限pct"]
    # 低波(ATR 0.5%×倍数1.0=0.5% < 下限)→ 夹到下限
    tp_lo = isr.compute_trade_plan("X", q, "2026-09-08", load_kline_fn=lambda c: _atr_hist(0.5))
    assert tp_lo["止损距离%"] == pytest.approx(lo)
    # 高波(ATR 20%×1.0=20% > 上限)→ 夹到上限
    tp_hi = isr.compute_trade_plan("X", q, "2026-09-08", load_kline_fn=lambda c: _atr_hist(20.0))
    assert tp_hi["止损距离%"] == pytest.approx(hi)
    # 中波(ATR 4%)→ 落在区间内、严格大于低波档
    tp_mid = isr.compute_trade_plan("X", q, "2026-09-08", load_kline_fn=lambda c: _atr_hist(4.0))
    assert lo < tp_mid["止损距离%"] < hi


def test_trade_plan_falls_back_to_intraday_range_when_no_atr():
    """历史不足算不出 ATR → 回退当日振幅锚(仍是可算口径,不缺省除非振幅也无)。"""
    q = {"price": 10.0, "open": 10.0, "high": 10.6, "low": 9.6}    # 当日振幅=(10.6-9.6)/10=10%
    only_prev = pd.DataFrame({"date": pd.to_datetime(["2026-09-07"]), "open": [10.0],
                              "high": [10.1], "low": [9.9], "close": [10.0], "volume": [1e6],
                              "amount": [1e7], "turnover": [1.0], "pct_chg": [0.0]})  # 仅 1 根 → ATR None
    tp = isr.compute_trade_plan("X", q, "2026-09-08", load_kline_fn=lambda c: only_prev)
    assert "振幅" in tp["波动锚"]                                   # 回退到当日振幅锚


def test_trade_plan_none_when_no_price():
    """停牌/无午盘价 → 无法定计划(None),render 侧会打「无法定价位」而非乱造。"""
    assert isr.compute_trade_plan("X", {"price": None}, "2026-09-08",
                                  load_kline_fn=lambda c: _atr_hist(4.0)) is None
    assert isr.compute_trade_plan("X", None, "2026-09-08",
                                  load_kline_fn=lambda c: _atr_hist(4.0)) is None


def test_atr_pct_excludes_as_of_and_future(monkeypatch):
    """防未来:ATR 只用 date<as_of 的历史,午盘 bar/当日/未来行绝不进 ATR。"""
    hist = _atr_hist(4.0)
    dirty = pd.concat([hist, pd.DataFrame({
        "date": pd.to_datetime(["2026-09-08", "2026-09-09"]),
        "open": [99, 99], "high": [999, 999], "low": [1, 1], "close": [99, 99],
        "volume": [9, 9], "amount": [9, 9], "turnover": [9, 9], "pct_chg": [9, 9]})],
        ignore_index=True)
    clean = isr._atr_pct("X", "2026-09-08", load_kline_fn=lambda c: hist)
    with_dirty = isr._atr_pct("X", "2026-09-08", load_kline_fn=lambda c: dirty)
    assert clean == pytest.approx(with_dirty)                       # 脏的当日/未来行不改变 ATR


def test_render_includes_trade_plan_table(monkeypatch, tmp_path):
    """render 传 quotes → 买入组产 D-0 交易计划表(止损/止盈/收盘了结);缺 quotes 则不产该表。"""
    reranked = [_mk(f"P{i}", 8.0 - i, "看多") for i in range(6)]
    view = {"候选池规模": 6, "上限命中": False, "统计": {}, "回灌参数": {}, "重排": reranked}
    monkeypatch.setattr(isr.store, "get_view", lambda name, date=None: view)
    quotes = {f"P{i}": {"price": 10.0, "open": 10.0, "high": 10.2, "low": 9.8} for i in range(6)}
    text = isr.render_intraday_md("2026-09-08", out_dir=tmp_path, quotes=quotes,
                                  load_kline_fn=lambda c: _atr_hist(4.0)).read_text(encoding="utf-8")
    assert "D-0 交易计划" in text and "止损位" in text and "止盈位" in text
    assert "收盘无条件了结" in text or "收盘无条件平仓" in text
    # 不传 quotes → 无交易计划表(向后兼容)
    text2 = isr.render_intraday_md("2026-09-08", out_dir=tmp_path).read_text(encoding="utf-8")
    assert "D-0 交易计划" not in text2


# ————————————————————————————————————————————————
# ⑩ P0-1:11:30 快照自检(存在性+样本率)——不达标即暴露,别静默丢当日复盘样本
# ————————————————————————————————————————————————
def test_snapshot_self_check_ok(tmp_path):
    isr.persist_noon_snapshot({f"{i:06d}": {"price": 10.0} for i in range(8)}, "2026-09-08",
                              out_root=tmp_path)
    chk = isr.snapshot_self_check("2026-09-08", 10, out_root=tmp_path)       # 8/10=80%≥60%
    assert chk["ok"] is True and chk["count"] == 8


def test_snapshot_self_check_flags_missing_and_low_coverage(tmp_path):
    # 文件缺失 → 不过
    miss = isr.snapshot_self_check("2026-09-08", 10, out_root=tmp_path)
    assert miss["ok"] is False and "未生成" in miss["reason"]
    # 半空(2/10=20%<60%)→ 不过
    isr.persist_noon_snapshot({f"{i:06d}": {"price": 10.0} for i in range(2)}, "2026-09-08",
                              out_root=tmp_path)
    low = isr.snapshot_self_check("2026-09-08", 10, out_root=tmp_path)
    assert low["ok"] is False and "样本率不足" in low["reason"]


def test_run_intraday_screen_reports_self_check(tmp_path):
    """编排把快照自检结果带进 report(可观测,便于告警/追因)。"""
    rep = isr.run_intraday_screen(
        "2026-09-08", quotes={"000001": _Q}, codes=["000001"],
        run_screen_all_fn=lambda *a, **k: {"union": 1, "llm_subset": 1, "各策略入选": {}},
        cand_msg_fn=lambda *a, **k: {"候选池规模": 1, "统计": {}},
        write_md=False, snapshot_root=tmp_path)
    assert rep["快照自检"]["ok"] is True and rep["快照自检"]["count"] == 1


# ————————————————————————————————————————————————
# ⑩ D1 旁路机读候选:noon 冻结、close 不覆盖,供午盘 Claude 逐票深度分析消费
#   锁语义:①内容与 split_buy_avoid 单一真源一致 ②纯旁路不写主档/不改现有产物
#          ③消费侧 JSON 优先、缺失回退解析 日内全A_ md ④防未来只带 ≤as_of 字段
# ————————————————————————————————————————————————
def _view_8():
    reranked = ([_mk(f"P{i}", 8.0 - i, "看多") for i in range(6)]
                + [_mk("S1", 1.0, "看空"), _mk("S2", 0.5, "看空")])
    for i, x in enumerate(reranked):
        x["候选排名"] = i + 1
    return {"候选池规模": 8, "上限命中": False,
            "统计": {"看多": 6, "看空": 2, "中性": 0}, "回灌参数": {}, "重排": reranked}


def test_persist_noon_candidates_content_and_split(monkeypatch, tmp_path):
    """旁路候选落盘:买入/规避切分与 split_buy_avoid 一致;全序台账保留;字段瘦身。"""
    view = _view_8()
    monkeypatch.setattr(isr.store, "get_view", lambda name, date=None: view)
    path = isr.persist_noon_candidates("2026-09-08", breadth={"上涨": 1}, out_root=tmp_path)
    assert path == isr.noon_candidates_path("2026-09-08", out_root=tmp_path)
    import json as _json
    payload = _json.loads(path.read_text(encoding="utf-8"))
    买入, 规避 = isr.split_buy_avoid(view["重排"])
    assert payload["买入代码"] == [x["code"] for x in 买入]      # 与单一真源切分一致
    assert payload["规避代码"] == [x["code"] for x in 规避]
    assert payload["规避代码"][:2] == ["S2", "S1"]               # 看空优先、最弱在前
    assert "S1" not in payload["买入代码"]                       # 看空不进买入
    assert len(payload["台账"]) == 8                            # 全序台账保留(复盘/审计)
    assert payload["as_of"] == "2026-09-08" and payload["freeze_label"] == isr.FREEZE_LABEL
    # 字段瘦身:只带机读稳定字段,理由拼成字符串
    assert set(payload["台账"][0]) == set(isr._CAND_FIELDS)
    assert isinstance(payload["台账"][0]["理由"], str)


def test_persist_noon_candidates_no_view_returns_none(monkeypatch, tmp_path):
    """无 view/重排 → 返回 None、不落盘、不阻断(纯旁路)。"""
    monkeypatch.setattr(isr.store, "get_view",
                        lambda name, date=None: (_ for _ in ()).throw(FileNotFoundError()))
    assert isr.persist_noon_candidates("2026-09-08", out_root=tmp_path) is None
    assert not isr.noon_candidates_path("2026-09-08", out_root=tmp_path).exists()


def test_persist_noon_candidates_never_writes_master(monkeypatch, tmp_path):
    """铁律:旁路落盘不触碰任何主档写入(防污染)。"""
    def _boom(*a, **k):
        raise AssertionError("旁路候选绝不应写主档")
    monkeypatch.setattr(isr.store, "put_master_kline", _boom, raising=False)
    monkeypatch.setattr(isr.store, "append_master_kline", _boom, raising=False)
    monkeypatch.setattr(isr.store, "get_view", lambda name, date=None: _view_8())
    isr.persist_noon_candidates("2026-09-08", out_root=tmp_path)


def test_read_noon_candidates_prefers_json(monkeypatch, tmp_path):
    """消费侧:旁路 JSON 存在 → 直接读它(source=json),不解析 md。"""
    monkeypatch.setattr(isr.store, "get_view", lambda name, date=None: _view_8())
    isr.persist_noon_candidates("2026-09-08", out_root=tmp_path)
    got = isr.read_noon_candidates("2026-09-08", root=tmp_path, top=3)
    assert got["source"] == "json"
    assert len(got["台账"]) == 3                                # top 截断
    assert got["买入代码"][0] == "P0"


def test_read_noon_candidates_fallback_to_md(tmp_path):
    """消费侧:无旁路 JSON → 回退解析 日内全A_<date>.md 台账(退化路径可用)。"""
    md = tmp_path / "日内全A_2026-09-08.md"
    md.write_text(
        "# 全A午盘选股 · 日内_2026-09-08\n\n"
        "## 今日可买入(精选 2 只 · 主评价对象)\n\n"
        "| 序 | 代码 | 名称 | 完整分 |\n|---|---|---|---|\n"
        "| 1 | 000001 | 甲 | 0.9 |\n| 2 | 000002 | 乙 | 0.8 |\n\n"
        "## 今日规避(精选 1 只 · 纠偏参照)\n\n"
        "| 序 | 代码 | 名称 | 完整分 |\n|---|---|---|---|\n"
        "| 1 | 600001 | 丙 | -0.5 |\n\n"
        "<details>\n<summary>完整候选台账 · 买入排序(共 3 只)</summary>\n\n"
        "| 排名 | 代码 | 名称 | 完整分 | 数据面综合分 | 消息面方向 | 消息面分 | 候选来源 | 理由 |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
        "| 1 | 000001 | 甲 | 0.9 | 0.6 | 看多 | 0.5 | 策略0合议 | 事件驱动:预告增速 |\n"
        "| 2 | 000002 | 乙 | 0.8 | 0.5 | 中性 | 0.0 | 量价放量 | — |\n"
        "| 3 | 600001 | 丙 | -0.5 | -0.3 | 看空 | -0.5 | 动量组合 | 减持 |\n\n"
        "</details>\n", encoding="utf-8")
    got = isr.read_noon_candidates("2026-09-08", root=tmp_path, selection_dir=tmp_path)
    assert got["source"] == "md"
    assert got["买入代码"] == ["000001", "000002"]
    assert got["规避代码"] == ["600001"]
    assert [x["code"] for x in got["台账"]] == ["000001", "000002", "600001"]
    assert got["台账"][0]["消息面方向"] == "看多" and got["台账"][0]["候选来源"] == "策略0合议"


def test_read_noon_candidates_none_when_absent(tmp_path):
    """JSON 与 md 都无 → None(供门控识别未就绪)。"""
    assert isr.read_noon_candidates("2026-09-08", root=tmp_path, selection_dir=tmp_path) is None


def test_run_intraday_screen_persists_candidates(monkeypatch, tmp_path):
    """编排默认落旁路候选;可 snapshot_root 重定向;report 带路径。"""
    monkeypatch.setattr(isr.store, "get_view", lambda name, date=None: _view_8())
    rep = isr.run_intraday_screen(
        "2026-09-08", quotes={"000001": _Q}, codes=["000001"],
        run_screen_all_fn=lambda *a, **k: {"union": 1, "llm_subset": 1, "各策略入选": {}},
        cand_msg_fn=lambda *a, **k: {"候选池规模": 1, "统计": {}},
        write_md=False, snapshot_root=tmp_path)
    assert rep["旁路候选"] == str(isr.noon_candidates_path("2026-09-08", out_root=tmp_path))
    assert isr.noon_candidates_path("2026-09-08", out_root=tmp_path).exists()


# ————————————————————————————————————————————————
# ⑪ 统筹Q2:阶段1早产候选(数据面综合分排序,免等阶段2消息面batch)——门控~11:50即可开跑
# ————————————————————————————————————————————————
def test_persist_stage1_candidates_distinct_file_and_marker(monkeypatch, tmp_path):
    """stage='stage1' 落独立文件名 + payload 带 stage 标记(与全量互不覆盖)。"""
    monkeypatch.setattr(isr.store, "get_view", lambda name, date=None: _view_8())
    p_full = isr.persist_noon_candidates("2026-09-08", out_root=tmp_path, stage="full")
    p_s1 = isr.persist_noon_candidates("2026-09-08", out_root=tmp_path, stage="stage1")
    assert p_full.name == isr.NOON_CANDIDATES_NAME
    assert p_s1.name == isr.NOON_CANDIDATES_STAGE1_NAME
    assert p_full != p_s1 and p_full.exists() and p_s1.exists()   # 两份并存、不覆盖
    import json as _json
    assert _json.loads(p_s1.read_text(encoding="utf-8"))["stage"] == "stage1"
    assert _json.loads(p_full.read_text(encoding="utf-8"))["stage"] == "full"


def test_run_intraday_screen_emits_stage1_early(monkeypatch, tmp_path):
    """编排默认早产阶段1候选:cand_msg 先被 no_llm=True 调一次(早产)、再全量;两份候选都落盘。"""
    monkeypatch.setattr(isr.store, "get_view", lambda name, date=None: _view_8())
    calls = []
    rep = isr.run_intraday_screen(
        "2026-09-08", quotes={"000001": _Q}, codes=["000001"],
        run_screen_all_fn=lambda *a, **k: {"union": 1, "llm_subset": 1, "各策略入选": {}},
        cand_msg_fn=lambda as_of, no_llm=False: (calls.append(no_llm)
                                                 or {"候选池规模": 1, "统计": {}}),
        write_md=False, snapshot_root=tmp_path)
    assert calls[0] is True                                   # 阶段1.5 早产先 no_llm
    assert calls[-1] is False                                 # 阶段2 全量 no_llm=False
    assert rep["阶段1候选"] == str(isr.noon_candidates_path(
        "2026-09-08", out_root=tmp_path, filename=isr.NOON_CANDIDATES_STAGE1_NAME))
    assert isr.noon_candidates_path("2026-09-08", out_root=tmp_path,
                                    filename=isr.NOON_CANDIDATES_STAGE1_NAME).exists()
    assert isr.noon_candidates_path("2026-09-08", out_root=tmp_path).exists()  # 全量也在


def test_run_intraday_screen_can_disable_stage1(monkeypatch, tmp_path):
    """可关早产(emit_stage1_candidates=False):只落全量、cand_msg 只全量调一次。"""
    monkeypatch.setattr(isr.store, "get_view", lambda name, date=None: _view_8())
    calls = []
    isr.run_intraday_screen(
        "2026-09-08", quotes={"000001": _Q}, codes=["000001"],
        run_screen_all_fn=lambda *a, **k: {"union": 1, "llm_subset": 1, "各策略入选": {}},
        cand_msg_fn=lambda as_of, no_llm=False: (calls.append(no_llm)
                                                 or {"候选池规模": 1, "统计": {}}),
        write_md=False, emit_stage1_candidates=False, snapshot_root=tmp_path)
    assert calls == [False]                                   # 只全量一次(无早产 no_llm)
    assert not isr.noon_candidates_path("2026-09-08", out_root=tmp_path,
                                        filename=isr.NOON_CANDIDATES_STAGE1_NAME).exists()


def test_read_noon_candidates_stage_selection(monkeypatch, tmp_path):
    """消费侧 stage 选择:stage1 只读早产、full 只读全量、auto 全量优先。"""
    monkeypatch.setattr(isr.store, "get_view", lambda name, date=None: _view_8())
    isr.persist_noon_candidates("2026-09-08", out_root=tmp_path, stage="stage1")
    # 此时只有 stage1:stage='stage1' 命中,stage='full' 落空回退(无 md → None)
    assert isr.read_noon_candidates("2026-09-08", root=tmp_path, stage="stage1")["stage"] == "stage1"
    assert isr.read_noon_candidates("2026-09-08", root=tmp_path, selection_dir=tmp_path,
                                    stage="full") is None
    # 再落全量:auto 应优先全量
    isr.persist_noon_candidates("2026-09-08", out_root=tmp_path, stage="full")
    assert isr.read_noon_candidates("2026-09-08", root=tmp_path, stage="auto")["stage"] == "full"
    assert isr.read_noon_candidates("2026-09-08", root=tmp_path, stage="stage1")["stage"] == "stage1"
