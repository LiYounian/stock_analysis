"""指数采集单测(mock 数据源,不触网)。

锁语义:别名→代码映射、交易所前缀、多源 fallback、落盘/读回一致(列归一化复用 market)。
"""
import pandas as pd
import pytest

from tools.collectors import index
from tools.store import repo as store


def _sina_df():
    """新浪风格(英文列,无 amount/turnover)。"""
    return pd.DataFrame({
        "date": ["2024-01-03", "2024-01-02", "2024-01-01"],   # 乱序,验证归一化排序
        "open": [10.0, 10.1, 9.9], "high": [10.5, 10.3, 10.0],
        "low": [9.8, 9.9, 9.7], "close": [10.2, 10.0, 9.95],
        "volume": [100, 110, 90],
    })


def _em_df():
    """东财风格(中文列,含成交额/涨跌幅)。"""
    return pd.DataFrame({
        "日期": ["2024-01-01", "2024-01-02"], "开盘": [9.9, 10.1], "收盘": [9.95, 10.0],
        "最高": [10.0, 10.3], "最低": [9.7, 9.9], "成交量": [90, 110],
        "成交额": [900, 1100], "涨跌幅": [0.5, 0.5], "换手率": [1.0, 1.1],
    })


def test_index_prefix():
    assert index.index_prefix("000300") == "sh000300"
    assert index.index_prefix("399006") == "sz399006"
    assert index.index_prefix("899050") == "bj899050"
    assert index.index_prefix("000001") == "sh000001"


def test_fetch_index_alias_and_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    monkeypatch.setitem(index._FETCHERS, "sina", lambda *a, **k: _sina_df())
    out = index.fetch_index(["沪深300"], start="20240101", end="20240103")
    assert "000300" in out                                   # 别名已转 6 位代码
    df = index.load_index("沪深300")                          # 读回也支持别名
    assert list(df["date"].dt.strftime("%Y-%m-%d")) == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert store.get_raw_meta("index_kline", "000300")["source"] == "sina"


def test_fetch_index_fallback_to_eastmoney(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)

    def boom(*a, **k):
        raise ConnectionError("sina down")
    monkeypatch.setitem(index._FETCHERS, "sina", boom)
    monkeypatch.setitem(index._FETCHERS, "eastmoney", lambda *a, **k: _em_df())
    out = index.fetch_index(["000905"], start="20240101", end="20240102")
    assert "000905" in out
    assert store.get_raw_meta("index_kline", "000905")["source"] == "eastmoney"


def test_fetch_index_all_sources_fail_skips(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)

    def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setitem(index._FETCHERS, "sina", boom)
    monkeypatch.setitem(index._FETCHERS, "eastmoney", boom)
    out = index.fetch_index(["000300"], start="20240101", end="20240102")
    assert out == {}                                         # 全失败→跳过,不伪造成功
