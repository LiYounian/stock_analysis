"""编排接线单测(合议全链路):步骤顺序、事件采集降级、council 二次附着纳入多因子/事件驱动。

锁语义:council 数据生产者(factor 横截面 + 事件精数值)必须排在 serialize 之后、council 回写之前;
        采集失败降级不炸;reattach 后多因子/事件驱动专家不再弃权。
"""
import pandas as pd

from tools import run
from tools.analysis import serialize
from tools.contracts.expert import validate_verdict


# ———————————— cmd_all 步骤顺序 ————————————
def test_cmd_all_step_order(monkeypatch):
    """council 回写在 serialize/events/factor 之后;panel/screen 在 council 之后。"""
    calls = []

    def rec(name):
        def _f(*a, **k):
            calls.append(name)
        return _f

    monkeypatch.setattr(run, "_prep", lambda argv: (["000001", "000002"], "2026-08-08"))
    monkeypatch.setattr(run, "collect_values", rec("collect_values"))
    monkeypatch.setattr(run, "collect_message", rec("collect_message"))
    monkeypatch.setattr(run, "run_sentiment", rec("run_sentiment"))
    monkeypatch.setattr(run, "run_serialize", rec("run_serialize"))
    monkeypatch.setattr(run, "run_events", rec("run_events"))
    monkeypatch.setattr(run, "run_factor", rec("run_factor"))
    monkeypatch.setattr(run, "run_council", rec("run_council"))
    monkeypatch.setattr(run, "run_panel", rec("run_panel"))
    monkeypatch.setattr(run, "run_screen", rec("run_screen"))
    monkeypatch.setattr(run, "run_backtest", rec("run_backtest"))

    run.cmd_all([])

    # 关键顺序断言
    assert calls.index("run_serialize") < calls.index("run_events")
    assert calls.index("run_serialize") < calls.index("run_factor")
    assert calls.index("run_events") < calls.index("run_council")
    assert calls.index("run_factor") < calls.index("run_council")
    assert calls.index("run_council") < calls.index("run_panel")
    assert calls.index("run_council") < calls.index("run_screen")
    # 回测是闭环收尾:排在 screen 之后(读已累积的达标池快照)
    assert calls.index("run_screen") < calls.index("run_backtest")
    assert calls[-1] == "run_backtest"


# ———————————— 回测接进闭环收尾:调用 + 优雅降级 + 幂等 ————————————
def test_run_backtest_calls_run_and_store_offline(monkeypatch):
    """run_backtest 只调用 backtest_summary.run_and_store,离线口径 fetch=False。"""
    from tools.backtest import backtest_summary as bs
    got = {}

    def fake_run_and_store(*a, **k):
        got.update(k)
        return {"达标日数": 0, "样本数": 0, "状态": "待观察"}

    monkeypatch.setattr(bs, "run_and_store", fake_run_and_store)
    run.run_backtest()
    assert got.get("fetch") is False            # 收尾步不新增网络依赖
    assert got.get("generated_at")              # 注入时间戳便于复现


def test_backtest_step_degrades_no_crash(monkeypatch):
    """回测步抛错 → _safe 吞掉,cmd_all 收尾不中止(回测是可选增强)。"""
    noop = lambda *a, **k: None
    monkeypatch.setattr(run, "_prep", lambda argv: (["000001"], "2026-08-08"))
    for name in ("collect_values", "collect_message", "collect_market_context",
                 "run_sentiment", "run_serialize", "run_events", "run_factor",
                 "run_council", "run_panel", "run_screen"):
        monkeypatch.setattr(run, name, noop)

    def boom():
        raise RuntimeError("回测算不动(缺缓存)")

    monkeypatch.setattr(run, "run_backtest", boom)
    run.cmd_all([])                              # 回测炸,但闭环不应抛


