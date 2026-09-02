"""候选定向富集 enrich_candidates 单测:保证"会被深度分析/推荐的候选票"一定有系统全套
消息面+财报+资金流数据(修"被推荐买入的票反而没有系统数据支撑"的硬伤)。

锁语义(为什么这么写,防未来重写误删规则):
  · **幂等/skip-if-cached**:各维只补尚无缓存的票,已缓存票不重采(重跑不重复烧钱、不破坏已有数据)。
  · **富集后候选票拿到数据**:采到后覆盖报告标 'ok'。
  · **缺数据降级标记不静默**:源当日无数据 → 'empty';采集失败/未落盘 → 'missing';二者都进
    `degraded` 明细,绝不静默当作已覆盖。
  · **no_llm 透传**:数据-only 模式跳过 LLM 情绪 + 财报文本层,sentiment 维标 'skipped_no_llm'。

hermetic:全程 monkeypatch 假缓存 + 计数桩,不触网、不调 LLM。
"""
import pytest

from tools import run
from tools.analysis import event
from tools.collectors import annual_report as ar
from tools.collectors import financial as fin
from tools.collectors import fundflow as ff
from tools.collectors import news


class _FakeStore:
    """按维度存 code→value 的假缓存;load 缺失抛 FileNotFoundError;fetch 把 need 子集写入。"""

    def __init__(self):
        self.d = {"news": {}, "fundflow": {}, "financial": {}, "annual": {}, "sentiment": {}}
        self.fetch_calls = {"news": [], "fundflow": [], "financial": [], "annual": [],
                            "sentiment": [], "fin_text": []}

    def loader(self, dim):
        def _load(code, *a, **k):
            store = self.d[dim]
            if code not in store:
                raise FileNotFoundError(code)
            return store[code]
        return _load


@pytest.fixture
def fake(monkeypatch):
    fs = _FakeStore()
    # —— skip-if-cached 判定用的 loader ——
    monkeypatch.setattr(news, "load_news", fs.loader("news"))
    monkeypatch.setattr(ff, "load_fundflow", fs.loader("fundflow"))
    monkeypatch.setattr(fin, "load_financial", fs.loader("financial"))
    monkeypatch.setattr(ar, "load_annual_report", fs.loader("annual"))
    monkeypatch.setattr(event, "load_sentiment", fs.loader("sentiment"))

    # —— 采集/计算桩:记录被采票 + 按 supply 决定写入哪些(模拟源可得/不可得)——
    def _mk(dim, supply):
        def _f(codes, *a, **k):
            fs.fetch_calls[dim].extend(codes)
            for c in codes:
                if c in supply:
                    fs.d[dim][c] = supply[c]
            return {}
        return _f

    fs._mk = _mk
    return fs


def _install(monkeypatch, fs, news_supply, ff_supply, fin_supply, ar_supply,
             sent_supply, ann_supply):
    """把 run 里的采集入口换成写假缓存的桩。ann=年报,sent=情绪(run_sentiment 落 sentiment)。"""
    monkeypatch.setattr(run, "collect_message", fs._mk("news", news_supply))
    monkeypatch.setattr(ff, "fetch_fundflow", fs._mk("fundflow", ff_supply))
    monkeypatch.setattr(run, "run_financial_collect", fs._mk("financial", fin_supply))
    monkeypatch.setattr(run, "run_annual_report", fs._mk("annual", ann_supply))
    monkeypatch.setattr(run, "run_sentiment", fs._mk("sentiment", sent_supply))
    monkeypatch.setattr(run, "run_financial_text",
                        lambda codes, as_of: fs.fetch_calls["fin_text"].extend(codes))


