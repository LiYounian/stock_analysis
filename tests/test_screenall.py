"""全A 多策略选股(screenall)单测:多策略选出并集 → 只对(并集∪自选)做新闻/LLM。

锁语义(为什么这么写,防未来重写误删):
  · llm_subset = 各在产策略选出 picks 并集 ∪ 自选池(去重保序)。
  · 最贵的新闻/LLM(collect_message/run_sentiment)**只对 llm_subset**,不对全A codes_all(省 token 命门)。
  · 单个 screener 抛错被 _safe 隔离 → 其余照跑、不崩(降级到只用能跑通的策略选出票)。
  · --no-llm(no_llm=True)→ 不调 collect_message / run_sentiment(纯数据快跑)。
  · S01 趋势深跌反包 / 箱体3 箱体形态已因显著负下线,不再进本编排(本测试不再桩接、不再断言其在产)。

hermetic:全程 monkeypatch,在产 run_*_screen 返回小假 view,阶段②重活替换为计数桩,不触网。
"""
import pytest

from tools import run
from tools.pipeline import (screen_conditional_rank, screen_council, screen_max_range,
                            screen_momentum, screen_reversal_turnover, screen_s02,
                            screen_semi_factor, screen_strong, screen_volume)


@pytest.fixture(autouse=True)
def _isolate_analysis(analysis_tmpdir):
    """本文件所有测试:analysis 落盘根切到 tmp_path(见 conftest.analysis_tmpdir)。

    为什么必须有:run_screen_all 是**真编排**,收尾还挂着 多策略命中闸门 / 龙虎榜记分卡 等
    落盘步。桩接再全也难保未来新增的收尾步不落盘,而 data/analysis 是 git 跟踪目录——
    这条 fixture 是"只切 IO 入口"的兜底,产物进临时目录、跑完即弃。
    """


def _stub_stage2(monkeypatch, calls):
    """把阶段②所有重活替换为「记录首个位置参数(codes)」的计数桩;K线主档同步/回测置空。

    候选定向富集 enrich_candidates 也桩接为计数桩(额外记录 no_llm),并返回合法覆盖报告 dict
    (run_screen_all 收尾会读 report['candidates']/['summary']),其内部 news/LLM 分维由
    test_enrich_candidates.py 独立锁语义。

    收尾三步(多策略命中闸门 / 当日龙虎榜采集 / 龙虎榜前向记分卡)同样桩掉:它们会真触网、
    并把 `多策略命中闸门.json` 与 `data/analysis/backtest/lhb_forward_scorecard.csv` 落进
    **git 跟踪**的 data/analysis(记分卡那条走模块常量路径,不经 _ANALYSIS_DIR,只能桩)。
    本文件锁的是"贵活只对 llm_subset"的编排语义,这三步各有独立单测,在此不需要真跑。
    """
    monkeypatch.setattr(run.master_sync, "sync_master", lambda codes, as_of=None: {"mode": "test", "ok": len(codes)})
    monkeypatch.setattr(run.stock_pool, "get_codes", lambda: ["WATCH1", "WATCH2"])
    monkeypatch.setattr(run, "run_backtest", lambda: None)

    def rec(name):
        def _f(*a, **k):
            calls.append((name, list(a[0]) if a and isinstance(a[0], (list, tuple)) else None))
        return _f

    def _enrich(codes, as_of, no_llm=False):
        calls.append(("enrich_candidates", list(codes), {"no_llm": no_llm}))
        return {"candidates": len(codes), "counts": {}, "per_code": {},
                "degraded": {}, "summary": "stub"}

    for fn in ("collect_values_missing", "collect_market_context",
               "collect_ticks", "run_serialize", "run_events", "run_factor",
               "run_council", "run_panel", "run_screen",
               "collect_lhb", "_update_lhb_scorecard"):
        monkeypatch.setattr(run, fn, rec(fn))
    monkeypatch.setattr(run, "run_multi_gate", lambda picks_by_view, as_of: {})
    monkeypatch.setattr(run, "enrich_candidates", _enrich)


