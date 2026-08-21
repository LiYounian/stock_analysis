"""策略 S03「最大范围选股」单测:锁 6 条规则语义 + 历史门槛 + 北交所排除。

纯合成 K 线,不触网、不读盘。断言锁住"为什么选中/为什么排除",防未来重写误删规则。
"""
import numpy as np
import pandas as pd

from tools.pipeline import screen_max_range as mr


def _frame(closes, lows=None):
    """按收盘价序列造规整 K 线(date 用工作日,open=close,high=close,low=close-1)。"""
    n = len(closes)
    dates = pd.bdate_range(end="2026-08-14", periods=n)
    closes = np.asarray(closes, dtype=float)
    lows = closes - 1.0 if lows is None else np.asarray(lows, dtype=float)
    return pd.DataFrame({
        "date": dates, "open": closes, "high": closes, "low": lows, "close": closes,
        "volume": np.full(n, 1e6), "amount": closes * 1e6,
        "turnover": np.full(n, 1.0), "pct_chg": np.zeros(n),
    })


def _good_closes():
    """满足全部条件的收盘序列:长期 100,第 245 根跳 +8%(大阳),尾部缓升到 108.8(高位)。"""
    c = [100.0] * 245 + [108.0, 108.2, 108.4, 108.8, 108.85]
    return c[:250] if len(c) >= 250 else c + [108.85] * (250 - len(c))


def test_formula_selects_matching_stock():
    kdf = _frame(_good_closes())
    r = mr.screen_latest(kdf, code="600519")
    assert r["SELECT"] is True, r
    assert r["明细"]["32日涨超6%次数"] >= 1
    # 高位:距高点 ≥ 82%
    assert r["明细"]["距250日高点%"] >= 82.0


def test_history_short_not_selected():
    kdf = _frame(_good_closes()[:249])   # 249 < 250 → 历史不足
    assert mr.screen_latest(kdf, code="600519")["SELECT"] is False


def test_bj_prefix_excluded_but_002_kept():
    kdf = _frame(_good_closes())
    # 002(中小板)保留 → 其余条件满足则入选
    assert mr.screen_latest(kdf, code="002001")["SELECT"] is True
    # 北交所前缀 8 / 4 排除(C5 False → SELECT False)
    r8 = mr.screen_latest(kdf, code="830001")
    assert r8["C5_非北交所"] is False and r8["SELECT"] is False
    assert mr.screen_latest(kdf, code="430001")["C5_非北交所"] is False


def test_big_retrace_rejected():
    """当日相对昨收大跌(回撤 > 4%)→ C4 False → 不选(即便其余满足)。"""
    c = _good_closes()
    c[-1] = c[-2] * 0.90          # 当日跌 10%
    r = mr.screen_latest(_frame(c), code="600519")
    assert r["C4_当日回撤"] is False and r["SELECT"] is False


def test_below_ma_rejected():
    """收盘跌破均线(全程走平后最后一根不再是高位)→ 距高点/均线条件不满足 → 不选。"""
    kdf = _frame([100.0] * 250)   # 全程走平:无大阳、close 不 > MA(等于)
    r = mr.screen_latest(kdf, code="600519")
    assert r["SELECT"] is False
