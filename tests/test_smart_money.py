"""smart_money.py 单测(纯解析/派生,不触网)。

锁语义:全市场明细按代码切片(含 6 位补零)、列名容错取数、
股东户数连续减少期数派生。
"""
import pandas as pd

from tools.collectors import smart_money as sm


def test_lhb_rows_slice_and_zfill():
    df = pd.DataFrame([
        {"代码": "600000", "上榜日": "2026-08-05", "上榜原因": "涨幅偏离",
         "龙虎榜净买额": 1.2e8, "龙虎榜买入额": 2e8, "龙虎榜卖出额": 8e7,
         "换手率": 5.0, "涨跌幅": 9.9},
        {"代码": "1", "上榜日": "2026-08-04", "上榜原因": "换手",       # 会被补零成 000001
         "龙虎榜净买额": -3e7, "龙虎榜买入额": 1e7, "龙虎榜卖出额": 4e7,
         "换手率": 3.0, "涨跌幅": -2.0},
    ])
    rows = sm._lhb_rows_of(df, "600000")
    assert len(rows) == 1
    assert rows[0]["net_buy"] == 1.2e8 and rows[0]["reason"] == "涨幅偏离"
    assert sm._lhb_rows_of(df, "000001")[0]["net_buy"] == -3e7   # 补零命中


def test_block_rows_colname_tolerance():
    df = pd.DataFrame([
        {"证券代码": "000021", "交易日期": "2026-08-05", "成交价": 12.3,
         "成交量": 1e6, "成交额": 1.23e7, "折溢率": -5.0,
         "买方营业部": "机构专用", "卖方营业部": "某营业部"},
    ])
    rows = sm._block_rows_of(df, "000021")
    assert rows[0]["premium_rate"] == -5.0 and rows[0]["buyer"] == "机构专用"


def test_summarize_holder_streak():
    items = [                       # 已按日期倒序:近两期减少,第三期增加
        {"date": "2026-06-30", "holders": 100, "change_ratio": -1.5},
        {"date": "2026-03-31", "holders": 110, "change_ratio": -2.0},
        {"date": "2025-12-31", "holders": 105, "change_ratio": 3.0},
    ]
    s = sm.summarize_holder(items)
    assert s["最新股东户数"] == 100 and s["连续减少期数"] == 2


def test_summarize_holder_empty():
    s = sm.summarize_holder([])
    assert s["最新股东户数"] is None and s["连续减少期数"] == 0
