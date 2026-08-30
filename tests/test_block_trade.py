"""block_trade.py 大宗交易采集器单测(mock 网络,不触网)。

锁的语义:
- _norm_row 归一到契约字段;direction = 折溢率符号;inst_buy = 买方"机构专用";
- 盘后披露标记 visible_after_close=True;
- 落盘按票分区 + (trade_date,buyer,seller,volume,deal_price) 去重幂等 + 前向增量并集;
- 新鲜度门控 / stale_codes;
- block_asof **严格小于 as_of**(盘后披露,交易日当天不可用)——无未来函数;
- 区间拉取失败优雅降级。
"""
import pandas as pd
import pytest

from tools.collectors import block_trade as bt
from tools.store import repo as store


def _raw_row(code="000100", trade_date="2024-01-04", premium=0.0,
             buyer="中信建投证券", seller="机构专用", volume=930000.0,
             deal_price=4.18, **kw):
    row = {
        "证券代码": code, "证券简称": "某股", "交易日期": trade_date,
        "涨跌幅": -1.87, "收盘价": 4.18, "成交价": deal_price, "折溢率": premium,
        "成交量": volume, "成交额": deal_price * volume, "成交额/流通市值": 0.005,
        "买方营业部": buyer, "卖方营业部": seller,
    }
    row.update(kw)
    return row


def test_norm_row_contract_and_flags():
    ev = bt._norm_row(_raw_row(premium=-5.0, buyer="机构专用"))
    assert ev["code"] == "000100" and ev["trade_date"] == "2024-01-04"
    assert ev["direction"] == -1                      # 折价
    assert ev["inst_buy"] == 1                         # 买方机构专用
    assert ev["visible_after_close"] is True
    assert ev["premium_rate"] == -5.0 and ev["amount_to_float"] == 0.005


def test_direction_and_inst():
    assert bt.block_direction(2.0) == 1 and bt.block_direction(-2.0) == -1
    assert bt.block_direction(0.0) == 0 and bt.block_direction(None) == 0
    assert bt.block_inst_buy("机构专用") == 1
    assert bt.block_inst_buy("中信建投证券") == 0 and bt.block_inst_buy(None) == 0


def test_norm_row_rejects_bad():
    assert bt._norm_row({"证券代码": "", "交易日期": "2024-01-04"}) is None
    assert bt._norm_row({"证券代码": "abc", "交易日期": "2024-01-04"}) is None


def _install_range(monkeypatch, rows):
    df = pd.DataFrame([bt._norm_row(r) for r in rows])
    monkeypatch.setattr(bt, "fetch_range_df", lambda *a, **k: df)


def test_fetch_persists_and_dedup_multi_trade(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_range(monkeypatch, [
        _raw_row(deal_price=4.18, volume=930000.0),
        _raw_row(deal_price=4.18, volume=930000.0),           # 完全同键 → 去重
        _raw_row(deal_price=4.20, volume=700000.0),           # 同日另一笔 → 保留
        _raw_row(code="000333", deal_price=54.5, volume=1220000.0),
    ])
    out = bt.fetch_block_trade("20240101", "20240110")
    assert set(out) == {"000100", "000333"}
    assert len(out["000100"]) == 2                             # 一票一日多笔保留、同键去重
    assert store.get_raw_meta("block_trade", "000100")["source"] == "eastmoney"


def test_incremental_union_keeps_old(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_range(monkeypatch, [_raw_row(trade_date="2024-01-03", deal_price=4.0)])
    bt.fetch_block_trade("20240101", "20240104")
    _install_range(monkeypatch, [_raw_row(trade_date="2024-01-08", deal_price=4.5)])
    out = bt.fetch_block_trade("20240105", "20240110", skip_fresh=False)
    dates = {ev["trade_date"] for ev in out["000100"]}
    assert dates == {"2024-01-03", "2024-01-08"}


def test_freshness_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_range(monkeypatch, [_raw_row(deal_price=1.0)])
    bt.fetch_block_trade("20240101", "20240110")
    _install_range(monkeypatch, [_raw_row(deal_price=9.9)])
    out = bt.fetch_block_trade("20240101", "20240110", skip_fresh=True, max_days=30)
    assert [ev["deal_price"] for ev in out["000100"]] == [1.0]     # 未覆盖


def test_stale_codes(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_range(monkeypatch, [_raw_row(code="000100")])
    bt.fetch_block_trade("20240101", "20240110")
    assert bt.stale_codes(["000100", "000999"], max_days=30) == ["000999"]


def test_range_df_degrades(monkeypatch):
    import akshare as ak
    monkeypatch.setattr(ak, "stock_dzjy_mrmx",
                        lambda **k: (_ for _ in ()).throw(ConnectionError("限流")))
    assert bt.fetch_range_df("20240101", "20240110").empty


def test_asof_strict_less_than(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_range(monkeypatch, [_raw_row(trade_date="2024-01-04")])
    bt.fetch_block_trade("20240101", "20240110")
    assert bt.block_asof("000100", "2024-01-04") == []            # 当天不可用
    assert len(bt.block_asof("000100", "2024-01-05")) == 1        # T+1 可见


def test_load_roundtrip_and_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_range(monkeypatch, [_raw_row(code="000100")])
    bt.fetch_block_trade("20240101", "20240110")
    assert isinstance(bt.load_block_trade("000100"), list)
    with pytest.raises(FileNotFoundError):
        bt.load_block_trade("999999")
