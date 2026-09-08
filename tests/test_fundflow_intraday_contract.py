"""午盘 Q · M1 · fundflow_intraday 契约测试。

覆盖:
    · 常量/API 存在
    · 参数校验(freq/as_of/codes 长度)
    · as_of 截断(未来函数红线)
    · 空数据/网络失败异常
    · _parse 归一
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from tools.collectors import fundflow_intraday as ff


# ────────────────────────────── 常量/接口 ──────────────────────────────

def test_module_constants():
    assert ff.FREQ_1MIN == 1
    assert ff.FREQ_5MIN == 5
    assert ff.MAX_BATCH_CODES == 50
    assert ff._SOURCE == "eastmoney:fflow/kline"
    assert "fflow/kline/get" in ff._FF_URL
    assert ff._COLS[0] == "time"
    assert "主力净流入" in ff._COLS
    assert "超大单净流入" in ff._COLS


def test_secid_mapping():
    assert ff._secid("600519") == "1.600519"
    assert ff._secid("002415") == "0.002415"
    assert ff._secid("300750") == "0.300750"


def test_public_api_signatures_exist():
    assert callable(ff.fetch_one)
    assert callable(ff.collect)
    assert callable(ff.load_intraday)


# ────────────────────────────── 参数校验 ──────────────────────────────

def test_fetch_one_rejects_bad_freq():
    with pytest.raises(ValueError, match="freq"):
        ff.fetch_one("600519", freq=15)


def test_fetch_one_rejects_bad_as_of():
    with pytest.raises(ValueError, match="as_of"):
        ff.fetch_one("600519", as_of="14:305")
    with pytest.raises(ValueError, match="as_of"):
        ff.fetch_one("600519", as_of="1430")


def test_collect_rejects_too_many_codes():
    with pytest.raises(ValueError, match="MAX_BATCH_CODES"):
        ff.collect(["000001"] * 51, freq=5)


# ────────────────────────────── _parse 归一 ──────────────────────────────

def test_parse_normal():
    """典型返回 → DataFrame 列齐、time 是 Timestamp、按 time 升序。"""
    js = {"data": {"klines": [
        "2026-09-05 09:30,-100000.0,50000.0,-30000.0,80000.0,-200000.0,-0.5",
        "2026-09-05 09:35,120000.0,-60000.0,20000.0,-40000.0,-40000.0,0.3",
    ]}}
    df = ff._parse(js)
    assert list(df.columns) == ff._COLS
    assert len(df) == 2
    assert isinstance(df["time"].iloc[0], pd.Timestamp)
    assert df["time"].iloc[0] < df["time"].iloc[1]
    assert df["主力净流入"].iloc[0] == -100000.0
    assert df["主力净占比"].iloc[1] == 0.3


def test_parse_empty():
    """空 klines → 空 DataFrame 列齐。"""
    df = ff._parse({"data": {"klines": []}})
    assert df.empty
    assert list(df.columns) == ff._COLS


def test_parse_missing_data_field():
    """data 字段缺失 → 空 DataFrame(不 KeyError)。"""
    df = ff._parse({"rc": 0})
    assert df.empty


def test_parse_bad_numeric_becomes_nan():
    """数值列非法 → NaN(不静默填 0)。"""
    js = {"data": {"klines": ["2026-09-05 09:30,abc,50000.0,-30000.0,80000.0,-200000.0,-0.5"]}}
    df = ff._parse(js)
    assert pd.isna(df["主力净流入"].iloc[0])


# ────────────────────────────── as_of 截断(未来函数红线) ──────────────────────────────

def test_truncate_by_as_of_cuts_future():
    """给定 14:30 as_of,>14:30 的行被剔除。"""
    df = pd.DataFrame({
        "time": pd.to_datetime([
            "2026-09-05 14:25", "2026-09-05 14:30",
            "2026-09-05 14:35", "2026-09-05 14:50",
        ]),
        "主力净流入": [1.0, 2.0, 3.0, 4.0],
    })
    trunc = ff._truncate_by_as_of(df, "14:30")
    assert len(trunc) == 2
    assert trunc["主力净流入"].tolist() == [1.0, 2.0]


def test_truncate_by_as_of_none_keeps_all():
    df = pd.DataFrame({"time": pd.to_datetime(["2026-09-05 14:35"]), "主力净流入": [1.0]})
    trunc = ff._truncate_by_as_of(df, None)
    assert len(trunc) == 1


def test_truncate_empty_df_ok():
    empty = pd.DataFrame(columns=ff._COLS)
    trunc = ff._truncate_by_as_of(empty, "14:30")
    assert trunc.empty


# ────────────────────────────── fetch_one 行为(mock 网络层) ──────────────────────────────

def test_fetch_one_empty_raises_valueerror():
    """空 klines → ValueError,不返回空 DataFrame 伪装成功。"""
    with patch.object(ff, "_http_get", return_value={"data": {"klines": []}}):
        with pytest.raises(ValueError, match="为空"):
            ff.fetch_one("600519", freq=5)


def test_fetch_one_respects_as_of():
    """fetch_one 传 as_of='14:30' → 结果里没有 time>14:30 的行。"""
    js = {"data": {"klines": [
        "2026-09-05 14:25,1,0,0,0,0,0",
        "2026-09-05 14:30,2,0,0,0,0,0",
        "2026-09-05 14:35,3,0,0,0,0,0",
        "2026-09-05 14:50,4,0,0,0,0,0",
    ]}}
    with patch.object(ff, "_http_get", return_value=js):
        df = ff.fetch_one("600519", freq=5, as_of="14:30")
    assert len(df) == 2
    assert (df["time"].dt.strftime("%H:%M") <= "14:30").all()


def test_fetch_one_full_day_when_no_as_of():
    js = {"data": {"klines": [
        "2026-09-05 09:30,1,0,0,0,0,0",
        "2026-09-05 14:50,4,0,0,0,0,0",
        "2026-09-05 15:00,5,0,0,0,0,0",
    ]}}
    with patch.object(ff, "_http_get", return_value=js):
        df = ff.fetch_one("600519", freq=5)
    assert len(df) == 3
