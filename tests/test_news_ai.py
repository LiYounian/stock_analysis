"""统一「新闻+AI」视图单测:生产(enrich/write)+ 消费(data_access reader)。

锁定语义:
  - enrich_news 按索引对齐合并:每条含 ai.方向/强度/与本股关系/评论/原因,长度与原始新闻对齐;
  - 评论复用抽取「摘要」(空则「暂无」),原因复用抽取「原因」;
  - LLM 未配置 / 抽取抛错 / 缺字段 → ai 降级中性(方向=中性、强度=0),不崩;
  - data_access.news_list 优先读 news_ai;无则回退原始新闻并补空 ai;
  - news_flow 拍平全池并带 code/name/sector,按时间倒序。
路径隔离:tmp_path + monkeypatch store 路径根;set_active_date 固定日期,绝不污染真实 data/。
"""
import pytest

from tools.analysis import news_ai
from tools.store import repo
from web import data_access as da

_D = "2026-08-07"

_NEWS = [
    {"title": "A利好公告", "content": "正文A", "time": "2026-08-07 10:00",
     "source": "东财", "url": "http://a"},
    {"title": "B诉讼", "content": "正文B", "time": "2026-08-07 09:00",
     "source": "财联社电报", "url": ""},
]


@pytest.fixture
def store(tmp_path, monkeypatch):
    """隔离 store 路径根 + 固定运行日期(与 test_store.py 同款 fixture)。"""
    raw = tmp_path / "raw"
    analysis = tmp_path / "analysis"
    raw.mkdir()
    analysis.mkdir()
    monkeypatch.setattr(repo, "_RAW_DIR", raw)
    monkeypatch.setattr(repo, "_ANALYSIS_DIR", analysis)
    repo.set_active_date(_D)
    yield repo
    repo.set_active_date(None)


# —— 生产:enrich_news 合并对齐 ——
def test_enrich_news_merges_ai(monkeypatch):
    """原始新闻 + 抽取结果按索引对齐,ai 复用摘要/原因。"""
    fake_events = [
        {"影响方向": "利好", "影响强度": 4, "与本股关系": "直接",
         "摘要": "看好A", "原因": "获政府补贴"},
        {"影响方向": "利空", "影响强度": 3, "与本股关系": "间接",
         "摘要": "", "原因": ""},          # 摘要空 → 评论「暂无」
    ]
    monkeypatch.setattr(news_ai.store, "get_raw",
                        lambda kind, code, date="latest": list(_NEWS))
    monkeypatch.setattr(news_ai.event, "extract_news_events",
                        lambda code, client=None, limit=None: fake_events)

    out = news_ai.enrich_news("000021")
    assert len(out) == 2                                   # 长度与原始新闻对齐
    a, b = out
    assert a["title"] == "A利好公告" and a["content"] == "正文A"
    assert a["source"] == "东财" and a["url"] == "http://a"
    assert a["ai"] == {"方向": "利好", "强度": 4, "与本股关系": "直接",
                       "评论": "看好A", "原因": "获政府补贴"}
    assert b["ai"]["方向"] == "利空" and b["ai"]["强度"] == 3
    assert b["ai"]["评论"] == "暂无"                        # 摘要空 → 暂无


def test_enrich_news_degrades_on_llm_error(monkeypatch):
    """抽取抛错(如 LLM 未配置)→ 每条 ai 降级中性,长度仍对齐,不崩。"""
    def _boom(*a, **k):
        raise RuntimeError("LLM 未配置")

    monkeypatch.setattr(news_ai.store, "get_raw",
                        lambda kind, code, date="latest": list(_NEWS))
    monkeypatch.setattr(news_ai.event, "extract_news_events", _boom)

    out = news_ai.enrich_news("000021")
    assert len(out) == 2
    for o in out:
        assert o["ai"] == {"方向": "中性", "强度": 0, "与本股关系": "",
                           "评论": "", "原因": ""}


