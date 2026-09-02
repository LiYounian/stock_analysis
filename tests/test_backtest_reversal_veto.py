"""反转否决层 A/B 回测器语义锁(踩雷标签 + 无未来函数 + A/B 剔除口径)。

锁死:
  1. 踩雷标签 = T+1..T+dd_horizon 最低价相对建仓价最大回撤 ≤ 阈值(纯价、可长历史算)。
  2. 无未来函数:因子只读 kdf[:t+1] 尾部;前瞻收益/踩雷只取 t 之后价作标签。
  3. run_ab:被否决高分票从 B 池剔除,A=全部高分池;踩雷率/收益分池统计正确。
⚠️ 非投资建议。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.backtest import backtest_reversal_veto as bt


def _mk_kline(closes, lows=None, turnovers=None):
    n = len(closes)
    dates = pd.date_range("2020-01-01", periods=n, freq="D").strftime("%Y-%m-%d")
    return pd.DataFrame({
        "date": dates, "close": closes,
        "low": lows if lows is not None else closes,
        "turnover": turnovers if turnovers is not None else [1.0] * n,
    })


def test_build_panel_trap_label(monkeypatch):
    """建仓后 10 日内出现 -20% 低点 → 踩雷=True;温和票 → False。"""
    # TRAP:t=10 建仓价=100,之后第3日 low=79(-21%)→ 踩雷
    trap_close = [100.0] * 12 + [100.0] * 10 + [100.0] * 20
    trap_low = list(trap_close)
    trap_low[10 + 3] = 79.0                                  # T+3 盘中 -21%
    # SAFE:建仓后小幅波动(最低 -5%)
    safe_close = [100.0] * 42
    safe_low = list(safe_close)
    for i in range(11, 22):
        safe_low[i] = 96.0                                  # 最多 -4%

    klines = {"TRAP": _mk_kline(trap_close, trap_low),
              "SAFE": _mk_kline(safe_close, safe_low)}
    monkeypatch.setattr(bt, "build_panel", bt.build_panel)   # keep
    from tools.collectors import market
    monkeypatch.setattr(market, "load_kline", lambda c: klines[c])

    panel = bt.build_panel(["TRAP", "SAFE"], rev_n=5, turn_n=5, horizons=(5, 10),
                           step=1, warmup=8, dd_horizon=10, dd_thresh=-15.0)
    assert not panel.empty
    # TRAP 在 t=10(date index 10)那行应踩雷
    trap_rows = panel[panel["code"] == "TRAP"]
    assert trap_rows["踩雷"].any()
    safe_rows = panel[panel["code"] == "SAFE"]
    assert not safe_rows["踩雷"].any()


def test_build_panel_no_future(monkeypatch):
    """无未来函数:改 t 之后的价只改被预测标签(r_N/踩雷),不改因子 rev/turn。"""
    base = [10.0 + 0.1 * i for i in range(40)]
    from tools.collectors import market
    monkeypatch.setattr(market, "load_kline", lambda c: _mk_kline(base))
    p1 = bt.build_panel(["X"], rev_n=5, turn_n=5, horizons=(5,), step=1,
                        warmup=8, dd_horizon=5)
    mutated = list(base)
    mutated[30:] = [999.0] * (len(mutated) - 30)             # 只改 t=30 之后
    monkeypatch.setattr(market, "load_kline", lambda c: _mk_kline(mutated))
    p2 = bt.build_panel(["X"], rev_n=5, turn_n=5, horizons=(5,), step=1,
                        warmup=8, dd_horizon=5)
    # t<=24 的行(其前瞻窗 t+5<=29 不触及被改段)因子与收益都应一致
    m = p1.merge(p2, on=["date", "code"], suffixes=("_1", "_2"))
    early = m[m["date"] <= _mk_kline(base)["date"].iloc[24]]
    assert np.allclose(early["rev_1"], early["rev_2"])
    assert np.allclose(early["r_5_1"], early["r_5_2"])


def test_run_ab_veto_splits_pools(monkeypatch):
    """run_ab:被否决高分票进「被否决高分票」池、从 B 剔除;A=全部。"""
    # 造 3 只票的横截面:每日都有 rev/turn,composite 排序稳定
    rng = np.random.default_rng(0)
    rows = []
    dates = [f"2020-01-{d:02d}" for d in range(1, 26)]
    for code, base in [("HOLLOW", 0.3), ("GOOD1", 0.1), ("GOOD2", 0.05)]:
        for d in dates:
            rows.append({"date": d, "code": code, "rev": base + rng.normal(0, 0.001),
                         "turn": -0.5, "dd": -5.0, "踩雷": (code == "HOLLOW"),
                         "r_5": 1.0, "r_10": 2.0})
    panel = pd.DataFrame(rows)

    # 只否决 HOLLOW
    def fake_extract(code, as_of, c=None):
        return {"is_st": code == "HOLLOW"}

    def fake_verdict(feats, c=None):
        trig = bool((feats or {}).get("is_st"))
        return {"触发": trig, "否决": trig, "剔除": False}

    from tools.strategy import reversal_veto as rv
    monkeypatch.setattr(rv, "extract_features", fake_extract)
    monkeypatch.setattr(rv, "veto_verdict", fake_verdict)

    res = bt.run_ab(panel, topk=3, horizons=(5, 10), min_cross=3)
    assert res["A(纯量价)"]["n"] > res["B(加否决层)"]["n"]    # B 少了被否决票
    assert res["否决票数"] >= 1
    assert res["被否决高分票"]["踩雷率%"] == 100.0            # 只否决了 HOLLOW(全踩雷)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
