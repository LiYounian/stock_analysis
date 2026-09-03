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
    # hermetic:资金流批级再扫的趟间冷却置 0(单测不真睡;趟数语义由专门用例锁)
    monkeypatch.setenv("ENRICH_FF_COOLDOWN", "0")
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


def test_fundflow_resweep_only_retries_residual(monkeypatch, fake):
    """资金流**批级再扫**:上游连接层限流的失败成簇(整批全挂→窗过后又整批可用),单票级
    retry 的秒级退避常整批落在同一冷却窗内 → 单趟采完仍大面积缺 fundflow(=「买入候选
    provenance.fundflow=false」硬伤的实际成因)。故按趟重试。

    锁三条语义(防未来重写把再扫删掉或退化成全量重采):
      · 首趟全挂的票会被**再采**(不是一趟失败就放弃);
      · 每趟只重采**上一趟真的没采到**的票——已采到的不再重复采(不重复烧网络/不覆盖已有数据);
      · 全采到后立即停扫(不空转到趟数上限)。
    """
    cand = ["002811", "603270", "688569"]
    monkeypatch.setenv("ENRICH_FF_SWEEPS", "3")
    _install(monkeypatch, fake, {c: [1] for c in cand}, {}, {c: {"x": 1} for c in cand},
             {c: {"x": 1} for c in cand}, {c: {"s": 1} for c in cand}, {c: {"a": 1} for c in cand})

    # 资金流桩:第 1 趟整批失败(模拟失败簇),第 2 趟 002811/603270 通、688569 仍挂
    sweeps: list[list[str]] = []

    def _ff(codes, *a, **k):
        sweeps.append(list(codes))
        fake.fetch_calls["fundflow"].extend(codes)
        if len(sweeps) == 1:
            return {}                                   # 失败簇:整批没采到
        got = {c: [1] for c in codes if c != "688569"}
        fake.d["fundflow"].update(got)
        return got
    monkeypatch.setattr(ff, "fetch_fundflow", _ff)

    rep = run.enrich_candidates(cand, "2026-09-02")

    assert len(sweeps) == 3, f"首趟全挂应继续再扫至趟数上限,实际扫了 {len(sweeps)} 趟"
    assert sweeps[0] == cand                            # 首趟全批
    assert sweeps[1] == cand                            # 首趟全挂 → 整批再采
    assert sweeps[2] == ["688569"], "第3趟只该重采仍缺的票,不该重采已采到的"
    # 再扫救回来的票标 ok;始终采不到的票显式降级 missing(不静默)
    assert rep["per_code"]["002811"]["fundflow"] == "ok"
    assert rep["per_code"]["603270"]["fundflow"] == "ok"
    assert rep["per_code"]["688569"]["fundflow"] == "missing"
    assert rep["degraded"]["688569"] == {"fundflow": "missing"}


def test_fundflow_resweep_stops_when_all_fetched(monkeypatch, fake):
    """再扫是**补缺**而非固定重采:首趟就全采到 → 只扫 1 趟,不空转到趟数上限。"""
    cand = ["002811", "603270"]
    monkeypatch.setenv("ENRICH_FF_SWEEPS", "3")
    _install(monkeypatch, fake, {c: [1] for c in cand}, {c: [1] for c in cand},
             {c: {"x": 1} for c in cand}, {c: {"x": 1} for c in cand},
             {c: {"s": 1} for c in cand}, {c: {"a": 1} for c in cand})
    sweeps: list[list[str]] = []
    _orig = ff.fetch_fundflow

    def _ff(codes, *a, **k):
        sweeps.append(list(codes))
        _orig(codes)
        return {c: [1] for c in codes}
    monkeypatch.setattr(ff, "fetch_fundflow", _ff)

    rep = run.enrich_candidates(cand, "2026-09-02")

    assert len(sweeps) == 1, f"首趟全采到就该停扫,实际扫了 {len(sweeps)} 趟"
    assert all(rep["per_code"][c]["fundflow"] == "ok" for c in cand)


def test_fundflow_resweep_wallclock_budget_caps_sweeps(monkeypatch, fake):
    """再扫受**墙钟预算**封顶:源全挂时不把闭环无限拖长(预算=0 → 只扫 1 趟即收手并显式降级)。"""
    cand = ["688569"]
    monkeypatch.setenv("ENRICH_FF_SWEEPS", "5")
    monkeypatch.setenv("ENRICH_FF_BUDGET", "0")
    _install(monkeypatch, fake, {"688569": [1]}, {}, {"688569": {"x": 1}},
             {"688569": {"x": 1}}, {"688569": {"s": 1}}, {"688569": {"a": 1}})
    sweeps: list[list[str]] = []

    def _ff(codes, *a, **k):
        sweeps.append(list(codes))
        return {}
    monkeypatch.setattr(ff, "fetch_fundflow", _ff)

    rep = run.enrich_candidates(cand, "2026-09-02")

    assert len(sweeps) == 1, f"预算用尽应立刻收手,实际扫了 {len(sweeps)} 趟"
    assert rep["per_code"]["688569"]["fundflow"] == "missing"   # 收手也要显式降级、不静默


def test_empty_candidate_set_safe(fake):
    """空候选集 → 安全返回(不崩、不采集)。"""
    rep = run.enrich_candidates([], "2026-09-02")
    assert rep["candidates"] == 0


