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


# ———— #26 大宗交易 5000 行静默截断 → 递归缩窗 + 命中上限告警 ————
import logging  # noqa: E402


def _mk_block_df(codes):
    return pd.DataFrame([
        {"证券代码": c, "交易日期": "2026-08-05", "成交价": 1.0, "成交量": 1,
         "成交额": 1.0, "折溢率": 0.0, "买方营业部": "", "卖方营业部": ""}
        for c in codes
    ])


def test_split_window_disjoint():
    """二分子窗互斥无重叠(拼接不重复计数)、覆盖端点、单日不可再分。"""
    (l0, l1), (r0, r1) = sm._split_window("20260301", "20260903")
    assert l0 == "20260301" and r1 == "20260903"
    assert l1 < r0                                   # 左窗末 < 右窗首(互斥,无重叠)
    assert pd.Timestamp(r0) == pd.Timestamp(l1) + pd.Timedelta(days=1)
    assert sm._split_window("20260305", "20260305") is None   # 单日不可再分


def test_block_recursion_recovers_truncation(monkeypatch, caplog):
    """命中 5000 行上限的宽窗:二分缩窗重取直到每段 < 上限,目标高位代码不再被切掉。"""
    cap = sm._BLOCK_PAGE_CAP

    def fake_window(start, end):
        span = (pd.Timestamp(end) - pd.Timestamp(start)).days
        if span > 20:                                # 宽窗:命中上限(高位代码 300857 被切掉)
            return _mk_block_df(["600000"] * cap)
        return _mk_block_df(["600000", "300857"])    # 窄窗:完整,含目标票

    monkeypatch.setattr(sm, "_fetch_block_window", fake_window)
    with caplog.at_level(logging.WARNING, logger="collectors.smart_money"):
        df = sm._fetch_block_market("20260301", "20260903")
    assert df.attrs["truncated"] is False                    # 已完整恢复
    assert (df["证券代码"].astype(str).str.zfill(6) == "300857").any()   # 高位票召回
    assert not (df["证券代码"] == "600000").all() or len(df) != cap      # 非"恰好截断"的假全量
    assert any("二分缩窗重取" in r.message for r in caplog.records)      # 有声


def test_block_single_day_truncation_flags(monkeypatch, caplog):
    """单日仍命中上限=不可再分 → error 告警 + df.attrs['truncated']=True(接受但绝不静默)。"""
    cap = sm._BLOCK_PAGE_CAP
    monkeypatch.setattr(sm, "_fetch_block_window", lambda s, e: _mk_block_df(["600000"] * cap))
    with caplog.at_level(logging.ERROR, logger="collectors.smart_money"):
        df = sm._fetch_block_market("20260305", "20260305")
    assert df.attrs["truncated"] is True
    assert any("单日截断" in r.message for r in caplog.records)


def test_block_empty_raises(monkeypatch):
    """全窗皆空仍抛 ValueError(保留原契约,空≠截断)。"""
    monkeypatch.setattr(sm, "_fetch_block_window", lambda s, e: pd.DataFrame())
    import pytest
    with pytest.raises(ValueError):
        sm._fetch_block_market("20260301", "20260903")
