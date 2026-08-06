"""news.py 单测(mock 东财,不触网)。锁:列归一、时间窗过滤、倒序、往返。"""
import sys
import types

import pandas as pd
import pytest

from tools.collectors import news as nw
from tools.store import repo as store


def _fake_df():
    return pd.DataFrame({
        "关键词": ["000021"] * 3,
        "新闻标题": ["旧闻", "利好A", "利好B"],
        "新闻内容": ["c0", "c1", "c2"],
        "发布时间": ["2000-01-01 09:00:00", "2026-08-05 10:00:00", "2026-08-06 11:00:00"],
        "文章来源": ["s", "s", "s"], "新闻链接": ["u0", "u1", "u2"],
    })


def _install(monkeypatch, df):
    fake = types.SimpleNamespace(stock_news_em=lambda symbol: df)
    monkeypatch.setitem(sys.modules, "akshare", fake)


def test_fetch_normalizes_filters_sorts(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install(monkeypatch, _fake_df())
    # days 很大以纳入 2026 的两条,排除 2000 的旧闻靠 cutoff;这里用大窗但旧闻 2000 仍 < cutoff
    out = nw.fetch_news(["000021"], days=3650)   # ~10年窗
    items = out["000021"]
    # 2000 年那条无论如何 < cutoff(今为2026),被过滤
    assert all(it["time"][:4] == "2026" for it in items)
    assert items[0]["time"] > items[-1]["time"] if len(items) > 1 else True
    assert set(items[0].keys()) == {"title", "content", "time", "source", "url"}
    assert store.get_raw_meta("news", "000021")["source"] == "eastmoney"


def test_falls_back_to_cls_when_em_fails(monkeypatch, tmp_path):
    """东财挂 → 回落财联社电报,按股票名命中,meta.source 记备源。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    from tools.config import stock_pool
    monkeypatch.setattr(stock_pool, "get",
                        lambda code: types.SimpleNamespace(code=code, name="紫光国微"))
    cls_df = pd.DataFrame({
        "标题": ["紫光国微发布利好", "无关宏观新闻"],
        "内容": ["公司公告内容", "美联储议息"],
        "发布日期": ["2026-08-06", "2026-08-06"],
        "发布时间": ["10:00:00", "11:00:00"],
    })

    def _boom(symbol):
        raise ConnectionError("东财挂了")

    fake = types.SimpleNamespace(stock_news_em=_boom,
                                 stock_info_global_cls=lambda: cls_df)
    monkeypatch.setitem(sys.modules, "akshare", fake)

    items = nw.fetch_news(["000021"], days=3650)["000021"]
    assert len(items) == 1                          # 仅命中股票名那条(宏观条被过滤)
    assert "紫光国微" in items[0]["title"]
    assert items[0]["source"] == "财联社电报"
    assert store.get_raw_meta("news", "000021")["source"] == "财联社电报"


def test_em_empty_then_cls_empty_keeps_eastmoney(monkeypatch, tmp_path):
    """东财返回空 + 备源无命中 → 落空数据,source 仍记主源 eastmoney。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    from tools.config import stock_pool
    monkeypatch.setattr(stock_pool, "get",
                        lambda code: types.SimpleNamespace(code=code, name="紫光国微"))
    empty_cls = pd.DataFrame({"标题": [], "内容": [], "发布日期": [], "发布时间": []})
    fake = types.SimpleNamespace(stock_news_em=lambda symbol: pd.DataFrame(),
                                 stock_info_global_cls=lambda: empty_cls)
    monkeypatch.setitem(sys.modules, "akshare", fake)

    items = nw.fetch_news(["000021"], days=3650)["000021"]
    assert items == []
    assert store.get_raw_meta("news", "000021")["source"] == "eastmoney"


def test_load_roundtrip_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install(monkeypatch, _fake_df())
    nw.fetch_news(["000021"], days=3650)
    assert isinstance(nw.load_news("000021"), list)
    with pytest.raises(FileNotFoundError):
        nw.load_news("999999")