# ————————————————————————————————————————————————
# 新鲜度不遮蔽:'stale' 三态(采过 ≠ 当日新鲜)
#
# 为什么有这组断言:skip-if-cached 的判据是"这票历史上采过没"(store date="latest"),
# 因此**连续入选的老候选会被跳过采集、新闻永不刷新**。本组锁住"覆盖报告必须如实报陈旧、
# 不得并进 ok",否则新鲜度问题被悄悄遮住。注意这只改**报告**,不改采集行为(成本账不变)。
# ————————————————————————————————————————————————

def _pin_resolved(monkeypatch, resolved_by_kind):
    """桩掉 store 的 date-pin 解析:{kind: 实际命中的分区日 或 None}。"""
    from tools.store import repo as store

    def _fake(kind, code, date=None):
        if kind not in resolved_by_kind:
            raise FileNotFoundError(kind)
        return ({"x": 1}, resolved_by_kind[kind], None)

    monkeypatch.setattr(store, "get_raw_resolved", _fake)


def test_命中更早分区报stale而非ok(monkeypatch, fake):
    cand = ["000560"]
    fake.d["news"]["000560"] = [1]                    # 已有缓存 → 采集会被跳过
    fake.d["financial"]["000560"] = {"x": 1}
    fake.d["fundflow"]["000560"] = {"x": 1}
    fake.d["sentiment"]["000560"] = {"s": 1, "新鲜度": event.FRESH}
    _install(monkeypatch, fake, {}, {}, {}, {}, {}, {})
    _pin_resolved(monkeypatch, {"news": "2026-09-01", "financial_report": "2026-09-02",
                                "fundflow": "2026-09-02"})

    rep = run.enrich_candidates(cand, "2026-09-02")

    st = rep["per_code"]["000560"]
    assert st["news"] == "stale", "命中 09-01 分区却按 09-02 富集，必须报 stale 而不是 ok"
    assert st["financial"] == "ok" and st["fundflow"] == "ok"


def test_stale计入降级明细与summary不被并进ok(monkeypatch, fake):
    cand = ["000560"]
    for dim, val in (("news", [1]), ("financial", {"x": 1}), ("fundflow", {"x": 1})):
        fake.d[dim]["000560"] = val
    fake.d["sentiment"]["000560"] = {"s": 1, "新鲜度": event.FRESH}
    _install(monkeypatch, fake, {}, {}, {}, {}, {}, {})
    _pin_resolved(monkeypatch, {"news": "2026-08-20", "financial_report": "2026-09-02",
                                "fundflow": "2026-09-02"})

    rep = run.enrich_candidates(cand, "2026-09-02")

    assert rep["counts"]["news"]["stale"] == 1
    assert rep["counts"]["news"]["ok"] == 0, "stale 不许并进 ok"
    assert rep["degraded"]["000560"]["news"] == "stale", "stale 必须进降级明细(可见性)"
    assert "stale1" in rep["summary"], "summary 必须把陈旧数报出来"


def test_情绪维复用情绪层新鲜度三态(monkeypatch, fake):
    cand = ["000731", "002274", "002906"]
    for c, fresh in zip(cand, (event.FRESH, event.STALE, event.NODATA)):
        for dim, val in (("news", [1]), ("financial", {"x": 1}), ("fundflow", {"x": 1})):
            fake.d[dim][c] = val
        fake.d["sentiment"][c] = {"s": 1, "新鲜度": fresh}
    _install(monkeypatch, fake, {}, {}, {}, {}, {}, {})
    _pin_resolved(monkeypatch, {"news": "2026-09-02", "financial_report": "2026-09-02",
                                "fundflow": "2026-09-02"})

    rep = run.enrich_candidates(cand, "2026-09-02")

    assert rep["per_code"]["000731"]["sentiment"] == "ok"
    assert rep["per_code"]["002274"]["sentiment"] == "stale", "情绪层标陈旧就得报 stale"
    assert rep["per_code"]["002906"]["sentiment"] == "empty", "情绪层标无数据就得报 empty"


def test_判不出新鲜度时退化为ok绝不比现状更差(monkeypatch, fake):
    """store 解析失败/该维无日期分区 → 不许降级成 stale(否则会把好数据误报成陈旧)。"""
    cand = ["600984"]
    for dim, val in (("news", [1]), ("financial", {"x": 1}), ("fundflow", {"x": 1})):
        fake.d[dim]["600984"] = val
    fake.d["sentiment"]["600984"] = {"s": 1, "新鲜度": event.FRESH}
    _install(monkeypatch, fake, {}, {}, {}, {}, {}, {})
    _pin_resolved(monkeypatch, {})                    # 全维解析失败

    rep = run.enrich_candidates(cand, "2026-09-02")

    st = rep["per_code"]["600984"]
    assert st["news"] == "ok" and st["financial"] == "ok" and st["fundflow"] == "ok"
    assert rep["counts"]["news"]["stale"] == 0


def test_no_llm下情绪维仍是skipped不受新鲜度改动影响(monkeypatch, fake):
    cand = ["002811"]
    for dim, val in (("news", [1]), ("financial", {"x": 1}), ("fundflow", {"x": 1})):
        fake.d[dim]["002811"] = val
    _install(monkeypatch, fake, {}, {}, {}, {}, {}, {})
    _pin_resolved(monkeypatch, {"news": "2026-09-02", "financial_report": "2026-09-02",
                                "fundflow": "2026-09-02"})

    rep = run.enrich_candidates(cand, "2026-09-02", no_llm=True)

    assert rep["per_code"]["002811"]["sentiment"] == "skipped_no_llm"