def _stub_screeners(monkeypatch, picks_by_strategy, raise_for=None, edges_by_strategy=None):
    """monkeypatch 在产 run_*_screen 返回小假 view;raise_for 指定的策略抛错(测 _safe 隔离)。

    picks_by_strategy: {"council":[...], "s02":[...], "momentum":[...]}
    edges_by_strategy(#23,可选): {"council":[...], ...} → 给该 view 加 `边缘候选`(排序型才落此字段);
      默认 None → 不加(退回旧行为,analysis_set==llm_subset)。
    council 落 `top`(打分型),其余落 `入选清单`(规则型)——与真实脚本字段一致。
    (S01/箱体3 已下线,不再桩接。)

    **9 条在产 screener 全部桩接**:只桩这 3 条时,其余 6 条会在假 codes 上真跑——半导体多因子
    还带 fetch=True(触网),且各自把 view 落进 git 跟踪的 data/analysis/<as_of>/。它们在
    本文件里的语义贡献恒为"选出 0 只"(假 codes 选不出票),故桩成空 view 后断言完全等价,
    只是不再触网/不再落盘。各 screener 自身的选股语义由其独立单测锁。
    """
    edges_by_strategy = edges_by_strategy or {}

    def mk(view_key, codes, raising, edges=None):
        def _f(codes_all, as_of=None, fetch=True, **k):
            if raising:
                raise RuntimeError("screener boom")
            items = [{"code": c} for c in codes]
            view = {view_key: items}
            if edges:
                view["边缘候选"] = list(edges)
            return view
        return _f

    raise_for = raise_for or set()
    monkeypatch.setattr(screen_council, "run_council_screen",
                        mk("top", picks_by_strategy["council"], "council" in raise_for,
                           edges_by_strategy.get("council")))
    monkeypatch.setattr(screen_s02, "run_s02_screen",
                        mk("入选清单", picks_by_strategy["s02"], "s02" in raise_for,
                           edges_by_strategy.get("s02")))
    monkeypatch.setattr(screen_momentum, "run_momentum_screen",
                        mk("入选清单", picks_by_strategy["momentum"], "momentum" in raise_for,
                           edges_by_strategy.get("momentum")))
    # 其余在产 screener:桩成"选出 0 只"的空 view(与它们在假 codes 上的真实贡献一致)
    for mod, fname in ((screen_semi_factor, "run_semi_factor_screen"),
                       (screen_max_range, "run_max_range_screen"),
                       (screen_volume, "run_volume_screen"),
                       (screen_strong, "run_strong_screen"),
                       (screen_reversal_turnover, "run_reversal_turnover_screen"),
                       (screen_conditional_rank, "run_conditional_rank_screen")):
        monkeypatch.setattr(mod, fname, mk("入选清单", [], False))


def test_llm_subset_is_union_of_picks_and_watchlist(monkeypatch):
    """llm_subset == 在产策略 picks 并集 ∪ 自选池(去重保序);新闻/LLM 只对 llm_subset。"""
    calls: list[tuple] = []
    _stub_stage2(monkeypatch, calls)
    _stub_screeners(monkeypatch, {
        "council": ["C1", "C2"],
        "s02": ["C1", "S2"],        # C1 与 council 重叠 → 去重只一次
        "momentum": ["M1", "WATCH1"],  # WATCH1 已在自选 → 去重只一次
    })

    out = run.run_screen_all(["A", "B", "C"], "2026-08-11")

    # union = 各在产策略 picks 并集(去重保序;编排顺序 council→s02→momentum)
    assert out["union_picks"] == ["C1", "C2", "S2", "M1", "WATCH1"]
    # llm_subset = union ∪ 自选(WATCH1 已在 union → 不重复,WATCH2 追加)
    assert out["llm_subset_codes"] == ["C1", "C2", "S2", "M1", "WATCH1", "WATCH2"]

    # 候选定向富集 + 组装/合议 + 逐笔归档 全部只对 llm_subset,不是全A codes_all
    #  (SCREENALL_ENRICH_TOPK 默认 0 → 候选富集集 == llm_subset,airtight:所有 record 票都被富集)
    for name in ("enrich_candidates", "collect_values_missing",
                 "collect_ticks", "run_serialize", "run_council", "run_panel", "run_screen"):
        arg = next(a for n, a, *_ in calls if n == name)
        assert arg == out["llm_subset_codes"], f"{name} 应只对 llm_subset,实际 {arg}"
        assert "A" not in arg and "B" not in arg          # 全A codes 未泄漏到贵活

    # 顺序命门:候选定向富集(新闻/情绪/财报/资金流)与逐笔归档都必须排在 run_serialize **之前**,
    # 否则 serialize 组装 record 时读不到当日消息面/财报/资金流/逐笔摘要 → record 空、个股页卡片装不上。
    order = [n for n, *_ in calls]
    assert order.index("collect_ticks") < order.index("run_serialize"), \
        "collect_ticks 必须在 run_serialize 之前(serialize 的 tick 块按 as_of 读当日逐笔摘要)"
    assert order.index("enrich_candidates") < order.index("run_serialize"), \
        "enrich_candidates 必须在 run_serialize 之前(serialize 读候选票已采消息面/财报/资金流组装 record)"


