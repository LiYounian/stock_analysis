"""策略 S05「最强选股」单测:规则语义 + 筹码取数源开关(local 默认 / tushare 回退)。

纯合成 K 线 + mock 筹码,不触网。锁住:
  ①六均线多头/近期连涨/高位区间/筹码获利四条 signal_at 语义;
  ②local 路径(默认)不依赖 Tushare:未配 token 也出 present=True,量纲(获利比例×100)正确;
  ③开关置 tushare 可回退旧行为:未配 token → present=False + 需 Tushare 提示,不产出选股。
"""
import numpy as np
import pandas as pd

from tools.collectors import chip, market, tushare_daily
from tools.config import strategy
from tools.pipeline import screen_strong as ss
from tools.store import repo as store


def _use_tushare(monkeypatch):
    """把 ④筹码源固定到 tushare(旧行为回退),供门控/告警回归用例复用。"""
    monkeypatch.setattr(strategy, "STRONG_CHIP_SOURCE", "tushare")


def _use_local(monkeypatch):
    """把 ④筹码源固定到 local(默认路径),供本地筹码用例复用。"""
    monkeypatch.setattr(strategy, "STRONG_CHIP_SOURCE", "local")


def _strong_frame(n=260):
    """稳步上升(六均线多头 + 高位区间)+ 末段两日 +5%(近期连涨)。"""
    closes = [10.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * 1.003)
    closes = np.array(closes, dtype=float)
    closes[-8:] *= 1.05        # 倒数第 8 根 +5% 大涨
    closes[-3:] *= 1.05        # 倒数第 3 根 +5% 大涨
    dates = pd.bdate_range(end="2026-08-14", periods=n)
    return pd.DataFrame({
        "date": dates, "open": closes, "high": closes + 0.01, "low": closes - 0.01,
        "close": closes, "volume": np.full(n, 1e6), "amount": closes * 1e6,
        "turnover": np.full(n, 1.0), "pct_chg": np.zeros(n),
    })


def test_selects_with_high_winner_rate():
    kdf = _strong_frame()
    r = ss.screen_latest(kdf, chip={"winner_rate": 96.0, "cost_95pct": 1.0})
    assert r["C1_六均线多头"] and r["C2_近期连涨"] and r["C3_高位区间"] and r["C4_筹码获利"]
    assert r["SELECT"] is True


def test_win_via_cost95_branch():
    """winner_rate 不足但 HIGH ≥ cost_95pct → ④仍成立(近似 WINNER(HIGH))。"""
    kdf = _strong_frame()
    high_last = float(kdf["high"].iloc[-1])
    r = ss.screen_latest(kdf, chip={"winner_rate": 50.0, "cost_95pct": high_last - 0.001})
    assert r["C4_筹码获利"] is True and r["SELECT"] is True


def test_no_chip_never_selected():
    """无筹码(chip=None,免费源情形)→ ④False → 必不入选(Tushare-only)。"""
    kdf = _strong_frame()
    r = ss.screen_latest(kdf, chip=None)
    assert r["C4_筹码获利"] is False and r["SELECT"] is False


def test_history_short_not_selected():
    assert ss.screen_latest(_strong_frame(n=200), chip={"winner_rate": 99.0})["SELECT"] is False


def test_run_without_token_returns_notice(monkeypatch):
    """开关=tushare 且未配 TOKEN → run 返回 present=False + 需 Tushare 提示,不产出选股(不硬凑免费源)。"""
    _use_tushare(monkeypatch)
    monkeypatch.setattr(store, "put_view", lambda *a, **k: None)   # 隔离,不写 data/analysis
    monkeypatch.setattr(store, "set_active_date", lambda *a, **k: None)
    monkeypatch.setattr(tushare_daily, "is_configured", lambda: False)
    v = ss.run_strong_screen(["000001", "600000"], as_of="2026-08-21", fetch=False)
    assert v["present"] is False and v.get("需要Tushare") is True and v["入选数"] == 0


