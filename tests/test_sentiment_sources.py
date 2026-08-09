"""情绪三层接入单测(mock LLM,不联网):政策打分 + UGC 情感 + analyze_stock 三层整合。

全程 monkeypatch 隔离到 tmp_path,不污染真实 data/。断言锁住:
- 政策打分落盘结构 + 缓存命中(不重复调 LLM)
- UGC 情感返回 {净情绪, 多空} 结构 + 降级
- analyze_stock 让 sentiment 覆盖 新闻+舆情+政策 三层 + 三层加权口径
"""
import json

import pytest

from tools.analysis import event as ev
from tools.collectors import policy as pol
from tools.collectors import ugc as ug
from tools.store import repo as store


# ---------- 公共:隔离 data/ + mock LLM ----------
class _FakeClient:
    """按 schema 内容返回固定结果,并计数调用次数(验缓存命中)。"""

    def __init__(self):
        self.calls = 0

    def extract(self, text, schema, *, instruction, temperature=0.0):
        self.calls += 1
        if "受影响行业" in schema:                     # 政策打分 schema
            return {"影响方向": "利好", "影响强度": 4, "受影响行业": ["半导体"]}
        if "净情绪" in schema:                          # UGC 情感 schema
            return {"净情绪": 0.6, "多空": "偏多", "依据": "多数看多"}
        return {"事件类型": "业绩", "影响方向": "利好",  # 新闻抽取 schema
                "影响强度": 4, "与本股关系": "直接", "摘要": "x"}


@pytest.fixture
def isolate(monkeypatch, tmp_path):
    """LLM 缓存指 tmp;情绪/政策产出经 store,故 monkeypatch store 目录 + 固定运行日期。"""
    monkeypatch.setattr(ev.settings, "LLM_CACHE", tmp_path / "llm_cache")
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    store.set_active_date("2026-08-06")
    yield tmp_path
    store.set_active_date(None)


def _fake_policy(monkeypatch, items):
    monkeypatch.setattr(pol, "load_policy", lambda date=None: items)


def _fake_ugc(monkeypatch, code_to_posts):
    def _load(code):
        if code in code_to_posts:
            return code_to_posts[code]
        raise FileNotFoundError(code)
    monkeypatch.setattr(ug, "load_ugc", _load)


def _fake_news(monkeypatch, code_to_news):
    monkeypatch.setattr(ev.nw, "load_news",
                        lambda code: code_to_news.get(code, []))


# ---------- 政策打分 ----------
def test_score_policy_structure_and_persist(isolate, monkeypatch):
    _fake_policy(monkeypatch, [
        {"title": "国家出台半导体扶持政策", "summary": "补贴大幅提升",
         "industries": ["半导体"], "source": "新华社", "url": "u1",
         "date": "2026-08-01", "keyword": "半导体 政策"},
    ])
    c = _FakeClient()
    scored = ev.score_policy(client=c)

    assert len(scored) == 1
    r = scored[0]
    assert r["影响方向"] == "利好" and r["影响强度"] == 4
    assert r["受影响行业"] == ["半导体"] and r["层"] == "政策"
    assert r["title"] == "国家出台半导体扶持政策"     # 原字段保留

    # 落盘存在且结构一致(经 store 视图)
    assert store.get_view("sentiment_policy") == scored


def test_score_policy_cache_hit(isolate, monkeypatch):
    _fake_policy(monkeypatch, [
        {"title": "同一政策", "summary": "同一摘要", "industries": ["半导体"]},
    ])
    c = _FakeClient()
    ev.score_policy(client=c)
    ev.score_policy(client=c)                          # 二次:命中缓存
    assert c.calls == 1


def test_score_policy_no_cache_degrades(isolate, monkeypatch):
    def _raise(date=None):
        raise FileNotFoundError("无政策缓存")
    monkeypatch.setattr(pol, "load_policy", _raise)
    scored = ev.score_policy(client=_FakeClient())
    assert scored == []                                # 降级不崩
    assert store.get_view("sentiment_policy") == []    # 仍落空视图


# ---------- UGC 情感 ----------
def test_ugc_sentiment_structure(isolate, monkeypatch):
    _fake_ugc(monkeypatch, {"002156": [
        {"text": "这票要起飞"}, {"text": "基本面很好"},
    ]})
    r = ev.ugc_sentiment("002156", client=_FakeClient())
    assert r["净情绪"] == 0.6 and r["多空"] == "偏多"
    assert r["样本数"] == 2 and "依据" in r


