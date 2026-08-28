"""event.py 单测:三层归类/情感聚合(纯代码)+ LLM 缓存(mock);真实 LLM 用例可跳过。

含 L1 抽取提速(并行/顺序/失败隔离/缓存命中/条数上限)hermetic 测试:全 mock LLM,不触网。
"""
import re
import threading
import time

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


# ---------- L1 抽取提速:并行 / 顺序 / 失败隔离 / 缓存 / 条数上限 ----------
class _SeqClient:
    """假 LLM:可 sleep 模拟 I/O 延迟;从文本回显序号;可对指定序号抛错;线程安全计数。"""

    def __init__(self, delay: float = 0.0, fail_idx: set[int] | None = None):
        self.delay = delay
        self.fail_idx = fail_idx or set()
        self.calls = 0
        self._lock = threading.Lock()

    def extract(self, text, schema, *, instruction, temperature=0.0):
        with self._lock:
            self.calls += 1
        idx = int(re.search(r"标题:n(\d+)", text).group(1))
        if self.delay:
            time.sleep(self.delay)
        if idx in self.fail_idx:
            raise RuntimeError(f"boom-{idx}")
        return {"事件类型": "业绩", "影响方向": "利好", "影响强度": 3,
                "与本股关系": "直接", "摘要": "s", "原因": "r", "echo": idx}


def _fake_news(n: int) -> list[dict]:
    # load_news 已按 time 倒序;这里 n0 最新在前(模拟倒序),供 limit 取最近 N 校验
    return [{"title": f"n{i}", "content": "c", "time": f"2026-08-{10 - i:02d}",
             "source": "src", "url": f"u{i}"} for i in range(n)]


def _prep(monkeypatch, tmp_path, items, workers=8):
    monkeypatch.setattr(ev.settings, "LLM_CACHE", tmp_path)
    monkeypatch.setattr(ev.settings, "LLM_EXTRACT_WORKERS", workers)
    monkeypatch.setattr(ev.nw, "load_news", lambda code: list(items))
    monkeypatch.setattr(ev.stock_pool, "get", lambda code: None)


def test_extract_parallel_is_concurrent(monkeypatch, tmp_path):
    """并发确实并发:8 条各 sleep 0.1s,并行总耗时应远小于串行(<一半)。"""
    items = _fake_news(8)
    _prep(monkeypatch, tmp_path, items, workers=8)
    c = _SeqClient(delay=0.1)
    t0 = time.perf_counter()
    events = ev.extract_news_events("000001", client=c)
    elapsed = time.perf_counter() - t0
    assert len(events) == 8
    assert elapsed < 0.4        # 串行需 ~0.8s;并行应远小于一半(留足抖动余量)


def test_extract_preserves_input_order(monkeypatch, tmp_path):
    """结果按输入 items 顺序回填(index 对齐),即便完成顺序被并发打乱。"""
    items = _fake_news(12)
    _prep(monkeypatch, tmp_path, items, workers=8)
    c = _SeqClient(delay=0.02)
    events = ev.extract_news_events("000001", client=c)
    assert [e["echo"] for e in events] == list(range(12))
    assert [e["标题"] for e in events] == [f"n{i}" for i in range(12)]


def test_extract_failure_isolation(monkeypatch, tmp_path):
    """单条抛错 → 该条标 error,其余正常,不中断整批。"""
    items = _fake_news(5)
    _prep(monkeypatch, tmp_path, items, workers=8)
    c = _SeqClient(fail_idx={2})
    events = ev.extract_news_events("000001", client=c)
    assert "error" in events[2] and "boom-2" in events[2]["error"]
    assert all("error" not in events[i] for i in (0, 1, 3, 4))


def test_extract_cache_hit_no_recall(monkeypatch, tmp_path):
    """相同 (指令+文本) 第二次命中文件缓存,不再调真 client(计数器断言)。"""
    items = _fake_news(4)
    _prep(monkeypatch, tmp_path, items, workers=8)
    c = _SeqClient()
    ev.extract_news_events("000001", client=c)
    assert c.calls == 4
    ev.extract_news_events("000001", client=c)   # 全命中缓存
    assert c.calls == 4                           # 无新增调用


def test_extract_limit_caps_calls(monkeypatch, tmp_path):
    """传 >limit 条,只抽取最近 limit 条(倒序在前)→ 调用次数 == limit。"""
    items = _fake_news(10)
    _prep(monkeypatch, tmp_path, items, workers=8)
    c = _SeqClient()
    events = ev.extract_news_events("000001", client=c, limit=4)
    assert c.calls == 4 and len(events) == 4
    assert [e["echo"] for e in events] == [0, 1, 2, 3]   # 最近 4 条(倒序前 4)


def test_trimmed_schema_downstream_ok(monkeypatch, tmp_path):
    """schema 精简后,下游归类 + 情感聚合仍跑通不 KeyError。"""
    items = _fake_news(3)
    _prep(monkeypatch, tmp_path, items, workers=4)
    c = _SeqClient()
    events = ev.classify_events(ev.extract_news_events("000001", client=c))
    agg = ev.aggregate_sentiment(events)
    assert agg["样本数"] == 3 and agg["利好数"] == 3
    assert all(e["层"] == "公司行为" for e in events)     # 事件类型=业绩 → 公司行为


def test_workers_one_is_serial(monkeypatch, tmp_path):
    """workers<=1 退化串行,结果与并行一致(顺序 + 内容)。"""
    items = _fake_news(5)
    _prep(monkeypatch, tmp_path, items, workers=1)
    c = _SeqClient()
    events = ev.extract_news_events("000001", client=c)
    assert [e["echo"] for e in events] == list(range(5))


