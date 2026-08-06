"""fundamental.py 单测(mock 两源,不触网)。

锁语义:指标名映射正确(防 akshare 改版静默错位)、最新期选取、
缺字段/某源失败降级、落盘/读盘往返。
"""
import types

import pandas as pd
import pytest

from tools.collectors import fundamental as fd
from tools.store import repo as store


def _fake_abstract_df():
    """模拟同花顺财务摘要:选项/指标 + 多个报告期列。"""
    return pd.DataFrame({
        "选项": ["常用指标"] * 8,
        "指标": ["营业总收入", "归母净利润", "营业总收入增长率", "归属母公司净利润增长率",
                "净资产收益率(ROE)", "毛利率", "销售净利率", "资产负债率"],
        "20260331": [3723775220.39, 242178267.01, 12.5, 30.1, 8.8, 15.2, 6.5, 41.68],
        "20251231": [1, 1, 1, 1, 1, 1, 1, 1],
    })


def _install_fake_ak(monkeypatch, abstract_df=None, baidu_val=575.27, baidu_raise=False):
    def _baidu(symbol, indicator, period):
        if baidu_raise:
            raise ConnectionError("百度挂了")
        return pd.DataFrame({"date": ["2026-08-04"], "value": [baidu_val]})
    fake = types.SimpleNamespace(
        stock_financial_abstract=lambda symbol: abstract_df,
        stock_zh_valuation_baidu=_baidu,
    )
    monkeypatch.setitem(__import__("sys").modules, "akshare", fake)


def test_abstract_mapping(monkeypatch):
    """指标名映射到正确字段 + 取最新期(第 3 列)。"""
    _install_fake_ak(monkeypatch, _fake_abstract_df())
    rec = fd._fetch_abstract("000021")
    assert rec["报告期"] == "20260331"
    assert rec["营收"] == 3723775220.39
    assert rec["净利"] == 242178267.01
    assert rec["ROE"] == 8.8
    assert rec["负债率"] == 41.68


def test_baidu_latest_value(monkeypatch):
    _install_fake_ak(monkeypatch, baidu_val=47.98)
    b = fd._fetch_baidu("000021")
    assert b["PE_TTM"] == 47.98 and b["PB"] == 47.98    # 假源三项同值


def test_baidu_source_fail_degrades(monkeypatch):
    """百度失败 → 估值字段 None,不抛错。"""
    _install_fake_ak(monkeypatch, baidu_raise=True)
    b = fd._fetch_baidu("000021")
    assert b == {"PE_TTM": None, "PB": None, "总市值": None}


def test_missing_indicator_none(monkeypatch):
    """指标缺失(改版)→ 该字段 None,不 KeyError。"""
    df = _fake_abstract_df()
    df = df[df["指标"] != "毛利率"]                     # 模拟某指标消失
    _install_fake_ak(monkeypatch, df)
    rec = fd._fetch_abstract("000021")
    assert rec["毛利率"] is None
    assert rec["营收"] == 3723775220.39


def test_fetch_and_load_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_fake_ak(monkeypatch, _fake_abstract_df())
    out = fd.fetch_fundamental(["000021"])
    assert "000021" in out
    loaded = fd.load_fundamental("000021")
    assert loaded["ROE"] == 8.8
    assert store.get_raw_meta("fundamental", "000021")["source"] == fd._SOURCE
    with pytest.raises(FileNotFoundError):
        fd.load_fundamental("999999")