def test_enrich_news_degrades_bad_item(monkeypatch):
    """抽取整体成功但个别条目标 error / 缺方向 → 该条降级,不影响其它条。"""
    fake_events = [
        {"影响方向": "利好", "影响强度": 5, "与本股关系": "直接", "摘要": "强利好"},
        {"error": "解析失败"},
    ]
    monkeypatch.setattr(news_ai.store, "get_raw",
                        lambda kind, code, date="latest": list(_NEWS))
    monkeypatch.setattr(news_ai.event, "extract_news_events",
                        lambda code, client=None, limit=None: fake_events)

    out = news_ai.enrich_news("000021")
    assert out[0]["ai"]["方向"] == "利好" and out[0]["ai"]["原因"] == ""   # 无原因字段 → 空串
    assert out[1]["ai"]["方向"] == "中性" and out[1]["ai"]["强度"] == 0


def test_enrich_news_no_raw_returns_empty(monkeypatch):
    def _missing(*a, **k):
        raise FileNotFoundError("无新闻")

    monkeypatch.setattr(news_ai.store, "get_raw", _missing)
    assert news_ai.enrich_news("000021") == []


def test_write_news_ai_persists(store, monkeypatch):
    """write_news_ai 落盘 news_ai 按票视图,可经 store 读回。"""
    store.put_raw("news", "000021", list(_NEWS))
    monkeypatch.setattr(news_ai.event, "extract_news_events",
                        lambda code, client=None, limit=None: [
                            {"影响方向": "利好", "影响强度": 4, "与本股关系": "直接",
                             "摘要": "看好", "原因": "补贴"},
                            {"影响方向": "中性", "影响强度": 1, "与本股关系": "无关",
                             "摘要": "无关", "原因": ""},
                        ])
    n = news_ai.write_news_ai(["000021"])
    assert n == 1
    got = store.get_code_view("news_ai", "000021")
    assert len(got) == 2 and got[0]["ai"]["方向"] == "利好"


# —— 消费:data_access reader ——
def test_news_list_prefers_news_ai(store):
    enriched = [{"title": "A", "time": "t", "source": "s", "url": "u", "content": "c",
                 "ai": {"方向": "利好", "强度": 4, "与本股关系": "直接",
                        "评论": "x", "原因": "y"}}]
    store.put_code_view("news_ai", "000021", enriched)
    got = da.news_list("000021", _D)
    assert got == enriched
    assert got[0]["ai"]["方向"] == "利好"


def test_news_list_fallback_raw_empty_ai(store):
    """无 news_ai → 回退原始新闻,补空 ai(方向=中性)。"""
    store.put_raw("news", "000600", [{"title": "B", "content": "cc",
                                      "time": "t2", "source": "s2", "url": "u2"}])
    got = da.news_list("000600", _D)
    assert len(got) == 1
    assert got[0]["title"] == "B"
    assert got[0]["ai"] == {"方向": "中性", "强度": 0, "与本股关系": "",
                            "评论": "", "原因": ""}


def test_news_list_both_missing_returns_empty(store):
    assert da.news_list("999999", _D) == []


def test_news_detail_by_index(store):
    enriched = [{"title": "A", "ai": {"方向": "利好"}},
                {"title": "B", "ai": {"方向": "利空"}}]
    store.put_code_view("news_ai", "000021", enriched)
    assert da.news_detail("000021", 1, _D)["title"] == "B"
    assert da.news_detail("000021", 9, _D) is None       # 越界 → None


def test_news_flow_flattens_pool(store):
    """news_flow 拍平全池:带 code/name/sector,按时间倒序。"""
    store.put_record({"meta": {"code": "000021", "name": "票A", "sector": "半导体"},
                      "signals": None, "events": []})
    store.put_record({"meta": {"code": "000600", "name": "票B", "sector": "电力"},
                      "signals": None, "events": []})
    store.put_code_view("news_ai", "000021", [
        {"title": "A新闻", "time": "2026-08-07 10:00", "source": "s", "url": "u",
         "content": "c", "ai": {"方向": "利好"}}])
    store.put_code_view("news_ai", "000600", [
        {"title": "B新闻", "time": "2026-08-07 11:00", "source": "s", "url": "u",
         "content": "c", "ai": {"方向": "利空"}}])
    flow = da.news_flow(_D)
    assert len(flow) == 2
    assert flow[0]["time"] > flow[1]["time"]             # 时间倒序
    top = flow[0]
    assert top["code"] == "000600" and top["name"] == "票B" and top["sector"] == "电力"
    assert top["ai"]["方向"] == "利空"
