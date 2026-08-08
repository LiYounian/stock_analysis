"""板块轮动数据接线单测:collect_market_context 采 沪深300+申万一级、健壮降级、且已接进 cmd_all。"""
from tools import run


def test_collect_market_context_calls_index_and_board(monkeypatch):
    from tools.collectors import board, index
    calls = {}
    monkeypatch.setattr(index, "fetch_index",
                        lambda codes: calls.__setitem__("idx", codes) or {"000300": 1})
    monkeypatch.setattr(board, "fetch_board_list",
                        lambda: [{"name": "电子", "code": "801080"}, {"name": "银行", "code": "801780"}])
    monkeypatch.setattr(board, "fetch_board_kline",
                        lambda names: calls.__setitem__("board", names) or {n: 1 for n in names})
    run.collect_market_context()
    assert calls["idx"] == ["沪深300"]                 # 基准按别名采(load_index 也按别名读)
    assert calls["board"] == ["电子", "银行"]           # 全部申万一级


def test_collect_market_context_degrades_no_raise(monkeypatch):
    from tools.collectors import board, index
    def boom(*a, **k):
        raise RuntimeError("被墙/接口异常")
    monkeypatch.setattr(index, "fetch_index", boom)
    monkeypatch.setattr(board, "fetch_board_list", boom)
    run.collect_market_context()                       # 单源失败也不抛(降级)


def test_collect_market_context_empty_board_list_skips(monkeypatch):
    from tools.collectors import board, index
    monkeypatch.setattr(index, "fetch_index", lambda codes: {"000300": 1})
    monkeypatch.setattr(board, "fetch_board_list", lambda: [])   # 清单空
    called = {}
    monkeypatch.setattr(board, "fetch_board_kline",
                        lambda names: called.__setitem__("bk", True) or {})
    run.collect_market_context()
    assert "bk" not in called                          # 清单空 → 不再去拉行业指数


def test_cmd_all_wires_context(monkeypatch):
    called = {}
    for fn in ("collect_values", "collect_message", "run_sentiment", "run_serialize",
               "run_events", "run_factor", "run_council", "run_panel", "run_screen"):
        monkeypatch.setattr(run, fn, lambda *a, **k: None)
    monkeypatch.setattr(run, "collect_market_context", lambda: called.__setitem__("ctx", True))
    monkeypatch.setattr(run, "_pool", lambda argv=None: ["000001"])
    run.cmd_all(["all"])
    assert called.get("ctx") is True                   # cmd_all 确实调了指数采集
