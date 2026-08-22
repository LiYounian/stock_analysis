"""DB01 首板回调回测断言测试——锁住「为什么这么做」的语义,防未来重写误删规则。

覆盖:①防未来函数(命门,注入 t 之后数据不改变 T 时刻信号/regime)②涨停判定 board+date
aware ③成本模型 date-aware 印花 + round-trip 净<毛 ④成交概率折算(一字跌停顺延)
⑤ST 动态护栏 ⑥判定门槛 H1∧H4∧H5∧H6。

⚠️ 非投资建议。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.backtest import backtest_db01 as db


def _mk(dates, closes, opens=None, highs=None, lows=None, pct=None,
        amount=None, turnover=None):
    n = len(dates)
    opens = opens or list(closes)
    highs = highs or [max(o, c) for o, c in zip(opens, closes)]
    lows = lows or [min(o, c) for o, c in zip(opens, closes)]
    if pct is None:
        pct = [0.0] + [round((closes[i] / closes[i - 1] - 1) * 100, 4) for i in range(1, n)]
    amount = amount or [1e8] * n
    turnover = turnover or [5.0] * n
    return pd.DataFrame({"date": pd.to_datetime(dates), "open": opens, "high": highs,
                         "low": lows, "close": closes, "volume": [1e6] * n,
                         "amount": amount, "turnover": turnover, "pct_chg": pct})


# ── ① 防未来函数(命门)────────────────────────────────────────────
def test_涨停连板判定不看未来():
    """streak[t] 只由 ≤t 决定:改动 t 之后的 bar 不改变 t 及之前的 is_zt/streak。"""
    dates = pd.bdate_range("2022-01-03", periods=10)
    closes = [10, 11, 12.1, 12, 13, 13, 13, 13, 13, 13]  # 前两日涨停(+10%,+10%)
    df = _mk([str(d.date()) for d in dates], closes)
    a = db.annotate_limit(df, "600000")
    # 篡改最后 3 根为暴涨
    df2 = df.copy()
    df2.loc[7:, "close"] = [99, 120, 150]
    df2.loc[7:, "pct_chg"] = [200, 21, 25]
    b = db.annotate_limit(df2, "600000")
    # t=0..6 的 is_zt / streak 必须完全一致(不被未来污染)
    assert (a["is_zt"].iloc[:7] == b["is_zt"].iloc[:7]).all()
    assert (a["streak"].iloc[:7] == b["streak"].iloc[:7]).all()


def test_信号扫描不看未来():
    """scan_candidates 在 t 的判定只读 ≤t;注入 t 之后极端数据不改变已产候选。"""
    dates = [str(d.date()) for d in pd.bdate_range("2022-01-03", periods=70)]
    closes = [10.0] * 60 + [10.0, 11.0] + [11.0] * 8   # 第 61 根首板(+10%)
    df = _mk(dates, closes)
    kl = {"600000": db.annotate_limit(df, "600000")}
    reg = db.build_regime(kl)
    # 强制放开门(子样本 N_lu 小),只测信号时序不变性
    reg["gate_open"] = True
    c1 = db.scan_candidates(kl, reg, require_gate=True)
    # 篡改末尾
    df2 = df.copy(); df2.loc[65:, "close"] = 999; df2.loc[65:, "pct_chg"] = 500
    kl2 = {"600000": db.annotate_limit(df2, "600000")}
    c2 = db.scan_candidates(kl2, reg, require_gate=True)
    ts1 = {(c["code"], c["t"]) for c in c1 if c["t"] <= 64}
    ts2 = {(c["code"], c["t"]) for c in c2 if c["t"] <= 64}
    assert ts1 == ts2, "t≤64 的候选不应被 t≥65 的篡改改变"


# ── ② 涨停判定 board + date aware ──────────────────────────────────
def test_涨停阈值board_date_aware():
    d19 = pd.Timestamp("2019-01-01"); d21 = pd.Timestamp("2021-01-01")
    assert db.limit_up_threshold("600000", d21) == 9.8       # 主板恒 9.8
    assert db.limit_up_threshold("300001", d19) == 9.8       # 创业板 2020-08-24 前 10%
    assert db.limit_up_threshold("300001", d21) == 19.8      # 之后 20%
    assert db.limit_up_threshold("688001", d21) == 19.8      # 科创 20%
    assert db.limit_up_threshold("688001", pd.Timestamp("2019-06-01")) == 9.8


def test_ST涨停不被判首板():
    """ST 股 ±5% 涨停(pct_chg≈5)< 主板 9.8 阈值 → is_zt=False → 天然不进池。"""
    dates = [str(d.date()) for d in pd.bdate_range("2022-01-03", periods=5)]
    df = _mk(dates, [10, 10.5, 11.02, 11.5, 12], pct=[0, 5.0, 4.95, 4.35, 4.35])
    a = db.annotate_limit(df, "600000")
    assert not a["is_zt"].any(), "±5% 涨幅不应被判为主板涨停"


# ── ③ 成本模型 ─────────────────────────────────────────────────────
def test_印花税date_aware():
    assert db.stamp_tax_rate(pd.Timestamp("2023-01-01")) == 0.0010
    assert db.stamp_tax_rate(pd.Timestamp("2024-01-01")) == 0.0005


def test_round_trip净小于毛():
    """同买卖价:净收益必 < 毛收益(双边滑点+佣金+过户费+印花)。"""
    net = db._round_trip_net(10.0, 10.0, pd.Timestamp("2024-01-01"))
    assert net < 0, "平价进出扣费后必亏(成本存在)"
    # 毛 0% 时净 ≈ -(2*slip + 2*comm + 2*transfer + stamp)
    expect = -(2 * db._SLIP + 2 * db._COMM + 2 * db._TRANSFER + 0.0005)
    assert abs(net - expect) < 1e-3


def test_成本参数为挖掘者精确口径():
    """锁死成本口径:佣金 0.025%/side、过户费 0.001%/side,防未来误改回默认。"""
    assert db._COMM == 0.00025
    assert db._TRANSFER == 0.00001
    assert db._SLIP == 0.0020


def test_基线A与DB01同成本():
    """H1 apples-to-apples:两者走同一 _round_trip_net,成本参数同。"""
    d = _mk([str(x.date()) for x in pd.bdate_range("2022-01-03", periods=5)],
            [10, 10, 10, 10, 10])
    a = db.annotate_limit(d, "600000")
    tr = db.simulate_trade(a, 1, apply_r_filter=True)
    base = db.simulate_trade(a, 1, apply_r_filter=False)
    # r=0 落在 [-5%,+3%],两者都入场且净收益一致(同价同成本)
    assert tr["入场"] and base["入场"]
    assert tr["net"] == base["net"]


# ── ④ 成交概率折算(一字跌停顺延)──────────────────────────────────
def test_一字跌停顺延卖出():
    """计划卖出日(T+2)一字跌停 → 顺延到下一可成交日开盘,如实计被迫持有损益。"""
    dates = [str(x.date()) for x in pd.bdate_range("2022-01-03", periods=6)]
    # t=1 买入(T+1=idx2 开盘);T+2=idx3 一字跌停(open==high==low 且 -10%)→ 顺延 idx4
    closes = [10, 10, 10, 9.0, 9.2, 9.2]
    opens = [10, 10, 10, 9.0, 9.1, 9.2]
    highs = [10, 10, 10, 9.0, 9.3, 9.2]
    lows = [10, 10, 10, 9.0, 9.0, 9.2]
    pct = [0, 0, 0, -10.0, 2.2, 0]
    df = _mk(dates, closes, opens=opens, highs=highs, lows=lows, pct=pct)
    a = db.annotate_limit(df, "600000")
    assert bool(a["is_yizi_down"].iloc[3]), "idx3 应判一字跌停"
    tr = db.simulate_trade(a, 1, apply_r_filter=False)
    assert tr["sell_delayed"], "应顺延卖出"
    assert tr["sell_date"] == dates[4], "顺延到 idx4 开盘卖"


# ── ④b 连板计数(§3 情绪门)停牌≥5交易日清零 ───────────────────────
def test_连板停牌5交易日清零():
    """连续涨停中停牌 ≥5 交易日 → 复牌涨停连板清零重算;<5 不中断。"""
    cal = pd.bdate_range("2022-01-03", periods=40)
    cal_idx = {dt: i for i, dt in enumerate(cal)}
    # 场景1:idx0,1 涨停(连板2);跳过 6 个交易日(停牌)后 idx8 涨停 → 应清零为 1
    dates = [cal[0], cal[1], cal[8], cal[9]]
    is_zt = np.array([True, True, True, True])
    sk = db._streak_with_halt(dates, is_zt, cal_idx)
    assert list(sk) == [1, 2, 1, 2], f"停牌≥5清零失败:{list(sk)}"
    # 场景2:停牌仅 3 交易日(<5)→ 不中断
    dates2 = [cal[0], cal[1], cal[4], cal[5]]
    sk2 = db._streak_with_halt(dates2, is_zt, cal_idx)
    assert list(sk2) == [1, 2, 3, 4], f"停牌<5不应中断:{list(sk2)}"


# ── ⑤ ST 动态护栏 ──────────────────────────────────────────────────
def test_ST动态护栏识别():
    dates = [str(x.date()) for x in pd.bdate_range("2022-01-03", periods=70)]
    pct = [0.0] * 70
    for i in (10, 20, 30):    # 3 次 ±5% 特征
        pct[i] = 5.0
    closes = [10.0]
    for i in range(1, 70):
        closes.append(round(closes[-1] * (1 + pct[i] / 100), 4))
    df = _mk(dates, closes, pct=pct)
    a = db.annotate_limit(df, "600000")
    assert db.is_st_like(a, 35), "近60日≥3次±5%且无>9.5% → 判疑似ST"
    # 若期间有一次真涨停(>9.5%)则不判 ST
    pct[25] = 10.0
    closes2 = [10.0]
    for i in range(1, 70):
        closes2.append(round(closes2[-1] * (1 + pct[i] / 100), 4))
    a2 = db.annotate_limit(_mk(dates, closes2, pct=pct), "600000")
    assert not db.is_st_like(a2, 35), "有真涨停 → 非 ST 特征"


# ── ⑥ 判定门槛语义 ─────────────────────────────────────────────────
def test_成立门槛为H1且H4且H5且H6():
    """综合判定 = H1∧H4∧H5∧H6;缺任一即不成立;H1真∧H6假 → 判不可交易。"""
    base = {
        "H1_回调择时净增量": {"成立": True},
        "H4_成本存活全A": {"成立": True},
        "H5_样本外方向一致": {"方向一致且OOS>0": True},
        "H6_可交易层存活(命门)": {"成交额前50%": {"净超额显著>0": True}},
    }
    assert db._final_verdict(base)["成立门槛H1∧H4∧H5∧H6"] is True
    # H6 否定 → 不成立 且 判「不可交易」
    b2 = {**base, "H6_可交易层存活(命门)": {"成交额前50%": {"净超额显著>0": False}}}
    v2 = db._final_verdict(b2)
    assert v2["成立门槛H1∧H4∧H5∧H6"] is False
    assert "不可交易" in v2["结论"]
    # H4 否定 → 不成立
    b3 = {**base, "H4_成本存活全A": {"成立": False}}
    assert db._final_verdict(b3)["成立门槛H1∧H4∧H5∧H6"] is False


def test_幸存者偏差声明常驻():
    """结论必带幸存者偏差声明(退市股不含=高估),防未来重写误删诚实前置。"""
    v = db._final_verdict({
        "H1_回调择时净增量": {"成立": False},
        "H4_成本存活全A": {"成立": False},
        "H5_样本外方向一致": {"方向一致且OOS>0": False},
        "H6_可交易层存活(命门)": {"成交额前50%": {"净超额显著>0": False}},
    })
    assert "幸存者偏差声明" in v and "高估" in v["幸存者偏差声明"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