def test_run_with_token_and_chip_produces_pick(monkeypatch):
    """开关=tushare + 配 token + 筹码可取 + 命中票 → 产出入选。"""
    _use_tushare(monkeypatch)
    monkeypatch.setattr(store, "put_view", lambda *a, **k: None)   # 隔离,不写 data/analysis
    monkeypatch.setattr(store, "set_active_date", lambda *a, **k: None)
    monkeypatch.setattr(tushare_daily, "is_configured", lambda: True)
    monkeypatch.setattr(ss, "_chip_map", lambda as_of: {"000001": {"winner_rate": 97.0, "cost_95pct": 1.0}})
    monkeypatch.setattr(market, "load_kline_recent", lambda code, rows=None: _strong_frame())
    v = ss.run_strong_screen(["000001"], as_of="2026-08-21", fetch=False)
    assert v["present"] is True and v["入选数"] == 1 and v["入选清单"][0]["code"] == "000001"


def test_chip_gated_by_token_not_by_fetch(monkeypatch):
    """回归锁:筹码 cyq_perf 的获取只由 token(is_configured)门控,**与 fetch 参数解耦**。

    fetch 只管 OHLC 日线是否重拉;筹码链路(_chip_map → tushare_daily.fetch_chip)必须
    与 fetch 无关。防以后有人误把筹码获取挂到 fetch 开关上(fetch=False 时不取筹码),
    导致每日环境跑 fetch=False 时策略9 恒空(历史根因是缺 token,不是缺 fetch)。

    做法:直接打桩 tushare_daily.fetch_chip 记录调用,分别以 fetch=False / fetch=True 跑,
    断言两种 fetch 下 fetch_chip 都被调用、且都产出同样的入选结果。
    """
    _use_tushare(monkeypatch)
    calls: list[str] = []

    def _fake_fetch_chip(as_of):
        calls.append(as_of)
        return pd.DataFrame({"code": ["000001"], "winner_rate": [97.0], "cost_95pct": [1.0]})

    monkeypatch.setattr(store, "put_view", lambda *a, **k: None)   # 隔离,不写 data/analysis
    monkeypatch.setattr(store, "set_active_date", lambda *a, **k: None)
    monkeypatch.setattr(tushare_daily, "is_configured", lambda: True)
    monkeypatch.setattr(tushare_daily, "fetch_chip", _fake_fetch_chip)  # 真链路 _chip_map→fetch_chip
    monkeypatch.setattr(market, "load_kline_recent", lambda code, rows=None: _strong_frame())

    v_no_fetch = ss.run_strong_screen(["000001"], as_of="2026-08-21", fetch=False)
    assert len(calls) == 1, "fetch=False 时筹码链路仍必须被调用(筹码与 fetch 解耦)"
    assert v_no_fetch["present"] is True and v_no_fetch["入选数"] == 1

    v_fetch = ss.run_strong_screen(["000001"], as_of="2026-08-21", fetch=True)
    assert len(calls) == 2, "fetch=True 时筹码链路同样被调用"
    # 筹码逻辑对 fetch 不敏感:两种 fetch 下入选结果一致
    assert v_fetch["入选数"] == v_no_fetch["入选数"] == 1
    assert v_fetch["入选清单"][0]["code"] == v_no_fetch["入选清单"][0]["code"] == "000001"


def test_warns_when_token_set_but_chip_missing(monkeypatch, caplog):
    """回归锁 + 静默告警:配了 token 但筹码取不到 → 必打 WARNING(区分三分法)。

    ③配 token 但 chip 空 → WARNING(唯一告警情形);对照:
    ①无 token / ②配 token 但入选0 → 均不得误报 WARNING(另见下方 no-token 与 has-pick 用例)。
    """
    _use_tushare(monkeypatch)
    monkeypatch.setattr(store, "put_view", lambda *a, **k: None)
    monkeypatch.setattr(store, "set_active_date", lambda *a, **k: None)
    monkeypatch.setattr(tushare_daily, "is_configured", lambda: True)
    monkeypatch.setattr(ss, "_chip_map", lambda as_of: None)   # 配了 token 却取不到筹码
    with caplog.at_level("WARNING", logger="pipeline.screen_strong"):
        v = ss.run_strong_screen(["000001"], as_of="2026-08-21", fetch=False)
    assert v["present"] is False and v.get("需要Tushare") is True
    warns = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("TUSHARE_TOKEN" in m and "cyq_perf" in m for m in warns), \
        "配 token 但筹码取不到时必须打 WARNING 提示 token 可能失效"


