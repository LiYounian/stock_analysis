"""news.py 单测(mock 各源,不触网)。

锁的语义:
- 东财列归一、时间窗过滤、倒序、往返(既有);
- 东财 + 新浪**并集去重**(重叠只留一条、两源独有都在);
- 新浪独有的"更近日期"条目能进最终结果(证明补召回稀疏票);
- cutoff 过滤:超窗条目被丢;
- 单源抛异常时另一源结果仍正常(隔离);
- meta.source 反映实际贡献源(如 "eastmoney+新浪");
- 两并集源皆空 → 回落财联社电报(降级保底)。
"""
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
    """装东财假 akshare;新浪源默认置空(不触网),各测按需覆盖 nw._fetch_sina。"""
    fake = types.SimpleNamespace(stock_news_em=lambda symbol: df)
    monkeypatch.setitem(sys.modules, "akshare", fake)
    monkeypatch.setattr(nw, "_fetch_sina", lambda code, cutoff: [])


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


def test_union_dedup_and_backfill(monkeypatch, tmp_path):
    """东财 + 新浪并集:重叠(同 url)只留一条,两源独有都在,新浪更近日期补召回。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    # 东财:稀疏票,只到 08-04,含一条与新浪重叠(同 url u_dup)
    em_df = pd.DataFrame({
        "关键词": ["300209"] * 2,
        "新闻标题": ["东财独有旧闻", "重叠新闻"],
        "新闻内容": ["ec0", "edup"],
        "发布时间": ["2026-08-03 09:00:00", "2026-08-04 10:00:00"],
        "文章来源": ["em", "em"], "新闻链接": ["u_em_only", "u_dup"],
    })
    fake = types.SimpleNamespace(stock_news_em=lambda symbol: em_df)
    monkeypatch.setitem(sys.modules, "akshare", fake)
    # 新浪:覆盖到 08-10(补召回),含同 url u_dup 的重叠条 + 独有更近条
    sina = [
        {"title": "重叠新闻(新浪抓到)", "content": "sdup", "time": "2026-08-04 10:30:00",
         "source": "新浪", "url": "u_dup"},
        {"title": "新浪独有近日新闻", "content": "s1", "time": "2026-08-10 15:36:00",
         "source": "新浪", "url": "u_sina_only"},
    ]
    monkeypatch.setattr(nw, "_fetch_sina", lambda code, cutoff: sina)

    items = nw.fetch_news(["300209"], days=30)["300209"]
    urls = [it["url"] for it in items]
    # 重叠 url 只出现一次,且保留主源(东财)先到者
    assert urls.count("u_dup") == 1
    dup = next(it for it in items if it["url"] == "u_dup")
    assert dup["title"] == "重叠新闻"          # 主源在前,去重留东财先到版本(非新浪版)
    # 两源各自独有条目都在
    assert "u_em_only" in urls and "u_sina_only" in urls
    # 新浪独有的更近日期条目进入结果(补召回),且倒序在最前
    assert items[0]["url"] == "u_sina_only" and items[0]["time"][:10] == "2026-08-10"
    # meta.source 反映两源贡献
    assert store.get_raw_meta("news", "300209")["source"] == "eastmoney+新浪"


def test_cutoff_drops_out_of_window(monkeypatch, tmp_path):
    """超窗条目(无论来自哪源)被 cutoff 过滤掉。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    em_df = pd.DataFrame({
        "关键词": ["300209"] * 2,
        "新闻标题": ["窗内", "太旧"],
        "新闻内容": ["a", "b"],
        "发布时间": ["2026-08-09 10:00:00", "2020-01-01 10:00:00"],
        "文章来源": ["em", "em"], "新闻链接": ["u_in", "u_old_em"],
    })
    fake = types.SimpleNamespace(stock_news_em=lambda symbol: em_df)
    monkeypatch.setitem(sys.modules, "akshare", fake)
    sina = [
        {"title": "新浪窗内", "content": "x", "time": "2026-08-10 09:00:00",
         "source": "新浪", "url": "u_sina_in"},
        {"title": "新浪太旧", "content": "y", "time": "2019-05-05 09:00:00",
         "source": "新浪", "url": "u_old_sina"},
    ]
    monkeypatch.setattr(nw, "_fetch_sina", lambda code, cutoff: sina)

    items = nw.fetch_news(["300209"], days=30)["300209"]
    urls = {it["url"] for it in items}
    assert urls == {"u_in", "u_sina_in"}      # 两条超窗都被丢


