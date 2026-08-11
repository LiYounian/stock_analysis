"""策略 4「动量组合」全A screener 单测(hermetic,不触网)。

锁死红线:
  · 复用 momentum.combo_momentum_screen(仅 A 腿,纯价格动量);
  · 强势(上行、R²高、拉普拉斯'买')票入选,弱势(下行)不入选;
  · view schema 完整 + 标注仅 A 腿口径;
  · 空池 / 历史不足不崩;
  · 防未来函数:动量打分只看尾部 lookback+1 根,更早历史不影响截至当日结论。
"""
import numpy as np
import pandas as pd

from tools.pipeline import screen_momentum as mom
from tools.store import repo as store


def _kdf(closes: list[float], start="2024-01-01") -> pd.DataFrame:
    n = len(closes)
    dates = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"date": dates, "open": closes, "high": closes,
                         "low": closes, "close": closes, "volume": [1000.0] * n})


def _uptrend(n=40, r=0.01):
    return [round(100.0 * (1 + r) ** i, 4) for i in range(n)]


def _downtrend(n=40, r=0.01):
    return [round(100.0 * (1 - r) ** i, 4) for i in range(n)]


def _flat(n=40):
    return [100.0] * n


# ———————————————————— 强势入选 / 弱势不入选 ————————————————————
def test_uptrend_selected_downtrend_not(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    kl = {"UP": _kdf(_uptrend()), "DOWN": _kdf(_downtrend()), "FLAT": _kdf(_flat())}
    monkeypatch.setattr(mom.market, "load_kline", lambda c: kl[c])
    v = mom.run_momentum_screen(["UP", "DOWN", "FLAT"], as_of="2024-06-01",
                                fetch=False, top_k=30)
    picked = [x["code"] for x in v["入选清单"]]
    assert "UP" in picked
    assert "DOWN" not in picked          # 下行 → 拉普拉斯'卖'闸门滤除
    assert "FLAT" not in picked          # 横盘 → 无'买'信号 / 动量弱


def test_top_k_caps_selection(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    # 多只强势票,top_k=2 → 只取前 2
    kl = {f"UP{i}": _kdf(_uptrend(r=0.005 + 0.001 * i)) for i in range(5)}
    monkeypatch.setattr(mom.market, "load_kline", lambda c: kl[c])
    v = mom.run_momentum_screen(list(kl), as_of="2024-06-01", fetch=False, top_k=2)
    assert v["入选数"] == 2 and v["top_k"] == 2


# ———————————————————— view schema 完整 + A 腿口径标注 ————————————————————
def test_view_schema_and_a_leg_only(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    kl = {"UP": _kdf(_uptrend())}
    monkeypatch.setattr(mom.market, "load_kline", lambda c: kl[c])
    v = mom.run_momentum_screen(["UP"], as_of="2024-06-01", fetch=False)
    for key in ("as_of", "策略", "口径", "扫描数", "有效样本", "跳过数(历史不足)",
                "入选数", "top_k", "入选清单", "规则", "防未来函数"):
        assert key in v
    assert "A 腿" in v["口径"] and "B 腿" in v["口径"]
    assert v["策略"].startswith("动量组合")
    # 入选项含动量明细
    assert "R²" in v["入选清单"][0]["特征"]
    got = store.get_view("动量组合", date="2024-06-01")
    assert got["入选数"] == v["入选数"]


# ———————————————————— 空池 / 历史不足不崩 ————————————————————
def test_empty_pool_no_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    v = mom.run_momentum_screen([], as_of="2024-06-01", fetch=False)
    assert v["入选数"] == 0 and v["扫描数"] == 0


def test_insufficient_history_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    kl = {"SHORT": _kdf(_uptrend(n=10))}          # 10 根 < 回看+1=26
    monkeypatch.setattr(mom.market, "load_kline", lambda c: kl[c])
    v = mom.run_momentum_screen(["SHORT"], as_of="2024-06-01", fetch=False)
    assert v["入选数"] == 0 and v["跳过数(历史不足)"] == 1 and v["有效样本"] == 0


def test_missing_kline_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")

    def raise_nf(c):
        raise FileNotFoundError(c)

    monkeypatch.setattr(mom.market, "load_kline", raise_nf)
    v = mom.run_momentum_screen(["NONE"], as_of="2024-06-01", fetch=False)
    assert v["入选数"] == 0 and v["跳过数(历史不足)"] == 1


# ———————————————————— 防未来函数:动量只看尾部窗口 ————————————————————
def test_no_lookahead_only_trailing_window(monkeypatch, tmp_path):
    """更早的历史(尾部 lookback+1 根之外)不影响截至当日的动量打分/入选。"""
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    tail = _uptrend(n=30)                          # 尾部一致
    short = _kdf(tail)
    long = _kdf(_flat(20) + tail)                  # 前面塞 20 根横盘,尾部与 short 相同
    # 分别扫描,尾部相同 → 动量分应一致
    monkeypatch.setattr(mom.market, "load_kline", lambda c: short)
    v1 = mom.run_momentum_screen(["X"], as_of="2024-06-01", fetch=False)
    monkeypatch.setattr(mom.market, "load_kline", lambda c: long)
    v2 = mom.run_momentum_screen(["X"], as_of="2024-06-02", fetch=False)
    assert v1["入选数"] == v2["入选数"] == 1
    s1 = v1["入选清单"][0]["特征"]["动量分"]
    s2 = v2["入选清单"][0]["特征"]["动量分"]
    assert abs(s1 - s2) < 1e-9