def test_no_warn_when_no_token(monkeypatch, caplog):
    """三分法①(tushare 回退):未配 token → 正常占位,**不得**误报 WARNING。"""
    _use_tushare(monkeypatch)
    monkeypatch.setattr(store, "put_view", lambda *a, **k: None)
    monkeypatch.setattr(store, "set_active_date", lambda *a, **k: None)
    monkeypatch.setattr(tushare_daily, "is_configured", lambda: False)
    with caplog.at_level("WARNING", logger="pipeline.screen_strong"):
        ss.run_strong_screen(["000001"], as_of="2026-08-21", fetch=False)
    assert not [r for r in caplog.records if r.levelname == "WARNING"], \
        "未配 token 是正常占位,不该告警"


def test_no_warn_when_token_and_zero_picks(monkeypatch, caplog):
    """三分法②(tushare 回退):配 token + 筹码可取但当天0票入选 → 合法结果,**不得**误报 WARNING。"""
    _use_tushare(monkeypatch)
    monkeypatch.setattr(store, "put_view", lambda *a, **k: None)
    monkeypatch.setattr(store, "set_active_date", lambda *a, **k: None)
    monkeypatch.setattr(tushare_daily, "is_configured", lambda: True)
    # 筹码取得到,但 winner_rate 不足且 cost_95pct 极高 → C4 恒 False → 0 入选
    monkeypatch.setattr(ss, "_chip_map", lambda as_of: {"000001": {"winner_rate": 10.0, "cost_95pct": 1e9}})
    monkeypatch.setattr(market, "load_kline_recent", lambda code, rows=None: _strong_frame())
    with caplog.at_level("WARNING", logger="pipeline.screen_strong"):
        v = ss.run_strong_screen(["000001"], as_of="2026-08-21", fetch=False)
    assert v["present"] is True and v["入选数"] == 0
    assert not [r for r in caplog.records if r.levelname == "WARNING"], \
        "配 token 且筹码可取、只是当天0票,是合法结果,不该告警"


# ============================================================================
# 阶段二:本地筹码路径(默认 STRONG_CHIP_SOURCE=local)——零 Tushare 依赖 + 量纲锁
# ============================================================================
def _no_tushare(monkeypatch):
    """把 Tushare 入口全部打成"一碰就炸",证明 local 路径绝不触碰 Tushare。"""
    def _boom(*a, **k):
        raise AssertionError("local 路径不得调用 Tushare")
    monkeypatch.setattr(tushare_daily, "is_configured", _boom)
    monkeypatch.setattr(tushare_daily, "fetch_chip", _boom)
    monkeypatch.setattr(ss, "_chip_map", _boom)


def test_local_produces_pick_without_tushare(monkeypatch):
    """核心:STRONG_CHIP_SOURCE=local 且**未配/不碰 Tushare** 时,S05 仍能 present=True 正常出结果。

    证明 ④筹码取数已切到本地 chip 推演,不再依赖 cyq_perf、不写"需 Tushare"占位。
    """
    _use_local(monkeypatch)
    _no_tushare(monkeypatch)
    monkeypatch.setattr(store, "put_view", lambda *a, **k: None)
    monkeypatch.setattr(store, "set_active_date", lambda *a, **k: None)
    monkeypatch.setattr(market, "load_kline_recent", lambda code, rows=None: _strong_frame())
    # 本地 chip 推演:获利比例 0.98(小数)→ 适配层 ×100 = 98% > 阈值 95 → ④成立
    monkeypatch.setattr(chip, "summarize_asof",
                        lambda df, as_of: {"获利比例": 0.98, "成本区间上沿": 1.0, "降级": False})
    v = ss.run_strong_screen(["000001"], as_of="2026-08-21", fetch=False)
    assert v["present"] is True and "需要Tushare" not in v
    assert v["入选数"] == 1 and v["入选清单"][0]["code"] == "000001"
    # 明细里的 winner_rate 必须是百分数量纲(98),不是小数(0.98)
    assert v["入选清单"][0]["明细"]["winner_rate"] == 98.0


