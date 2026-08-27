"""ops.remote_fetch.main 分支判定回归。

锁死语义:成功路径返回的 skipped 是"跳过的停牌股数"(整数),**不得**被误判成"整体跳过采集"。
历史 bug:main 曾用 `if res.get("skipped"):` 判整体跳过,成功路径 skipped=3(真值)误入分支 →
取 res['reason'] KeyError → 退非零 → 上游 pull_refresh.sh 每天误报"本地全A采集失败"(掩盖真失败)。
改判据为 `is True` 哨兵(仅非交易日/整体跳过路径 skipped=True)。
"""
from ops import remote_fetch


def test_main_spot_success_with_skipped_count(monkeypatch, capsys):
    """成功路径 skipped=<int>(停牌数):走"采集完成",返回0,绝不取 reason(不 KeyError)。"""
    monkeypatch.setattr(remote_fetch, "run_fetch",
                        lambda *a, **k: {"ok": 5547, "date": "2026-08-27",
                                         "mode": "tushare_spot", "source": "tushare_daily", "skipped": 3})
    rc = remote_fetch.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "采集完成 2026-08-27" in out
    assert "跳过采集" not in out


def test_main_not_trading_day_skip(monkeypatch, capsys):
    """整体跳过路径 skipped=True + reason:走"跳过采集",返回0。"""
    monkeypatch.setattr(remote_fetch, "run_fetch",
                        lambda *a, **k: {"skipped": True, "reason": "not_trading_day", "date": "2026-08-30"})
    rc = remote_fetch.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "跳过采集(not_trading_day)" in out


def test_main_spot_success_zero_skipped(monkeypatch, capsys):
    """成功且 skipped=0(边界):0 本是假值不会误入,锁住不回退。"""
    monkeypatch.setattr(remote_fetch, "run_fetch",
                        lambda *a, **k: {"ok": 5550, "date": "2026-08-27", "mode": "tushare_spot", "skipped": 0})
    rc = remote_fetch.main([])
    assert rc == 0
    assert "采集完成" in capsys.readouterr().out
