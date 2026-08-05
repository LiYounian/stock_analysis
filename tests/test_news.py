"""news.py 单测(mock 东财,不触网)。锁:列归一、时间窗过滤、倒序、往返。"""
import sys
import types

import pandas as pd
import pytest

from tools.collectors import news as nw


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
    monkeypatch.setattr(nw, "_NEWS_DIR", tmp_path)
    monkeypatch.setattr(nw, "_news_path", lambda code: tmp_path / f"{code}.json")
    _install(monkeypatch, _fake_df())
    # days 很大以纳入 2026 的两条,排除 2000 的旧闻靠 cutoff;这里用大窗但旧闻 2000 仍 < cutoff
    out = nw.fetch_news(["000021"], days=3650)   # ~10年窗
    items = out["000021"]
    # 2000 年那条无论如何 < cutoff(今为2026),被过滤
    assert all(it["time"][:4] == "2026" for it in items)
    assert items[0]["time"] > items[-1]["time"] if len(items) > 1 else True
    assert set(items[0].keys()) == {"title", "content", "time", "source", "url"}


def test_load_roundtrip_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(nw, "_NEWS_DIR", tmp_path)
    monkeypatch.setattr(nw, "_news_path", lambda code: tmp_path / f"{code}.json")
    _install(monkeypatch, _fake_df())
    nw.fetch_news(["000021"], days=3650)
    assert isinstance(nw.load_news("000021"), list)
    with pytest.raises(FileNotFoundError):
        nw.load_news("999999")
