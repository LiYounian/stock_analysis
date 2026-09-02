"""动量高位超买抑制层 A/B 回测逻辑语义锁(hermetic,手搓 panel,不触 IO)。

锁死:
  1. annotate_triggers 用 overheat_verdict 标注触发(DRY,不重实现阈值)。
  2. run_ab:A=纯动量 TopK;B=触发票分层沉底后 TopK(超买票挤出、健康票回填)。
  3. 被挤出票 = A有B无 且为触发票;B 踩雷率 < A 踩雷率(抑制减踩雷)。
  4. 不误杀:健康强票留在 B。

⚠️ 非投资建议。
"""
from __future__ import annotations

import pandas as pd
import pytest

from tools.backtest import backtest_momentum_overheat as bt


CFG = {
    "启用": True, "模式": "软降级", "降级系数": 0.3, "最少命中轴数": 2, "评估候选数": 50,
    "轴": {"超买共振": {"启用": True, "共振门槛": 2},
          "涨幅透支": {"启用": True, "bias20门槛": 20.0, "涨幅窗口": 10, "涨幅门槛%": 30.0}},
}


def _row(code, score, trig, crash, r):
    """构造一行 panel。trig=True 给超买透支特征;crash 决定踩雷;r=前瞻收益%。"""
    return {
        "date": "2024-06-03", "code": code, "score": score,
        "ob_verdict": "超买" if trig else "中性", "ob_reson": 2 if trig else 0,
        "bias20": 30.0 if trig else 5.0, "ret_n": 50.0 if trig else 8.0, "ret_window": 10,
        "dd": -20.0 if crash else -2.0, "踩雷": bool(crash),
        "r_1": r, "r_5": r, "r_10": r,
    }


def _panel():
    # HOT: 动量最高但超买透支 + 会踩雷 + 前瞻负;H1..H5 健康、前瞻正
    rows = [
        _row("HOT", 100.0, trig=True, crash=True, r=-8.0),
        _row("H1", 9.0, trig=False, crash=False, r=3.0),
        _row("H2", 8.0, trig=False, crash=False, r=2.0),
        _row("H3", 7.0, trig=False, crash=False, r=1.5),
        _row("H4", 6.0, trig=False, crash=False, r=1.0),
        _row("H5", 5.0, trig=False, crash=False, r=0.5),
    ]
    return pd.DataFrame(rows)


def test_annotate_triggers_dry():
    p = bt.annotate_triggers(_panel(), CFG)
    trig = dict(zip(p["code"], p["_trig"]))
    assert trig["HOT"] is True
    assert all(trig[c] is False for c in ["H1", "H2", "H3", "H4", "H5"])


def test_run_ab_squeezes_hot_and_backfills(monkeypatch):
    monkeypatch.setattr(bt, "_oh_cfg", lambda: CFG)
    ab = bt.run_ab(_panel(), topk=2, horizons=(1, 5, 10), step_reb=1, min_cross=3)
    # A TopK = {HOT, H1};B TopK = {H1, H2}(HOT 被沉底挤出,H2 回填)
    assert ab["被挤出票(A有B无)"]["n"] == 1                 # HOT
    assert ab["高位超买子样本(入选A且触发)"]["n"] == 1       # HOT
    assert ab["回填票(B有A无)"]["n"] == 1                    # H2
    # B 踩雷率 < A 踩雷率(A 含 HOT 会踩雷;B 全健康)
    assert ab["B(加抑制层)TopK"]["踩雷率%"] < ab["A(纯动量)TopK"]["踩雷率%"]
    assert ab["B(加抑制层)TopK"]["踩雷率%"] == pytest.approx(0.0)
    # B 前瞻收益 ≥ A(剔除踩雷负收益的 HOT)
    for N in (1, 5, 10):
        assert ab["B(加抑制层)TopK"][f"均收益{N}日%"] >= ab["A(纯动量)TopK"][f"均收益{N}日%"]
    # 不误杀:被挤出票(HOT)踩雷率高于保留票
    assert ab["被挤出票(A有B无)"]["踩雷率%"] > ab["保留票(A∩B)"]["踩雷率%"]


def test_run_ab_no_trigger_ab_identical(monkeypatch):
    """无触发票 → A/B TopK 完全一致(不回归)。"""
    monkeypatch.setattr(bt, "_oh_cfg", lambda: CFG)
    rows = [_row(f"H{i}", 10.0 - i, trig=False, crash=False, r=1.0) for i in range(6)]
    ab = bt.run_ab(pd.DataFrame(rows), topk=2, horizons=(1, 5, 10), step_reb=1, min_cross=3)
    assert ab["被挤出票(A有B无)"]["n"] == 0
    assert ab["A(纯动量)TopK"] == ab["B(加抑制层)TopK"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