def test_source_isolation_em_fails_sina_survives(monkeypatch, tmp_path):
    """东财抛异常时,新浪结果仍正常返回(单源隔离),meta.source 仅记新浪。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)

    def _boom(symbol):
        raise ConnectionError("东财挂了")

    fake = types.SimpleNamespace(stock_news_em=_boom)
    monkeypatch.setitem(sys.modules, "akshare", fake)
    sina = [{"title": "新浪照常", "content": "z", "time": "2026-08-10 09:00:00",
             "source": "新浪", "url": "u_sina"}]
    monkeypatch.setattr(nw, "_fetch_sina", lambda code, cutoff: sina)

    items = nw.fetch_news(["300209"], days=30)["300209"]
    assert [it["url"] for it in items] == ["u_sina"]
    assert store.get_raw_meta("news", "300209")["source"] == "新浪"


def test_source_isolation_sina_fails_em_survives(monkeypatch, tmp_path):
    """新浪抛异常时,东财结果仍正常返回(反向隔离)。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    em_df = pd.DataFrame({
        "关键词": ["300209"],
        "新闻标题": ["东财照常"], "新闻内容": ["c"],
        "发布时间": ["2026-08-09 10:00:00"],
        "文章来源": ["em"], "新闻链接": ["u_em"],
    })
    fake = types.SimpleNamespace(stock_news_em=lambda symbol: em_df)
    monkeypatch.setitem(sys.modules, "akshare", fake)

    def _boom(code, cutoff):
        raise TimeoutError("新浪超时")

    monkeypatch.setattr(nw, "_fetch_sina", _boom)

    items = nw.fetch_news(["300209"], days=30)["300209"]
    assert [it["url"] for it in items] == ["u_em"]
    assert store.get_raw_meta("news", "300209")["source"] == "eastmoney"


def test_dedup_by_title_date_when_no_url():
    """无 url 时按 title+日期(time[:10])去重:同标题同日只留一条,跨日保留。"""
    a = [{"title": "T", "content": "", "time": "2026-08-10 09:00:00",
          "source": "eastmoney", "url": ""}]
    b = [{"title": "T", "content": "", "time": "2026-08-10 18:00:00",   # 同标题同日 → 去重
          "source": "新浪", "url": ""},
         {"title": "T", "content": "", "time": "2026-08-09 18:00:00",   # 同标题跨日 → 保留
          "source": "新浪", "url": ""}]
    merged = nw._dedup_merge(a, b)
    assert len(merged) == 2
    assert merged[0]["source"] == "eastmoney"     # 主源先到者留


def test_parse_sina_normalizes_contract():
    """新浪 HTML 解析归一到契约字段(title/content/time/source/url),time 补 :00 秒。"""
    # 用 &nbsp; 分隔(真实页面格式),锁住解析前必须做实体替换
    html = (
        'x<div class="datelist"><ul>'
        "&nbsp;&nbsp;2026-08-10&nbsp;15:36&nbsp;&nbsp;"
        "<a target='_blank' href='https://sina/a.shtml'>胜宏科技涨2.07%</a> <br>"
        "&nbsp;&nbsp;2026-08-09&nbsp;20:12&nbsp;&nbsp;"
        "<a href=\"https://sina/b.shtml\">大盘综述</a> <br>"
        "</ul></div>y"
    )
    items = nw._parse_sina(html)
    assert len(items) == 2
    it = items[0]
    assert set(it.keys()) == {"title", "content", "time", "source", "url"}
    assert it["title"] == "胜宏科技涨2.07%"
    assert it["time"] == "2026-08-10 15:36:00"
    assert it["source"] == "新浪"
    assert it["url"] == "https://sina/a.shtml"


def test_falls_back_to_cls_when_em_fails(monkeypatch, tmp_path):
    """东财挂 + 新浪空 → 回落财联社电报,按股票名命中,meta.source 记备源。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    from tools.config import stock_pool
    monkeypatch.setattr(stock_pool, "get",
                        lambda code: types.SimpleNamespace(code=code, name="紫光国微"))
    monkeypatch.setattr(nw, "_fetch_sina", lambda code, cutoff: [])
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
    """东财返回空 + 新浪空 + 备源无命中 → 落空数据,source 仍记主源 eastmoney。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    from tools.config import stock_pool
    monkeypatch.setattr(stock_pool, "get",
                        lambda code: types.SimpleNamespace(code=code, name="紫光国微"))
    monkeypatch.setattr(nw, "_fetch_sina", lambda code, cutoff: [])
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
