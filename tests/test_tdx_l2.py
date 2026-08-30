"""tdx_l2.py 单测(mock mootdx,不触网)。

锁语义:列名兼容归一(vol/buyorsell)、方向映射、微观结构摘要、
分页拉取拼接、港股跳过、空数据降级。
"""
import pandas as pd
import pytest

from tools.collectors import tdx_l2


def _raw(n=5, vol_col="vol"):
    return pd.DataFrame({
        "time": ["09:30", "09:30", "09:31", "10:00", "14:57"][:n],
        "price": [10.0, 10.01, 9.99, 10.02, 10.05][:n],
        vol_col: [100, 50, 200, 300, 80][:n],
        "num": [5, 3, 8, 12, 4][:n],
        "buyorsell": [0, 1, 1, 0, 0][:n],
    })


def test_normalize_colname_tolerance_and_direction():
    df = tdx_l2._normalize(_raw(vol_col="vol"))       # 版本变体列名 vol
    assert list(df.columns) == ["time", "price", "volume", "num", "direction"]
    assert df["direction"].tolist() == ["买", "卖", "卖", "买", "买"]
    assert df["volume"].sum() == 730


def test_normalize_empty():
    df = tdx_l2._normalize(pd.DataFrame())
    assert list(df.columns) == ["time", "price", "volume", "num", "direction"] and df.empty


def test_summarize_microstructure():
    s = tdx_l2.summarize(tdx_l2._normalize(_raw()))
    assert s["总笔数"] == 5 and s["总成交量"] == 730.0
    assert s["主买占比"] == pytest.approx(0.6575, abs=1e-3)   # (100+300+80)/730
    assert s["净主动买量"] == pytest.approx(230.0)             # 买480 - 卖250
    assert s["大单笔数"] == 1                                  # 仅 300 达 95 分位


def test_summarize_empty():
    s = tdx_l2.summarize(pd.DataFrame(columns=["volume", "direction"]))
    assert s["总笔数"] == 0 and s["主买占比"] is None


def test_fetch_raw_pagination(monkeypatch):
    """满页续拉、末页(<_PAGE)停止。"""
    monkeypatch.setattr(tdx_l2, "_get_client", lambda: object())
    monkeypatch.setattr(tdx_l2, "_PAGE", 3)
    pages = [_raw(3), _raw(2)]        # 第1页满(3),第2页不满(2)→停
    calls = {"i": 0}

    def fake_page(client, code, start, date):
        i = calls["i"]; calls["i"] += 1
        return pages[i] if i < len(pages) else pd.DataFrame()
    monkeypatch.setattr(tdx_l2, "_fetch_page", fake_page)
    df = tdx_l2._fetch_raw("600519")
    assert len(df) == 5 and calls["i"] == 2       # 两页拼接,拉2次即停


def test_fetch_one_hk_skips(monkeypatch):
    from tools.config import stock_pool
    monkeypatch.setattr(stock_pool, "is_hk", lambda c: True)
    df = tdx_l2.fetch_one("02513")
    assert df.empty and list(df.columns) == ["time", "price", "volume", "num", "direction"]


def test_fetch_raw_empty_raises(monkeypatch):
    monkeypatch.setattr(tdx_l2, "_get_client", lambda: object())
    monkeypatch.setattr(tdx_l2, "_fetch_page", lambda *a: pd.DataFrame())
    with pytest.raises(ValueError):
        tdx_l2._fetch_raw("600519")
