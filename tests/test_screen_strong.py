"""策略 S05「最强选股」单测:规则语义 + 筹码依赖 + **Tushare 门控**(无 token 不出)。

纯合成 K 线 + mock 筹码,不触网。锁住:①六均线多头/近期连涨/高位区间/筹码获利四条;
②chip=None(无筹码)→ 必不入选(Tushare-only,不用免费源硬凑);③未配 token → run 返回
present=False + 需 Tushare 提示,不产出选股。
"""
import numpy as np
import pandas as pd

from tools.collectors import market, tushare_daily
from tools.pipeline import screen_strong as ss
from tools.store import repo as store


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
    """未配 TUSHARE_TOKEN → run 返回 present=False + 需 Tushare 提示,不产出选股(不硬凑免费源)。"""
    monkeypatch.setattr(store, "put_view", lambda *a, **k: None)   # 隔离,不写 data/analysis
    monkeypatch.setattr(store, "set_active_date", lambda *a, **k: None)
    monkeypatch.setattr(tushare_daily, "is_configured", lambda: False)
    v = ss.run_strong_screen(["000001", "600000"], as_of="2026-08-21", fetch=False)
    assert v["present"] is False and v.get("需要Tushare") is True and v["入选数"] == 0


def test_run_with_token_and_chip_produces_pick(monkeypatch):
    """配 token + 筹码可取 + 命中票 → 产出入选。"""
    monkeypatch.setattr(store, "put_view", lambda *a, **k: None)   # 隔离,不写 data/analysis
    monkeypatch.setattr(store, "set_active_date", lambda *a, **k: None)
    monkeypatch.setattr(tushare_daily, "is_configured", lambda: True)
    monkeypatch.setattr(ss, "_chip_map", lambda as_of: {"000001": {"winner_rate": 97.0, "cost_95pct": 1.0}})
    monkeypatch.setattr(market, "load_kline_recent", lambda code, rows=None: _strong_frame())
    v = ss.run_strong_screen(["000001"], as_of="2026-08-21", fetch=False)
    assert v["present"] is True and v["入选数"] == 1 and v["入选清单"][0]["code"] == "000001"
