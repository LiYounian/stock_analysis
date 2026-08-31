"""全A 多策略选股(screenall)单测:多策略选出并集 → 只对(并集∪自选)做新闻/LLM。

锁语义(为什么这么写,防未来重写误删):
  · llm_subset = 各在产策略选出 picks 并集 ∪ 自选池(去重保序)。
  · 最贵的新闻/LLM(collect_message/run_sentiment)**只对 llm_subset**,不对全A codes_all(省 token 命门)。
  · 单个 screener 抛错被 _safe 隔离 → 其余照跑、不崩(降级到只用能跑通的策略选出票)。
  · --no-llm(no_llm=True)→ 不调 collect_message / run_sentiment(纯数据快跑)。
  · S01 趋势深跌反包 / 箱体3 箱体形态已因显著负下线,不再进本编排(本测试不再桩接、不再断言其在产)。

hermetic:全程 monkeypatch,在产 run_*_screen 返回小假 view,阶段②重活替换为计数桩,不触网。
"""
from tools import run
from tools.pipeline import screen_council, screen_momentum, screen_s02


def _stub_stage2(monkeypatch, calls):
    """把阶段②所有重活替换为「记录首个位置参数(codes)」的计数桩;K线主档同步/回测置空。"""
    monkeypatch.setattr(run.master_sync, "sync_master", lambda codes, as_of=None: {"mode": "test", "ok": len(codes)})
    monkeypatch.setattr(run.stock_pool, "get_codes", lambda: ["WATCH1", "WATCH2"])
    monkeypatch.setattr(run, "run_backtest", lambda: None)

    def rec(name):
        def _f(*a, **k):
            calls.append((name, list(a[0]) if a and isinstance(a[0], (list, tuple)) else None))
        return _f

    for fn in ("collect_values_missing", "collect_message", "collect_market_context",
               "collect_ticks", "run_sentiment", "run_serialize", "run_events", "run_factor",
               "run_council", "run_panel", "run_screen"):
        monkeypatch.setattr(run, fn, rec(fn))


def _stub_screeners(monkeypatch, picks_by_strategy, raise_for=None):
    """monkeypatch 在产 run_*_screen 返回小假 view;raise_for 指定的策略抛错(测 _safe 隔离)。

    picks_by_strategy: {"council":[...], "s02":[...], "momentum":[...]}
    council 落 `top`(打分型),其余落 `入选清单`(规则型)——与真实脚本字段一致。
    (S01/箱体3 已下线,不再桩接;其余未桩接的 screener 在假 codes 上经 _safe 隔离贡献空。)
    """
    def mk(view_key, codes, raising):
        def _f(codes_all, as_of=None, fetch=True, **k):
            if raising:
                raise RuntimeError("screener boom")
            items = [{"code": c} for c in codes]
            return {view_key: items}
        return _f

    raise_for = raise_for or set()
    monkeypatch.setattr(screen_council, "run_council_screen",
                        mk("top", picks_by_strategy["council"], "council" in raise_for))
    monkeypatch.setattr(screen_s02, "run_s02_screen",
                        mk("入选清单", picks_by_strategy["s02"], "s02" in raise_for))
    monkeypatch.setattr(screen_momentum, "run_momentum_screen",
                        mk("入选清单", picks_by_strategy["momentum"], "momentum" in raise_for))


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

    # 新闻/LLM/组装/合议 + 逐笔归档 全部只对 llm_subset,不是全A codes_all
    for name in ("collect_message", "run_sentiment", "collect_values_missing",
                 "collect_ticks", "run_serialize", "run_council", "run_panel", "run_screen"):
        arg = next(a for n, a in calls if n == name)
        assert arg == out["llm_subset_codes"], f"{name} 应只对 llm_subset,实际 {arg}"
        assert "A" not in arg and "B" not in arg          # 全A codes 未泄漏到贵活

    # 顺序命门(合作者注释强调):逐笔归档 collect_ticks 必须排在 run_serialize **之前**,
    # 否则 serialize 的 tick 块读不到当日摘要、record/个股页卡片装不上。
    order = [n for n, _ in calls]
    assert order.index("collect_ticks") < order.index("run_serialize"), \
        "collect_ticks 必须在 run_serialize 之前(serialize 的 tick 块按 as_of 读当日逐笔摘要)"


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
    assert any(n == "run_council" for n, _ in calls)


def test_no_llm_skips_message_and_sentiment(monkeypatch):
    """--no-llm → 不调 collect_message / run_sentiment;数据类步骤仍跑。"""
    calls: list[tuple] = []
    _stub_stage2(monkeypatch, calls)
    _stub_screeners(monkeypatch, {
        "council": ["C1"], "s02": [], "momentum": [],
    })

    run.run_screen_all(["A"], "2026-08-11", no_llm=True)

    names = [n for n, _ in calls]
    assert "collect_message" not in names                 # 新闻跳过
    assert "run_sentiment" not in names                   # LLM 情绪跳过
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
