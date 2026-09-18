"""policy_stream.py 单测(L4:东财 7x24 全量流并入 policy 池)。mock 不触网。

锁语义:
- 游标翻页在目标日 00:00 处终止(不多拉前一日、不死循环);
- as-of 只保留窗口内条目,**绝不纳入未来日条目**(防未来红线);
- BK 板块码解析带缓存(同码只请求一次);
- 无 stockList 走规则兜底(policy._match_industries→to_sw);
- stockList 打标结果是**申万一级名**(∈ SW_INDUSTRIES);
- 池过滤丢弃池外行业条目(bound 下游逐条 LLM 成本);
- 取数失败降级为空、绝不 raise;
- 724 流并入同一个 policy 文件、keyword="7x24流"、契约字段齐全、同 url 去重;
- **industries 混装口径不变量**:池内任一行业名都能被 to_sw 认得,两格式经 to_sw 正确 rollup。
"""
import pandas as pd
import pytest

from tools.analysis.industry_map import SW_INDUSTRIES, to_sw
from tools.collectors import policy as pol
from tools.collectors import policy_stream as ps
from tools.config.stock_pool import Stock
from tools.store import repo as store


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    """store raw 根重定向到临时目录(落盘/BK缓存都落 tmp,不污染生产 data/)。"""
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(store, "_RAW_DIR", raw)
    monkeypatch.setattr(ps, "_BK_CACHE", None)      # 复位进程级 BK 缓存,避免跨用例串味
    return store


def _today():
    return pd.Timestamp.today().strftime("%Y-%m-%d")


def _item(title, date, hh="10:00:00", code="c1", stockList=None, summary=""):
    return {"title": title, "summary": summary, "showTime": f"{date} {hh}",
            "code": code, "stockList": stockList or []}


def _page(items, sort_end):
    return {"fastNewsList": items, "sortEnd": sort_end, "total": 5000}


# ---------- 游标翻页终止 ----------

