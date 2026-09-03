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


def test_cmd_all_wires_context(monkeypatch, analysis_tmpdir):
    """cmd_all 确实调了 collect_market_context(接线锁)。

    hermetic:cmd_all 的**每一步**都桩掉(含收尾的 run_backtest 与网络步 collect_lhb/
    collect_ticks)——否则 run_backtest 会真跑 backtest_summary.run_and_store,把
    backtest.json 落进 git 跟踪的 data/analysis/<最近达标日>/(只改"生成时间"字段也算脏工作区)。
    analysis_tmpdir 再把 analysis 落盘根切到 tmp_path 兜底:哪怕将来 cmd_all 新增了没桩到的
    落盘步,产物也进临时目录而不是版本库。
    """
    called = {}
    for fn in ("collect_values", "collect_message", "collect_lhb", "collect_ticks",
               "run_sentiment", "run_serialize", "run_events", "run_factor",
               "run_council", "run_panel", "run_screen", "run_backtest"):
        monkeypatch.setattr(run, fn, lambda *a, **k: None)
    monkeypatch.setattr(run, "collect_market_context", lambda: called.__setitem__("ctx", True))
    monkeypatch.setattr(run, "_pool", lambda argv=None: ["000001"])
    run.cmd_all(["all"])
    assert called.get("ctx") is True                   # cmd_all 确实调了指数采集