# ---------- 消息持续性研判接入(attach_persistence,hermetic)----------
def _persist_events():
    """构造已抽取+归类的新闻事件:2 公司行为 + 1 舆情 + 1 error 公司行为。"""
    return [
        {"事件类型": "业绩", "层": "公司行为", "影响方向": "利好", "标题": "n0"},
        {"事件类型": "市场传闻", "层": "舆情", "影响方向": "中性", "标题": "n1"},
        {"事件类型": "合作", "层": "公司行为", "影响方向": "利空", "标题": "n2"},
        {"error": "boom", "层": "公司行为", "标题": "n3"},
    ]


def _persist_items():
    return [{"title": f"n{i}", "content": f"c{i}"} for i in range(4)]


class _FakePersist:
    """假 classify_batch:按索引回不同持续性,记录收到的文本(验证只喂公司行为&≤t)。"""
    def __init__(self):
        self.seen_texts = []

    def classify_batch(self, texts, client=None, workers=None):
        self.seen_texts = list(texts)
        out = []
        for t in texts:                      # n0=结构性利好, n2=短暂
            if "n0" in t:
                out.append({"持续性": "结构性持续", "方向": "利好", "印证强度": "强", "依据": "在手订单饱满"})
            else:
                out.append({"持续性": "短暂事件", "方向": "利空", "印证强度": "弱", "依据": "一次性"})
        return out


def _patch_persist(monkeypatch, fake):
    import tools.analysis.news_persistence as npst
    monkeypatch.setattr(npst, "classify_batch", fake.classify_batch)
    monkeypatch.setattr(ev.settings, "SENTIMENT_PERSISTENCE_ON", True)


def test_attach_persistence_writes_fields_on_company_events_only(monkeypatch):
    events, items, fake = _persist_events(), _persist_items(), _FakePersist()
    _patch_persist(monkeypatch, fake)
    roll = ev.attach_persistence(events, items)
    # 只对无 error 的公司行为事件(index 0,2)分类,喂给分类器的正好这两条文本
    assert fake.seen_texts == ["标题:n0\n正文:c0", "标题:n2\n正文:c2"]
    assert events[0]["持续性"] == "结构性持续" and events[0]["印证强度"] == "强"
    assert events[0]["持续性方向"] == "利好" and events[0]["持续性依据"] == "在手订单饱满"
    assert events[2]["持续性"] == "短暂事件"
    # 舆情(1)与 error 公司行为(3)不写任何持续性字段
    assert "持续性" not in events[1] and "持续性" not in events[3]
    # rollup 计数正确
    assert roll == {"结构性利好数": 1, "结构性利空数": 0, "短暂事件数": 1,
                    "已分类数": 2, "最强结构印证": "强"}


def test_attach_persistence_does_not_touch_frozen_aggregate(monkeypatch):
    events, items, fake = _persist_events(), _persist_items(), _FakePersist()
    before = ev.aggregate_sentiment(events)         # 附加前的净情绪/样本数
    _patch_persist(monkeypatch, fake)
    ev.attach_persistence(events, items)
    after = ev.aggregate_sentiment(events)          # 附加后应完全一致(冻结)
    assert before == after


def test_attach_persistence_no_future_function_only_pinned_texts(monkeypatch):
    """无未来函数:分类器只收到传入(≤t)items 的文本,条数=公司行为条数,不多喂。"""
    events, items, fake = _persist_events(), _persist_items(), _FakePersist()
    _patch_persist(monkeypatch, fake)
    ev.attach_persistence(events, items)
    assert len(fake.seen_texts) == 2                # 恰好 2 条公司行为,无越界/无外部拉取
    # index 对齐:喂进去的文本正是 items[0]/items[2] 的 title+content
    assert all(x in ("标题:n0\n正文:c0", "标题:n2\n正文:c2") for x in fake.seen_texts)


def test_attach_persistence_off_switch_writes_nothing(monkeypatch):
    events, items, fake = _persist_events(), _persist_items(), _FakePersist()
    import tools.analysis.news_persistence as npst
    monkeypatch.setattr(npst, "classify_batch", fake.classify_batch)
    monkeypatch.setattr(ev.settings, "SENTIMENT_PERSISTENCE_ON", False)
    roll = ev.attach_persistence(events, items)
    assert roll == {} and fake.seen_texts == []     # 关闭:不调分类器
    assert all("持续性" not in e for e in events)


def test_attach_persistence_all_degraded_returns_empty(monkeypatch):
    """分类器全降级(持续性=None)→ 不写字段、rollup 空(优雅退化)。"""
    events, items = _persist_events(), _persist_items()

    class _Deg:
        def classify_batch(self, texts, client=None, workers=None):
            return [{"持续性": None, "error": "x"} for _ in texts]
    import tools.analysis.news_persistence as npst
    monkeypatch.setattr(npst, "classify_batch", _Deg().classify_batch)
    monkeypatch.setattr(ev.settings, "SENTIMENT_PERSISTENCE_ON", True)
    assert ev.attach_persistence(events, items) == {}
    assert all("持续性" not in e for e in events)


@pytest.mark.skipif(not lc.is_configured(), reason="LLM env 未配置")
def test_live_extract_news():
    c = lc.get_client()
    r = c.extract("标题:公司获得政府补贴5000万元\n正文:利好公司业绩。",
                  {"影响方向": "利好/利空/中性", "影响强度": "1~5"},
                  instruction="抽取该新闻对公司的影响方向与强度。只输出JSON。")
    assert r.get("影响方向") in ("利好", "利空", "中性")