def test_enrich_topk_bounds_candidate_set(monkeypatch):
    """SCREENALL_ENRICH_TOPK=M>0 → 候选富集集收窄为 每策略前 M ∪ 自选(限成本模式);
    serialize/合议仍对全 llm_subset(不缩记录),只是贵活富集集更小。"""
    monkeypatch.setenv("SCREENALL_ENRICH_TOPK", "1")
    calls: list[tuple] = []
    _stub_stage2(monkeypatch, calls)
    _stub_screeners(monkeypatch, {
        "council": ["C1", "C2"],       # 前1 → C1
        "s02": ["S1", "S2"],           # 前1 → S1
        "momentum": ["M1", "M2"],      # 前1 → M1
    })

    out = run.run_screen_all(["A"], "2026-08-11")

    # llm_subset 仍是全并集∪自选(记录不缩)
    assert out["llm_subset_codes"] == ["C1", "C2", "S1", "S2", "M1", "M2", "WATCH1", "WATCH2"]
    # 候选富集集 = 每策略前1 ∪ 自选(C2/S2/M2 被排除)
    enrich_arg = next(a for n, a, *_ in calls if n == "enrich_candidates")
    assert enrich_arg == ["C1", "S1", "M1", "WATCH1", "WATCH2"]
    # serialize 仍对全 llm_subset(不因富集集收窄而缩记录)
    ser_arg = next(a for n, a, *_ in calls if n == "run_serialize")
    assert ser_arg == out["llm_subset_codes"]


# ———————————— #23 深采分层门控:边缘候选拿数值面深采,新闻/LLM 不扩 ————————————
def _numeric_face_calls(calls):
    """6 类数值面深采 + 组装/合议/横表 的入参(应对 analysis_set,含边缘候选)。"""
    return ("collect_values_missing", "collect_ticks", "run_serialize",
            "run_events", "run_factor", "run_council", "run_panel", "run_screen")


def test_edge_candidates_get_numeric_face_but_not_llm(monkeypatch):
    """#23 核心语义(守则6):边缘候选进 analysis_set 拿 6 类数值面深采(fundflow/chip/holder_num/
    block_trade/tick/consensus + serialize/事件/因子/合议/横表);新闻/LLM 情绪(enrich_candidates)
    坚决**不扩**、仍只对 llm_subset(cand_set)。断开"未选中→缺数据→弃权→软收缩降权→更不选中"的环。"""
    monkeypatch.setattr(run.settings, "SCREENALL_EDGE_MAX", 300)
    calls: list[tuple] = []
    _stub_stage2(monkeypatch, calls)
    _stub_screeners(monkeypatch, {
        "council": ["C1", "C2"], "s02": [], "momentum": ["M1"],
    }, edges_by_strategy={
        "council": ["E1", "E2", "C1"],   # C1 已入选 → 去重不重复计入边缘
        "momentum": ["E2", "E3"],        # E2 与 council 边缘重叠 → 去重
    })

    out = run.run_screen_all(["A"], "2026-08-11")

    # llm_subset = union ∪ 自选(不含边缘候选)
    assert out["llm_subset_codes"] == ["C1", "C2", "M1", "WATCH1", "WATCH2"]
    # 边缘候选 = 各策略边缘并集去重、剔除已在 llm_subset 的(C1)→ E1/E2/E3
    assert out["edge_candidates"] == ["E1", "E2", "E3"]
    # analysis_set = llm_subset ∪ 边缘候选
    assert out["analysis_set_codes"] == ["C1", "C2", "M1", "WATCH1", "WATCH2", "E1", "E2", "E3"]

    # 数值面深采 + 组装/合议/横表 全对 analysis_set(含边缘候选)
    for name in _numeric_face_calls(calls):
        arg = next(a for n, a, *_ in calls if n == name)
        assert arg == out["analysis_set_codes"], f"{name} 应对 analysis_set(含边缘),实际 {arg}"
        assert "E1" in arg and "E2" in arg and "E3" in arg, f"{name} 缺边缘候选"

    # 新闻/LLM(enrich_candidates)坚决不扩:只对 cand_set(==llm_subset),边缘候选一个不进
    enrich_arg = next(a for n, a, *_ in calls if n == "enrich_candidates")
    assert enrich_arg == out["llm_subset_codes"]
    for e in ("E1", "E2", "E3"):
        assert e not in enrich_arg, "新闻/LLM 情绪不得扩到边缘候选(控成本)"