def test_cursor_paging_stops_at_target_date(monkeypatch):
    today = _today()
    yday = (pd.Timestamp(today) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    pages = {
        "": _page([_item("t1", today), _item("t2", today)], "c1"),
        "c1": _page([_item("t3", today), _item("t4", today)], "c2"),
        "c2": _page([_item("old1", yday), _item("old2", yday)], "c3"),  # 翻到前一日 → 停
        "c3": _page([_item("should_not_reach", yday)], "c4"),
    }
    calls = []

    def fake(sort_end=""):
        calls.append(sort_end)
        return pages[sort_end]

    monkeypatch.setattr(ps, "_fetch_page", fake)
    raw = ps._iter_stream(today, max_days=1)
    # 只收当日 4 条,前一日被丢
    assert [x["title"] for x in raw] == ["t1", "t2", "t3", "t4"]
    # 翻到含前一日的那页即停,不再翻 c3
    assert calls == ["", "c1", "c2"]


def test_paging_stops_on_stalled_cursor(monkeypatch):
    """游标不前进(重复)→ 停,防死循环。"""
    today = _today()
    seq = iter(["s1", "s1", "s1"])  # 游标重复

    def fake(sort_end=""):
        return _page([_item("x", today)], next(seq))

    monkeypatch.setattr(ps, "_fetch_page", fake)
    raw = ps._iter_stream(today, max_days=1)
    assert len(raw) == 2      # 首页 + 第二页收了,第二页游标重复后停


# ---------- as-of 防未来 ----------

def test_asof_excludes_future_items(monkeypatch):
    today = _today()
    tmr = (pd.Timestamp(today) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    def fake(sort_end=""):
        # 单页含未来+当日,游标置空即停
        return _page([_item("future", tmr), _item("now", today)], "")

    monkeypatch.setattr(ps, "_fetch_page", fake)
    raw = ps._iter_stream(today, max_days=1)
    titles = [x["title"] for x in raw]
    assert "future" not in titles          # 未来日剔除(防未来红线)
    assert titles == ["now"]


# ---------- BK 解析缓存 ----------

def test_bk_resolution_is_cached(store_dir, monkeypatch):
    n = {"calls": 0}

    def fake_fetch(bk_code):
        n["calls"] += 1
        return "存储芯片"

    monkeypatch.setattr(ps, "_fetch_bk_name", fake_fetch)
    a = ps._resolve_bk("BK1137")
    b = ps._resolve_bk("BK1137")          # 第二次命中缓存,不再请求
    assert a == b == "存储芯片"
    assert n["calls"] == 1


def test_bk_resolution_failure_returns_none(store_dir, monkeypatch):
    def boom(bk_code):
        raise RuntimeError("push2delay 挂了")

    monkeypatch.setattr(ps, "_fetch_bk_name", boom)
    assert ps._resolve_bk("BK0000") is None    # 失败返回 None、不抛、不缓存


# ---------- 打标:规则兜底 / 申万级 ----------

def test_no_stocklist_falls_back_to_rules():
    it = _item("国家出台半导体产业补贴新政", _today(), summary="发改委集成电路专项补贴")
    inds = ps._tag_one(it, use_llm=False)
    assert inds == ["电子"]                  # 半导体(概念)→to_sw→电子(申万一级)


def test_stocklist_tags_are_sw_level(monkeypatch):
    """stockList:BK 码经解析→to_sw、A 股码经 board_of,结果都是申万一级名。"""
    monkeypatch.setattr(ps, "_resolve_bk", lambda bk: {"BK1137": "存储芯片"}.get(bk))
    from tools.collectors import board
    monkeypatch.setattr(board, "board_of", lambda code: {"600519": "食品饮料"}.get(code))
    it = _item("盘面异动", _today(), stockList=["90.BK1137", "1.600519", "105.AMZN"])
    inds = ps._tag_one(it, use_llm=False)
    assert set(inds) == {"电子", "食品饮料"}       # 存储芯片→电子;600519→食品饮料;美股跳过
    assert all(x in SW_INDUSTRIES for x in inds)


def test_llm_fallback_off_by_default(monkeypatch):
    """默认 use_llm=False:无预打标 + 规则未命中时也不调 LLM。"""
    called = {"n": 0}
    monkeypatch.setattr(ps, "_llm_industries", lambda it: called.__setitem__("n", called["n"] + 1) or ["电子"])
    it = _item("与任何票池行业无关的国际政治新闻", _today())
    assert ps._tag_one(it, use_llm=False) == []
    assert called["n"] == 0
    # 打开开关才走
    assert ps._tag_one(it, use_llm=True) == ["电子"]
    assert called["n"] == 1


# ---------- 池过滤 ----------

def test_pool_filter_drops_offpool_industries(store_dir, monkeypatch):
    monkeypatch.setattr(ps.stock_pool, "get_pool", lambda: [Stock("000001", "n", "x", "电子")])
    monkeypatch.setattr(ps, "_iter_stream", lambda date, max_days: [
        _item("半导体补贴规划", _today(), summary="集成电路"),   # →电子(池内)
        _item("光通信板块规划", _today(), summary="光模块"),     # →通信(池外)
    ])
    out = ps.collect_stream(pool_only=True, archive=False)
    assert len(out) == 1
    assert out[0]["industries"] == ["电子"]


# ---------- 降级 ----------

def test_source_failure_degrades_to_empty(monkeypatch):
    def boom(sort_end=""):
        raise RuntimeError("np-weblist 炸了")

    monkeypatch.setattr(ps, "_fetch_page", boom)
    assert ps.collect_stream(archive=False) == []      # 降级为空,绝不 raise


# ---------- 合并进 policy 契约 ----------

def test_stream_merges_into_policy_file(store_dir, monkeypatch):
    """724 流经 fetch_policy 并入同一个 policy 文件,keyword="7x24流"、契约齐全、meta 记 724。"""
    # 关键词侧:一条国内政策
    kw_row = {"关键词": "半导体 补贴", "新闻标题": "半导体补贴新政", "新闻内容": "发改委集成电路专项",
              "发布时间": (pd.Timestamp.today() - pd.Timedelta(days=1)).strftime("%Y-%m-%d 09:00:00"),
              "文章来源": "证券时报", "新闻链接": "http://x/kw"}
    monkeypatch.setattr(pol, "_fetch_em", lambda kw: pd.DataFrame([kw_row]))
    # 724 侧:一条已打标契约
    stream_rec = {"date": _today(), "title": "美股存储芯片盘前走高", "source": "东财7x24",
                  "url": "http://x/724", "region": "国外", "summary": "盘面异动",
                  "industries": ["电子"], "keyword": "7x24流"}
    monkeypatch.setattr(pol, "_collect_stream", lambda days: [stream_rec])

    out = pol.fetch_policy(keywords=["半导体 补贴"], days=3650)
    by_kw = {r["keyword"] for r in out}
    assert "7x24流" in by_kw and "半导体 补贴" in by_kw
    s724 = next(r for r in out if r["keyword"] == "7x24流")
    assert set(s724) == {"date", "title", "source", "url", "region",
                         "summary", "industries", "keyword"}
    assert s724["industries"] == ["电子"] and s724["source"] == "东财7x24"
    m = store.get_raw_meta("policy", pol._policy_code(_today()))
    assert "724" in m["source"] and "eastmoney" in m["source"]


def test_stream_dedup_same_url(store_dir):
    """724 与关键词条目指同一 url → 去重(只留一条)。"""
    url = "http://x/dup"
    kw_rec_row = {"关键词": "芯片", "新闻标题": "同一事件", "新闻内容": "半导体",
                  "发布时间": _today() + " 09:00:00", "文章来源": "x", "新闻链接": url}
    stream_rec = {"date": _today(), "title": "同一事件", "source": "东财7x24", "url": url,
                  "region": "国内", "summary": "半导体", "industries": ["电子"], "keyword": "7x24流"}
    out = pol.tag_and_dump([kw_rec_row], days=3650, pretagged=[stream_rec])
    assert len([r for r in out if r["url"] == url]) == 1


# ---------- 混装口径不变量(防复发语义锁,统筹要求)----------

def test_industries_mixed_format_all_to_sw_recognizable():
    """policy 池 industries 混装 概念名(关键词路径)+ 申万一级名(724路径):

    锁死两条不变量,防后来者假设它是单一格式后又退回字面直比(词表漂移同类病):
      ① 池内任一行业名都能被 industry_map.to_sw 认得(概念名可映射 / 申万名幂等);
      ② 两种格式经 to_sw 归一后正确 rollup 到同一申万一级(此处「半导体」与「电子」都→电子)。
    """
    mixed_pool = [
        {"keyword": "半导体 补贴", "industries": ["半导体"]},   # 关键词路径:概念名
        {"keyword": "7x24流", "industries": ["电子"]},         # 724 路径:申万一级名
    ]
    # ① 每个行业名都可被 to_sw 认得(不 None)
    for rec in mixed_pool:
        for name in rec["industries"]:
            assert to_sw(name) is not None, f"{name} 不可归一,industries 混入了 to_sw 不认的名"
    # ② 经 to_sw 归一后正确 rollup:两条都落在「电子」
    rollup: dict[str, int] = {}
    for rec in mixed_pool:
        for name in rec["industries"]:
            sw = to_sw(name)
            rollup[sw] = rollup.get(sw, 0) + 1
    assert rollup == {"电子": 2}, f"混装格式经 to_sw 未正确 rollup: {rollup}"
    # 申万名幂等(724 路径写的就是它自己)
    assert to_sw("电子") == "电子"