def test_two_stage_runs_backtest_at_tail(monkeypatch):
    """两阶段流水线收尾也调回测步(在 screen 之后)。"""
    calls = []
    monkeypatch.setattr(run.master_sync, "sync_master", lambda c, **k: {"mode": "spot", "ok": 0})

    class _SP:
        @staticmethod
        def run_pattern_screen(codes, as_of=None, fetch=False):
            return {"达标清单": [], "接近达标": {}, "有效样本": 0, "候选数": 0}

    monkeypatch.setitem(__import__("sys").modules, "tools.pipeline.screen_pattern", _SP)
    monkeypatch.setattr(run.stock_pool, "get_codes", lambda: [])
    for name in ("collect_values_missing", "collect_message", "collect_market_context",
                 "run_sentiment", "run_serialize", "run_events", "run_factor",
                 "run_council", "_enrich_near_miss", "run_panel"):
        monkeypatch.setattr(run, name, lambda *a, **k: 0)
    monkeypatch.setattr(run, "run_screen", lambda *a, **k: calls.append("run_screen"))
    monkeypatch.setattr(run, "run_backtest", lambda: calls.append("run_backtest"))

    run.run_two_stage(["000001"], "2026-08-08", no_llm=True)
    assert "run_backtest" in calls
    assert calls.index("run_screen") < calls.index("run_backtest")


# ———————————— 百度新闻前向滚存接入 collect_message ————————————
def _stub_message_sources(monkeypatch):
    """把 collect_message 里的其它源全部存根成 no-op,只留 baidu 观察。"""
    monkeypatch.setattr(run.news, "fetch_news", lambda codes, **k: {})
    monkeypatch.setattr(run.ugc, "fetch_ugc", lambda codes, **k: {})
    monkeypatch.setattr(run.policy, "fetch_policy", lambda *a, **k: [])


def test_collect_message_wires_baidu_same_scope(monkeypatch):
    """百度采集挂进 collect_message,且对同一 codes(news_subset)——不另开全A范围。"""
    _stub_message_sources(monkeypatch)
    monkeypatch.setattr(run.settings, "BAIDU_NEWS_COLLECT", True)
    got = {}
    monkeypatch.setattr(run.baidu_news, "fetch_baidu_news",
                        lambda codes, **k: got.setdefault("codes", list(codes)) or {c: [] for c in codes})
    run.collect_message(["000001", "600000"])
    assert got["codes"] == ["000001", "600000"]        # 采集范围 == 传入子集,非全A


def test_collect_message_baidu_failure_no_crash(monkeypatch):
    """百度采集抛错 → _safe 吞掉,collect_message(闭环)不中止。"""
    _stub_message_sources(monkeypatch)
    monkeypatch.setattr(run.settings, "BAIDU_NEWS_COLLECT", True)

    def boom(codes, **k):
        raise RuntimeError("百度被限流")

    monkeypatch.setattr(run.baidu_news, "fetch_baidu_news", boom)
    run.collect_message(["000001"])                    # 不抛即通过


def test_collect_message_baidu_toggle_off(monkeypatch):
    """BAIDU_NEWS_COLLECT=False → 完全不调百度采集(限流时可关)。"""
    _stub_message_sources(monkeypatch)
    monkeypatch.setattr(run.settings, "BAIDU_NEWS_COLLECT", False)
    called = {"n": 0}

    def spy(codes, **k):
        called["n"] += 1
        return {}

    monkeypatch.setattr(run.baidu_news, "fetch_baidu_news", spy)
    run.collect_message(["000001"])
    assert called["n"] == 0


def test_recent_quarter_ends():
    qs = run._recent_quarter_ends("2026-08-08", 2)
    assert qs == ["20260630", "20260331"]           # as_of 前最近 2 个季度末
    assert all(len(q) == 8 for q in qs)


# ———————————— 事件采集降级不炸 ————————————
def test_run_events_degrade_no_crash(monkeypatch):
    """collectors 返回空(东财被墙/akshare 缺失)→ run_events 不抛。"""
    from tools.collectors import event_driven as ed
    monkeypatch.setattr(ed, "fetch_earnings_forecast", lambda period, kind: pd.DataFrame())
    monkeypatch.setattr(ed, "fetch_insider_trades", lambda tag="latest": pd.DataFrame())
    monkeypatch.setattr(ed, "fetch_management_change", lambda tag="latest": pd.DataFrame())
    run.run_events(["000001"], "2026-08-08")          # 不抛即通过


def test_run_events_survives_exception(monkeypatch):
    """即便采集函数抛异常,run_events 也应吞掉(降级纪律)——这里验证 collectors 已内建 try/except。"""
    from tools.collectors import event_driven as ed
    # 直接验证 collectors 层:akshare 不可用路径返回空 df,不抛
    monkeypatch.setattr(ed, "_akshare", lambda: None)
    assert ed.fetch_earnings_forecast("20260630", "yjyg").empty
    assert ed.fetch_insider_trades("latest").empty