def test_edge_set_bounded_by_global_cap(monkeypatch):
    """#23 成本硬约束:边缘候选全局受 SCREENALL_EDGE_MAX 封顶(防对全A深采)。"""
    monkeypatch.setattr(run.settings, "SCREENALL_EDGE_MAX", 2)     # 全局只留 2 只边缘
    calls: list[tuple] = []
    _stub_stage2(monkeypatch, calls)
    _stub_screeners(monkeypatch, {
        "council": ["C1"], "s02": [], "momentum": [],
    }, edges_by_strategy={"council": ["E1", "E2", "E3", "E4", "E5"]})

    out = run.run_screen_all(["A"], "2026-08-11")

    # 5 只边缘候选被全局上界砍到 2 只
    assert out["edge_candidates"] == ["E1", "E2"]
    assert out["analysis_set_codes"] == ["C1", "WATCH1", "WATCH2", "E1", "E2"]
    ser_arg = next(a for n, a, *_ in calls if n == "run_serialize")
    assert ser_arg == out["analysis_set_codes"]


def test_edge_cap_zero_disables_tiered_gate(monkeypatch):
    """SCREENALL_EDGE_MAX=0 → 分层门控关闭,analysis_set 退回 llm_subset(旧行为,一键回退)。"""
    monkeypatch.setattr(run.settings, "SCREENALL_EDGE_MAX", 0)
    calls: list[tuple] = []
    _stub_stage2(monkeypatch, calls)
    _stub_screeners(monkeypatch, {
        "council": ["C1"], "s02": [], "momentum": [],
    }, edges_by_strategy={"council": ["E1", "E2"]})

    out = run.run_screen_all(["A"], "2026-08-11")

    assert out["edge_candidates"] == []
    assert out["analysis_set_codes"] == out["llm_subset_codes"]
    for name in _numeric_face_calls(calls):
        arg = next(a for n, a, *_ in calls if n == name)
        assert arg == out["llm_subset_codes"]


def test_edges_from_view_extractor():
    """_edges_from_view 兼容有/无 边缘候选 字段;None/非 list 安全返回空。"""
    assert run._edges_from_view({"边缘候选": ["A", "B"]}) == ["A", "B"]
    assert run._edges_from_view({"入选清单": [{"code": "X"}]}) == []   # 信号型无此字段
    assert run._edges_from_view({"边缘候选": [1, "B", None, ""]}) == ["B"]  # 脏元素过滤
    assert run._edges_from_view(None) == []
    assert run._edges_from_view({"边缘候选": "notlist"}) == []


def test_one_screener_failure_isolated(monkeypatch):
    """某 screener 抛错 → _safe 隔离,其余策略选出票仍进 union、流水线不崩。"""
    calls: list[tuple] = []
    _stub_stage2(monkeypatch, calls)
    _stub_screeners(monkeypatch, {
        "council": ["C1"], "s02": ["S2"], "momentum": ["M1"],
    }, raise_for={"s02"})                                  # 策略2 崩

    out = run.run_screen_all(["A"], "2026-08-11")

    # 崩掉的 s02 贡献 0,其余策略 picks 仍在 union
    assert "S2" not in out["union_picks"]
    assert out["union_picks"] == ["C1", "M1"]
    assert out["各策略入选"]["策略2·放量后缩量回踩"] == 0
    # 阶段②照常跑(未中止)
    assert any(n == "run_council" for n, *_ in calls)


def test_no_llm_propagates_to_enrich(monkeypatch):
    """--no-llm → 透传 no_llm=True 给候选定向富集(其内部跳过 LLM 情绪/财报文本);数据类步骤仍跑。"""
    calls: list[tuple] = []
    _stub_stage2(monkeypatch, calls)
    _stub_screeners(monkeypatch, {
        "council": ["C1"], "s02": [], "momentum": [],
    })

    run.run_screen_all(["A"], "2026-08-11", no_llm=True)

    names = [n for n, *_ in calls]
    assert "enrich_candidates" in names
    # 候选富集收到 no_llm=True(其内部据此跳过 LLM 情绪 + 财报文本层)
    enrich = next(c for c in calls if c[0] == "enrich_candidates")
    assert enrich[2] == {"no_llm": True}
    assert "collect_values_missing" in names              # 数据补缺仍跑
    assert "run_serialize" in names and "run_council" in names


def test_picks_extractor_handles_top_and_selected():
    """_picks_from_view 兼容 top(打分型)/ 入选清单(规则型),None/缺字段安全返回空。"""
    assert run._picks_from_view({"top": [{"code": "X"}, {"code": "Y"}]}) == ["X", "Y"]
    assert run._picks_from_view({"入选清单": [{"code": "Z"}]}) == ["Z"]
    assert run._picks_from_view(None) == []               # screener 被 _safe 隔离失败
    assert run._picks_from_view({}) == []
    assert run._picks_from_view({"入选清单": [{"no_code": 1}]}) == []


