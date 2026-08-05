"""event.py 单测:三层归类/情感聚合(纯代码)+ LLM 缓存(mock);真实 LLM 用例可跳过。"""
import pytest

from tools.analysis import event as ev
from tools.llm import client as lc


def test_classify_layer():
    events = [{"事件类型": "政策"}, {"事件类型": "业绩"}, {"事件类型": "市场传闻"}, {"事件类型": "其他"}]
    ev.classify_events(events)
    assert [e["层"] for e in events] == ["政策", "公司行为", "舆情", "舆情"]


def test_aggregate_sentiment_excludes_irrelevant():
    events = [
        {"影响方向": "利好", "影响强度": 5, "与本股关系": "直接"},   # +1.0
        {"影响方向": "利空", "影响强度": 5, "与本股关系": "直接"},   # -1.0
        {"影响方向": "利好", "影响强度": 5, "与本股关系": "无关"},   # 剔除
        {"error": "x"},                                            # 剔除
    ]
    s = ev.aggregate_sentiment(events)
    assert s["样本数"] == 2 and s["利好数"] == 1 and s["利空数"] == 1
    assert s["净情绪分"] == 0.0        # +1 与 -1 抵消,均分 0


def test_aggregate_relation_weight():
    events = [{"影响方向": "利好", "影响强度": 4, "与本股关系": "间接"}]  # 4*0.5/5 = 0.4
    assert ev.aggregate_sentiment(events)["净情绪分"] == 0.4


class _FakeClient:
    def __init__(self):
        self.calls = 0

    def extract(self, text, schema, *, instruction, temperature=0.0):
        self.calls += 1
        return {"事件类型": "业绩", "影响方向": "利好", "影响强度": 4, "与本股关系": "直接"}


def test_cache_hit_avoids_second_call(monkeypatch, tmp_path):
    monkeypatch.setattr(ev.settings, "LLM_CACHE", tmp_path)
    c = _FakeClient()
    r1 = ev._cached_extract(c, "同一段文本", "同一指令")
    r2 = ev._cached_extract(c, "同一段文本", "同一指令")
    assert r1 == r2 and c.calls == 1        # 第二次命中缓存,不再调 LLM


@pytest.mark.skipif(not lc.is_configured(), reason="LLM env 未配置")
def test_live_extract_news():
    c = lc.get_client()
    r = c.extract("标题:公司获得政府补贴5000万元\n正文:利好公司业绩。",
                  {"影响方向": "利好/利空/中性", "影响强度": "1~5"},
                  instruction="抽取该新闻对公司的影响方向与强度。只输出JSON。")
    assert r.get("影响方向") in ("利好", "利空", "中性")
