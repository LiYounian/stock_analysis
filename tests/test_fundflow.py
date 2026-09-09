"""fundflow.py 单测(mock 东财 HTTP,不触网)。

锁语义:secid 映射、klines 解析(列顺序/排序)、空数据抛错、
摘要派生(连续净流入天数/近5日合计)、落盘读盘往返。
"""
import pandas as pd
import pytest

from tools.collectors import fundflow as ff
from tools.store import repo as store


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


def test_fetch_one_all_sources_fail_raises(monkeypatch):
    """三级 fallback 全空/失败 → 抛错,不返空 df 伪装成功(ValueError 非瞬时错→不触发重试等待)。"""
    monkeypatch.setattr(ff, "_http_get", lambda secid: {"data": {"klines": []}})   # 东财空
    monkeypatch.setattr(ff, "_fetch_tencent", lambda code: (_ for _ in ()).throw(ValueError("腾讯挂")))
    monkeypatch.setattr(ff, "_fetch_sina", lambda code: (_ for _ in ()).throw(ValueError("新浪挂")))
    with pytest.raises(ValueError):
        ff.fetch_one("000021")


def test_summarize_streak_and_sum():
    df = ff._parse(_fake_js())
    s = ff.summarize(df)
    assert s["主力连续净流入天数"] == 2         # 最后两天 >0,再前一天 <0
    assert s["今日主力净流入"] == 3000
    assert s["近5日主力合计"] == 4000           # -1000+2000+3000


def test_fetch_and_load_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    monkeypatch.setattr(ff, "_http_get", lambda secid: _fake_js())
    out = ff.fetch_fundflow(["000021"])
    assert "000021" in out and len(out["000021"]) == 3
    loaded = ff.load_fundflow("000021")
    assert loaded.iloc[-1]["主力净流入"] == 3000
    assert store.get_raw_meta("fundflow", "000021")["source"] == "eastmoney"
    with pytest.raises(FileNotFoundError):
        ff.load_fundflow("999999")


# ---------- 第二源 腾讯 proxy / 第三源 新浪 / 三级 fallback ----------
class _FakeResp:
    def __init__(self, js=None, text=None):
        self._js, self.text, self.encoding = js, text, None

    def raise_for_status(self):
        pass

    def json(self):
        return self._js


def _tx_js(main=-20147224, sup=-25681386, big=5534162, nor=19397673, sml=749551):
    """腾讯 hsfundtab 样本:main == super+big(恒等式成立);历史3天,末天=今日。"""
    one = [{"date": "2026-09-07", "mainNetIn": "111", "price": "10", "avgIn": "1"},
           {"date": "2026-09-08", "mainNetIn": "222", "price": "10", "avgIn": "1"},
           {"date": "2026-09-09", "mainNetIn": str(main), "price": "11.7", "avgIn": "1"}]
    return {"code": 0, "msg": "ok", "data": {
        "todayFundFlow": {"stockCode": "sz000001", "mainNetIn": str(main), "superFlow": str(sup),
                          "bigFlow": str(big), "normalFlow": str(nor), "smallFlow": str(sml)},
        "historyFundFlow": {"oneDayKlineList": one}}}


def test_fetch_tencent_maps_today_five_tier_history_main_only(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(js=_tx_js()))
    df = ff._fetch_tencent("000001")
    assert list(df.columns) == ff._COLS
    assert df["date"].is_monotonic_increasing
    last = df.iloc[-1]
    assert last["主力净流入"] == -20147224           # 今日主力
    assert last["超大单净流入"] == -25681386           # 今日五档补齐
    assert last["大单净流入"] == 5534162
    assert last["中单净流入"] == 19397673
    assert pd.isna(df.iloc[0]["超大单净流入"])          # 历史行四档 NaN
    assert df.iloc[0]["主力净流入"] == 111              # 历史仅主力净额


def test_fetch_tencent_identity_violation_raises(monkeypatch):
    """恒等式硬断言:main ≠ super+big → 抛(挡字段错位/单位错配)。"""
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(js=_tx_js(main=999)))
    with pytest.raises(ValueError, match="口径异常"):
        ff._fetch_tencent("000001")


def _sina_text():
    import json
    arr = [{"opendate": "2026-09-09", "netamount": "-12774425.78", "trade": "11.7"},
           {"opendate": "2026-09-08", "netamount": "333", "trade": "11.6"}]
    return "/*<script></script>*/\nvar FDFLOW=(" + json.dumps(arr) + ");"


def test_fetch_sina_main_only(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(text=_sina_text()))
    df = ff._fetch_sina("000001")
    assert list(df.columns) == ff._COLS
    assert df["date"].is_monotonic_increasing
    assert df.iloc[-1]["主力净流入"] == -12774425.78     # netamount
    assert pd.isna(df.iloc[-1]["超大单净流入"])           # 五档 NaN
    assert pd.isna(df.iloc[-1]["主力净占比"])             # 占比口径不可比 → NaN


def test_fallback_to_tencent_records_source(monkeypatch, tmp_path):
    """东财失败 → 腾讯命中;meta.source=tencent_proxy、tier_history=main_only。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    monkeypatch.setattr(ff, "_http_get", lambda secid: (_ for _ in ()).throw(ValueError("东财墙")))
    monkeypatch.setattr(ff, "_fetch_tencent", lambda code: ff._parse(_fake_js()))
    out = ff.fetch_fundflow(["000021"])
    assert "000021" in out
    meta = store.get_raw_meta("fundflow", "000021")
    assert meta["source"] == "tencent_proxy" and meta["tier_history"] == "main_only"


def test_fallback_to_sina_when_tencent_also_fails(monkeypatch, tmp_path):
    """东财+腾讯都失败 → 新浪命中;meta.source=sina。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    monkeypatch.setattr(ff, "_http_get", lambda secid: (_ for _ in ()).throw(ValueError("东财墙")))
    monkeypatch.setattr(ff, "_fetch_tencent", lambda code: (_ for _ in ()).throw(ValueError("腾讯挂")))
    monkeypatch.setattr(ff, "_fetch_sina", lambda code: ff._parse(_fake_js()))
    out = ff.fetch_fundflow(["000021"])
    assert store.get_raw_meta("fundflow", "000021")["source"] == "sina"
