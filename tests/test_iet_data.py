"""IET 探针真实 reader 单测(monkeypatch store,不碰真实数据)。

锁定 as-of 取值语义(防未来:只取≤date)与动量日期选取。IO 拼接靠冒烟,此处锁逻辑。
"""
import pandas as pd
import pytest

from tools.backtest.iet_probe import data as D
from tools.store import repo


@pytest.fixture(autouse=True)
def _clear():
    D.clear_caches()
    yield
    D.clear_caches()


def test_pe_pb_asof(monkeypatch):
    ser = pd.DataFrame({
        "date": ["2024-01-02", "2024-03-01", "2024-06-28"],
        "PE_TTM": [10.0, float("nan"), 15.0],
        "PB": [1.0, 1.2, 1.5],
        "总市值": [1, 2, 3],
    })
    monkeypatch.setattr(repo, "get_raw", lambda kind, code: ser)
    # as-of 2024-03-15 → 取 ≤该日最后一条(2024-03-01):PE=NaN→None, PB=1.2
    assert D.pe_reader("X", "2024-03-15") is None
    assert D.pb_reader("X", "2024-03-15") == 1.2
    # as-of 2024-06-28 → PE=15
    assert D.pe_reader("X", "2024-06-28") == 15.0
    # 早于最早日 → None(防未来:不取未来值)
    assert D.pb_reader("X", "2023-12-31") is None


def test_turnover_asof(monkeypatch):
    k = pd.DataFrame({
        "date": ["2024-01-02", "2024-01-03", "2024-01-04"],
        "turnover": [0.5, 0.8, 1.1],
    })
    monkeypatch.setattr(repo, "get_master_kline", lambda code: k)
    assert D.turnover_reader("X", "2024-01-03") == 0.8
    assert D.turnover_reader("X", "2024-01-03T23") == 0.8   # ≤ 字符串比较
    assert D.turnover_reader("X", "2023-01-01") is None


def test_rs_momentum_of_selects_leq_date(monkeypatch):
    # 直接注入动量映射,锁"取≤date最近有值日"
    fake = {"2025-07-09": 100.5, "2025-07-10": 101.0, "2025-07-14": 99.0}
    monkeypatch.setattr(D, "_momentum_by_date", lambda ind: fake)
    assert D.rs_momentum_of("电子", "2025-07-10") == 101.0
    assert D.rs_momentum_of("电子", "2025-07-12") == 101.0    # 非交易日→取≤的最近(07-10)
    assert D.rs_momentum_of("电子", "2025-07-14") == 99.0
    assert D.rs_momentum_of("电子", "2025-07-01") is None      # 早于序列 → None
    monkeypatch.setattr(D, "_momentum_by_date", lambda ind: {})
    assert D.rs_momentum_of("电子", "2025-07-10") is None