# ———————————— reattach_council 纳入多因子/事件驱动 ————————————
def _synthetic_record(code, roe, pe):
    return {"schema_version": "1.0",
            "meta": {"code": code, "name": "T" + code, "sector": "半导体",
                     "industry": "芯片", "as_of": "2026-08-08"},
            "snapshot": None,
            "valuation": {"pe_ttm": pe, "pb": 2.0, "mktcap_yi": 100, "报告期": "20260331", "pe_valid": True},
            "fundamental": {"ROE": roe, "毛利率": 30, "负债率": 40, "净利增速": 20},
            "signals": {"trend": {"评级": "偏多", "得分": 40, "依据": ["多头"]},
                        "ob_os": {"verdict": "中性", "resonance": 0},
                        "reversal": {"拐点标签": "无", "拐点评分": 0}},
            "prediction": None, "sentiment": None,
            "fundflow": {"今日主力净流入": 1e7, "主力连续净流入天数": 1},
            "events": [{"date": "2026-08-05", "type": "增持", "impact": "利好", "title": "股东增持"}],
            "timeseries_refs": {}, "provenance": {}}


def test_reattach_council_picks_up_factor_and_event(monkeypatch):
    """两只票:先 factor.precompute(横截面)→ 再 reattach_council → 多因子/事件驱动不再弃权。"""
    recs = {"000001": _synthetic_record("000001", roe=15.0, pe=20.0),
            "000002": _synthetic_record("000002", roe=5.0, pe=60.0)}
    kv = {}                                            # 内存 code_view 存根

    # —— 存根 store:记录读写 + factor code_view ——
    import tools.analysis.factor.score as score
    import tools.analysis.experts as experts
    import tools.analysis.serialize as ser

    def fake_get_record(code, date="latest"):
        if code in recs:
            return recs[code]
        raise FileNotFoundError(code)

    def fake_put_record(rec, date=None):
        recs[rec["meta"]["code"]] = rec
        return f"mem://{rec['meta']['code']}"

    def fake_put_code_view(name, code, obj, date=None):
        kv[(name, code)] = obj
        return f"mem://{name}/{code}"

    def fake_get_code_view(name, code, date="latest"):
        if (name, code) in kv:
            return kv[(name, code)]
        raise FileNotFoundError(f"{name}/{code}")

    # factor.precompute 读记录 + K线;stub 掉 load_record/market/set_active_date/put_code_view
    monkeypatch.setattr(score, "_availability", score._availability)  # 保持
    import tools.store.repo as store
    monkeypatch.setattr(store, "set_active_date", lambda d: None)
    monkeypatch.setattr(store, "put_code_view", fake_put_code_view)
    monkeypatch.setattr(store, "get_code_view", fake_get_code_view)
    monkeypatch.setattr(store, "get_record", fake_get_record)
    monkeypatch.setattr(store, "put_record", fake_put_record)
    monkeypatch.setattr(ser, "load_record", lambda c, date="latest": fake_get_record(c, date))
    from tools.collectors import market
    monkeypatch.setattr(market, "load_kline", lambda c: (_ for _ in ()).throw(FileNotFoundError(c)))
    # 事件驱动:断精数值,走公告 fallback
    from tools.analysis.event_driven import summary as ed_sum
    monkeypatch.setattr(ed_sum, "_load_precise", lambda code, t, ann_text="": [])

    # 1) 横截面预算
    score.precompute(as_of="2026-08-08", codes=["000001", "000002"])
    assert ("factor", "000001") in kv                  # code_view 已落

    # 2) 回写 council
    n = serialize.reattach_council(["000001", "000002"], "2026-08-08")
    assert n == 2

    # 3) 断言:多因子/事件驱动专家在 council 里不再弃权
    exp = {e["专家"]: e for e in recs["000001"]["council"]["experts"]}
    assert "多因子" in exp and exp["多因子"]["数据充分度"] != "缺失"
    assert "事件驱动" in exp and exp["事件驱动"]["数据充分度"] != "缺失"
    for e in recs["000001"]["council"]["experts"]:
        assert validate_verdict(e) == []
    # 默认组综合结论已产出
    assert recs["000001"]["council"]["default"]["综合方向"] in ("看多", "看空", "中性")