def test_picks_extractor_handles_rank_view():
    """_picks_from_view 兼容排行型 view(策略11):无 入选清单/top,从 排行 各维度榜取 code 并集去重保序。"""
    view = {
        "策略": "指标条件化状态排序",
        "排行": {
            "1日": [{"code": "A", "上涨概率%": 60}, {"code": "B"}],
            "5日": [{"code": "B"}, {"code": "C"}],          # B 与 1日 重复 → 去重
            "10日": [{"code": "C"}, {"code": "D"}],         # C 重复
        },
    }
    # 保序去重:按 1日→5日→10日 首次出现顺序
    assert run._picks_from_view(view) == ["A", "B", "C", "D"]
    # 排行型元素为 code 串也健壮
    assert run._picks_from_view({"排行": {"1日": ["E", "F"], "5日": ["E"]}}) == ["E", "F"]
    # 空排行 / 排行非 dict → 空
    assert run._picks_from_view({"排行": {}}) == []
    assert run._picks_from_view({"排行": []}) == []


def test_picks_extractor_prefers_selected_over_rank():
    """入选清单/top 存在时优先走原分支,排行分支不生效(原有两种 view 行为不变)。"""
    view = {"入选清单": [{"code": "Z"}], "排行": {"1日": [{"code": "A"}]}}
    assert run._picks_from_view(view) == ["Z"]
    view2 = {"top": [{"code": "T"}], "排行": {"1日": [{"code": "A"}]}}
    assert run._picks_from_view(view2) == ["T"]


# ———————————————————— 流式增量推送(模块化抗断点)————————————————————
def test_stream_incremental_push_per_strategy_and_record(monkeypatch):
    """每策略 view 落盘即推该 view 分片(`__view__:名`);run_council 后按批推 record 分片(code)。"""
    calls = []
    _stub_stage2(monkeypatch, calls)
    pushes = []
    monkeypatch.setattr(run, "_push_incremental", lambda date, keys: pushes.append((date, set(keys))))
    _stub_screeners(monkeypatch, {"council": ["C1"], "s02": ["S2"], "momentum": ["M1"]})
    run.run_screen_all(["A"], "2026-08-11")
    allkeys = [k for _, ks in pushes for k in ks]
    # 层1:各策略 view 分片增量推(至少 council/momentum 落盘的 view)
    assert "__view__:策略0合议" in allkeys
    assert "__view__:动量组合" in allkeys
    # 层2:run_council 后按批推 record 分片(非 __view__ 前缀 = code 分片)
    assert [k for k in allkeys if not k.startswith("__view__:")], "run_council 后应按批推 record"


def test_push_incremental_swallows_errors(monkeypatch):
    """best-effort:upload 抛错也不向上抛(闭环不被增量推拖垮)。"""
    from tools.config import settings
    from tools.sync import upload
    for k, v in (("STREAM_PUSH", True), ("SYNC_INGEST_URL", "http://x"),
                 ("SYNC_INGEST_TOKEN", "t"), ("SYNC_SIGNING_KEY", "k")):
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr(upload, "upload_date", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    run._push_incremental("2026-08-11", {"__view__:x"})   # 不抛即通过


def test_push_incremental_skips_without_creds(monkeypatch):
    """无同步凭证 → 跳过,不调 upload(直接 screenall 未加载 sync.env 场景)。"""
    from tools.config import settings
    from tools.sync import upload
    monkeypatch.setattr(settings, "STREAM_PUSH", True)
    monkeypatch.setattr(settings, "SYNC_INGEST_URL", "")
    called = []
    monkeypatch.setattr(upload, "upload_date", lambda *a, **k: called.append(1) or {})
    run._push_incremental("2026-08-11", {"__view__:x"})
    assert not called


def test_push_incremental_disabled_switch(monkeypatch):
    """STREAM_PUSH=false → 完全不推(一键回退旧的末尾统一 upload 行为)。"""
    from tools.config import settings
    from tools.sync import upload
    for k, v in (("STREAM_PUSH", False), ("SYNC_INGEST_URL", "http://x"),
                 ("SYNC_INGEST_TOKEN", "t"), ("SYNC_SIGNING_KEY", "k")):
        monkeypatch.setattr(settings, k, v)
    called = []
    monkeypatch.setattr(upload, "upload_date", lambda *a, **k: called.append(1) or {})
    run._push_incremental("2026-08-11", {"__view__:x"})
    assert not called