def test_local_dimension_lock(monkeypatch):
    """量纲锁:获利比例(0~1 小数)经适配层 ×100 后与阈值(95.0)同量纲。

    · 获利比例=0.98 → 98% > 95 → ④True → 入选;
    · 获利比例=0.90 → 90% < 95 且 cost95 极高使 HIGH<cost95 → ④False → 不入选。
    防 %↔小数写反(写反会让 0.9x 永远 < 95 → ④恒 False)。
    """
    _use_local(monkeypatch)
    monkeypatch.setattr(store, "put_view", lambda *a, **k: None)
    monkeypatch.setattr(store, "set_active_date", lambda *a, **k: None)
    monkeypatch.setattr(market, "load_kline_recent", lambda code, rows=None: _strong_frame())

    # 命中:0.98×100=98>95
    monkeypatch.setattr(chip, "summarize_asof",
                        lambda df, as_of: {"获利比例": 0.98, "成本区间上沿": 1e9, "降级": False})
    v_hit = ss.run_strong_screen(["000001"], as_of="2026-08-21", fetch=False)
    assert v_hit["入选数"] == 1, "获利比例0.98(=98%)应过阈值95 → 入选(量纲对)"

    # 不命中:0.90×100=90<95,且成本区间上沿极高 → HIGH<cost95 → cost95 分支也不触发
    monkeypatch.setattr(chip, "summarize_asof",
                        lambda df, as_of: {"获利比例": 0.90, "成本区间上沿": 1e9, "降级": False})
    v_miss = ss.run_strong_screen(["000001"], as_of="2026-08-21", fetch=False)
    assert v_miss["入选数"] == 0, "获利比例0.90(=90%)应低于阈值95 → 不入选"


def test_local_cost95_branch(monkeypatch):
    """cost95 映射锁:成本区间上沿 → cost_95pct,HIGH≥成本区间上沿 分支按预期触发。

    获利比例不足(90%<95),但成本区间上沿 ≤ 当日最高价 → ④仍成立(近似 WINNER(HIGH))。
    """
    _use_local(monkeypatch)
    monkeypatch.setattr(store, "put_view", lambda *a, **k: None)
    monkeypatch.setattr(store, "set_active_date", lambda *a, **k: None)
    kdf = _strong_frame()
    high_last = float(kdf["high"].iloc[-1])
    monkeypatch.setattr(market, "load_kline_recent", lambda code, rows=None: kdf)
    monkeypatch.setattr(chip, "summarize_asof",
                        lambda df, as_of: {"获利比例": 0.90, "成本区间上沿": high_last - 0.001,
                                           "降级": False})
    v = ss.run_strong_screen(["000001"], as_of="2026-08-21", fetch=False)
    assert v["入选数"] == 1, "HIGH≥成本区间上沿 → ④成立 → 入选(cost95 映射正确)"


def test_local_no_turnover_degrades_not_selected(monkeypatch):
    """降级样本:本地推演拿不到获利比例(换手缺失/数据不足)→ chip=None → ④False → 不入选。

    不静默伪造"有筹码",筹码不可用计入 view['筹码不可用数']。
    """
    _use_local(monkeypatch)
    _no_tushare(monkeypatch)
    monkeypatch.setattr(store, "put_view", lambda *a, **k: None)
    monkeypatch.setattr(store, "set_active_date", lambda *a, **k: None)
    monkeypatch.setattr(market, "load_kline_recent", lambda code, rows=None: _strong_frame())
    monkeypatch.setattr(chip, "summarize_asof",
                        lambda df, as_of: {"获利比例": None, "成本区间上沿": None, "降级": True})
    v = ss.run_strong_screen(["000001"], as_of="2026-08-21", fetch=False)
    assert v["present"] is True and v["入选数"] == 0 and v["筹码不可用数"] == 1


def test_local_adapter_maps_units_directly(monkeypatch):
    """直测适配层 _local_chip_of:获利比例×100→winner_rate、成本区间上沿→cost_95pct。"""
    monkeypatch.setattr(chip, "summarize",
                        lambda df: {"获利比例": 0.965, "成本区间上沿": 12.34})
    out = ss._local_chip_of(_strong_frame(), as_of=None)
    assert out["winner_rate"] == 96.5 and out["cost_95pct"] == 12.34