def test_candidate_gets_all_four_dims(monkeypatch, fake):
    """核心验收:全缺数据的候选票,富集后 news/sentiment/financial/fundflow 均落地(报告标 ok)。"""
    cand = ["002811", "603270", "688569"]
    supply = {c: [1] for c in cand}          # 源当日均可得
    _install(monkeypatch, fake, supply, {c: [1] for c in cand}, {c: {"x": 1} for c in cand},
             {c: {"x": 1} for c in cand}, {c: {"s": 1} for c in cand}, {c: {"a": 1} for c in cand})

    rep = run.enrich_candidates(cand, "2026-09-02")

    assert rep["candidates"] == 3
    for c in cand:
        st = rep["per_code"][c]
        assert st["news"] == "ok" and st["sentiment"] == "ok"
        assert st["financial"] == "ok" and st["fundflow"] == "ok", f"{c} 仍缺数据:{st}"
    assert rep["degraded"] == {}             # 无降级


def test_skip_if_cached_idempotent(monkeypatch, fake):
    """幂等:已缓存的票不重采(不进 fetch 名单);只补尚无缓存的票。"""
    # 002811 已全缓存;603270 全缺
    fake.d["news"]["002811"] = [1]
    fake.d["fundflow"]["002811"] = [1]
    fake.d["financial"]["002811"] = {"x": 1}
    fake.d["annual"]["002811"] = {"x": 1}
    fake.d["sentiment"]["002811"] = {"s": 1}
    cand = ["002811", "603270"]
    _install(monkeypatch, fake, {"603270": [1]}, {"603270": [1]}, {"603270": {"x": 1}},
             {"603270": {"x": 1}}, {"603270": {"s": 1}}, {"603270": {"x": 1}})

    rep = run.enrich_candidates(cand, "2026-09-02")

    # 已缓存的 002811 不进任何 fetch 名单(skip-if-cached)
    for dim in ("news", "fundflow", "financial", "annual"):
        assert "002811" not in fake.fetch_calls[dim], f"{dim} 不应重采已缓存的 002811"
        assert "603270" in fake.fetch_calls[dim], f"{dim} 应补采缺失的 603270"
    # 两票最终都 ok(幂等:已缓存的保留、缺失的补上)
    assert rep["per_code"]["002811"]["news"] == "ok"
    assert rep["per_code"]["603270"]["financial"] == "ok"


def test_missing_source_marks_degraded_not_silent(monkeypatch, fake):
    """源当日不可得 → 显式降级标记(missing/empty)并进 degraded 明细,绝不静默当已覆盖。"""
    cand = ["688569"]
    # 新闻源返回空列表(empty);资金流采集失败未落盘(missing);财报/年报/情绪均缺
    _install(monkeypatch, fake, {"688569": []}, {}, {}, {}, {}, {})

    rep = run.enrich_candidates(cand, "2026-09-02")

    st = rep["per_code"]["688569"]
    assert st["news"] == "empty"             # 源确无 → empty(合法降级)
    assert st["fundflow"] == "missing"       # 采集失败未落盘 → missing
    assert st["financial"] == "missing"
    assert st["sentiment"] == "missing"
    # 降级明细含该票、四维都在(不静默)
    assert "688569" in rep["degraded"]
    assert set(rep["degraded"]["688569"]) == {"news", "fundflow", "financial", "sentiment"}


def test_no_llm_skips_llm_layers(monkeypatch, fake):
    """no_llm=True → 不调 run_sentiment / run_financial_text;sentiment 维标 skipped_no_llm。"""
    cand = ["002811"]
    _install(monkeypatch, fake, {"002811": [1]}, {"002811": [1]}, {"002811": {"x": 1}},
             {"002811": {"x": 1}}, {"002811": {"s": 1}}, {"002811": {"x": 1}})

    rep = run.enrich_candidates(cand, "2026-09-02", no_llm=True)

    assert fake.fetch_calls["sentiment"] == []     # run_sentiment 未被调
    assert fake.fetch_calls["fin_text"] == []      # run_financial_text 未被调
    assert rep["per_code"]["002811"]["sentiment"] == "skipped_no_llm"
    # 数据类维(news/financial/fundflow)仍富集
    assert rep["per_code"]["002811"]["news"] == "ok"
    assert rep["per_code"]["002811"]["fundflow"] == "ok"


def test_empty_candidate_set_safe(fake):
    """空候选集 → 安全返回(不崩、不采集)。"""
    rep = run.enrich_candidates([], "2026-09-02")
    assert rep["candidates"] == 0
