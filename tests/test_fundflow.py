"""fundflow.py 单测(mock 东财 HTTP,不触网)。

锁语义:secid 映射、klines 解析(列顺序/排序)、空数据抛错、
摘要派生(连续净流入天数/近5日合计)、落盘读盘往返。
"""
import pytest

from tools.collectors import fundflow as ff


def test_secid():
    assert ff._secid("600667") == "1.600667"      # 沪
    assert ff._secid("688008") == "1.688008"       # 科创(68→6开头→沪)
    assert ff._secid("000021") == "0.000021"       # 深
    assert ff._secid("300124") == "0.300124"       # 创业(深)


def _fake_js():
    # 日期,主力,小单,中单,大单,超大单,主力占比,...(后面补足到 f65)
    tail = ",0,0,0,0,0,0,0,0"
    return {"data": {"klines": [
        "2026-08-01,-1000,500,500,-600,-400,-5.0" + tail,   # 主力净流出
        "2026-08-04,2000,-500,-1500,800,1200,8.0" + tail,   # 净流入
        "2026-08-05,3000,-1000,-2000,1000,2000,10.0" + tail,  # 净流入
    ]}}


def test_parse_order_and_cols():
    df = ff._parse(_fake_js())
    assert list(df.columns) == ff._COLS
    assert df["date"].is_monotonic_increasing
    assert df.iloc[-1]["主力净流入"] == 3000
    assert df.iloc[-1]["超大单净流入"] == 2000


def test_fetch_one_empty_raises(monkeypatch):
    monkeypatch.setattr(ff, "_http_get", lambda secid: {"data": {"klines": []}})
    with pytest.raises(ValueError):
        ff.fetch_one("000021")


def test_summarize_streak_and_sum():
    df = ff._parse(_fake_js())
    s = ff.summarize(df)
    assert s["主力连续净流入天数"] == 2         # 最后两天 >0,再前一天 <0
    assert s["今日主力净流入"] == 3000
    assert s["近5日主力合计"] == 4000           # -1000+2000+3000


def test_fetch_and_load_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(ff, "_FF_DIR", tmp_path)
    monkeypatch.setattr(ff, "_ff_path", lambda code: tmp_path / f"{code}.parquet")
    monkeypatch.setattr(ff, "_http_get", lambda secid: _fake_js())
    out = ff.fetch_fundflow(["000021"])
    assert "000021" in out and len(out["000021"]) == 3
    loaded = ff.load_fundflow("000021")
    assert loaded.iloc[-1]["主力净流入"] == 3000
    with pytest.raises(FileNotFoundError):
        ff.load_fundflow("999999")
