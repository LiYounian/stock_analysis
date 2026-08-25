"""策略11 screener 薄管线 单测:无 kline / 无池 优雅降级(不崩、view 空)。

端到端(建池 + 全A 真跑 + 概率合理性)在数据线机器验证——本 worktree 无全A主档/池。
"""
import pandas as pd

from tools.pipeline import screen_conditional_rank as scr


def _raise_no_pool():
    raise FileNotFoundError("state_pool 缺失")


def test_screener_degrades_gracefully(monkeypatch):
    """无 kline + 无 state_pool → 不崩,view 排行为空,预筛跳过计数正确,note 提示池缺失。"""
    monkeypatch.setattr(scr.cpred, "get_pool_index", _raise_no_pool)
    monkeypatch.setattr(scr.market, "load_kline_recent", lambda code: None)
    captured = {}
    monkeypatch.setattr(scr.store, "put_view",
                        lambda name, view: (captured.__setitem__(name, view), "path")[1])

    v = scr.run_conditional_rank_screen(["000001", "000002"], as_of=None, fetch=False)

    assert v["扫描数"] == 2
    assert v["预筛跳过"]["无K线或历史不足"] == 2
    assert all(v["排行"][h] == [] for h in ("1日", "5日", "10日"))
    assert "note" in v and "state_pool" in v["note"]
    assert "指标条件化状态排序" in captured


def test_screener_skips_short_history(monkeypatch):
    """历史不足 MIN_BARS → 跳过(不进排行)。"""
    monkeypatch.setattr(scr.cpred, "get_pool_index", _raise_no_pool)
    short = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=10),
                          "close": range(10)})
    monkeypatch.setattr(scr.market, "load_kline_recent", lambda code: short)
    monkeypatch.setattr(scr.store, "put_view", lambda name, view: "path")

    v = scr.run_conditional_rank_screen(["000001"], as_of=None, fetch=False)
    assert v["预筛跳过"]["无K线或历史不足"] == 1