def test_ugc_sentiment_no_cache_degrades(isolate, monkeypatch):
    _fake_ugc(monkeypatch, {})                         # 无任何 UGC 缓存
    r = ev.ugc_sentiment("002156", client=_FakeClient())
    assert r["净情绪"] == 0.0 and r["多空"] == "中性"
    assert r["样本数"] == 0 and r["degraded"] == "no_ugc_cache"


def test_ugc_sentiment_clamps(isolate, monkeypatch):
    _fake_ugc(monkeypatch, {"002156": [{"text": "x"}]})

    class _Over(_FakeClient):
        def extract(self, text, schema, *, instruction, temperature=0.0):
            self.calls += 1
            return {"净情绪": 5.0, "多空": "偏多", "依据": "y"}    # 越界 → clamp 到 1

    assert ev.ugc_sentiment("002156", client=_Over())["净情绪"] == 1.0


# ---------- analyze_stock 三层整合 ----------
def test_analyze_stock_three_layers(isolate, monkeypatch):
    code = "002156"                                    # 半导体
    # 池无关:analyze_stock 用 stock_pool.get(code).sector 匹配政策行业;002156 已不在 live 自选池,
    # 固定它的行业为半导体(否则砍池后取不到行业→政策层空→三层断言挂)。
    from tools.config.stock_pool import Stock
    monkeypatch.setattr(ev.stock_pool, "get",
                        lambda c: Stock("002156", "通富微电", "半导体封测", "半导体") if c == code else None)
    _fake_news(monkeypatch, {code: [
        {"title": "公司中标5亿", "content": "利好业绩", "time": "2026-08-01",
         "source": "东财", "url": "n1"},
    ]})
    _fake_ugc(monkeypatch, {code: [{"text": "看多"}, {"text": "冲"}]})
    # 政策缓存 → 先打分落盘
    _fake_policy(monkeypatch, [
        {"title": "半导体利好政策", "summary": "补贴", "industries": ["半导体"],
         "date": "2026-08-01"},
    ])
    c = _FakeClient()
    ev.score_policy(client=c)

    rec = ev.analyze_stock(code, client=c)
    s = rec["sentiment"]

    # 三层俱全
    assert set(s["三层"]) == {"新闻", "舆情", "政策"}
    assert s["三层"]["新闻"]["样本数"] == 1
    assert s["三层"]["舆情"]["样本数"] == 2 and s["三层"]["舆情"]["多空"] == "偏多"
    assert s["三层"]["政策"]["样本数"] == 1
    assert "口径" in s

    # events 含政策来源条目(层=政策)
    assert any(e.get("层") == "政策" for e in rec["events"])
    assert any(e.get("事件类型") == "政策" for e in rec["events"])

    # 三层加权:三层皆正 → 总分为正,且在各层净值之间
    news_net = s["三层"]["新闻"]["净情绪"]
    ugc_net = s["三层"]["舆情"]["净情绪"]
    pol_net = s["三层"]["政策"]["净情绪"]
    assert s["净情绪分"] > 0
    assert min(news_net, ugc_net, pol_net) <= s["净情绪分"] <= max(news_net, ugc_net, pol_net)

    # 落盘存在(经 store 按票视图)
    assert store.get_code_view("sentiment", code)["code"] == code


def test_analyze_stock_backward_compat_news_only(isolate, monkeypatch):
    """无 UGC / 无政策缓存时:退化为纯新闻净情绪(向后兼容)。"""
    code = "002156"
    _fake_news(monkeypatch, {code: [
        {"title": "利好", "content": "中标", "time": "t", "source": "s", "url": "u"},
    ]})
    _fake_ugc(monkeypatch, {})                         # 无 UGC
    # 不调 score_policy → 政策缓存缺失

    rec = ev.analyze_stock(code, client=_FakeClient())
    s = rec["sentiment"]
    assert s["三层"]["舆情"]["样本数"] == 0
    assert s["三层"]["政策"]["样本数"] == 0
    # 缺舆情/政策层 → 加权重归一后等于纯新闻净情绪
    assert s["净情绪分"] == s["三层"]["新闻"]["净情绪"]
    # 旧字段仍在(新闻口径)
    assert {"利好数", "利空数", "样本数"} <= set(s)
