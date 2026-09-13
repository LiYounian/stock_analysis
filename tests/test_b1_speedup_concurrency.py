"""B1 选股提速(2026-09-13)语义锁:①收盘裁策略11(与午盘同口径)②I/O 并发默认值已上调且可 env 覆盖。

依据 docs/计划/2026-09-13_B1选股提速profile报告.md:②screenall 慢是 I/O 主导(逐票网络采集 +
两轮 LLM 情绪/回灌),非 CPU。本轮改动:调高四处有界并发默认值(均 env 可覆盖) + 收盘也裁非alpha 策略11。

这些断言锁住"为什么改":防未来 prompt/代码重写时无意把并发调回稳妥值、或把策略11 重新纳入收盘、
或误裁别的策略。
"""
from __future__ import annotations

import importlib
import inspect
import os

from tools import run

STRAT11 = "策略11·指标条件化状态排序"


# ————————————————————————————————————————————————
# ① 收盘裁策略11(状态参考·非alpha,午盘已裁,收盘白捡 ~87s)
# ————————————————————————————————————————————————
def test_close_skip_default_cuts_strategy11():
    """收盘默认裁剪集必须只含策略11(不多不少,防误裁别的策略)。"""
    assert run._close_skip_strategies() == {STRAT11}
    assert run.CLOSE_SKIP_STRATEGIES == {STRAT11}


def test_close_skip_killswitch(monkeypatch):
    """env kill-switch:SCREENALL_KEEP_STRATEGY11 为真值 → 回退不裁(None);非真值仍裁。"""
    for truthy in ("1", "true", "TRUE", "yes"):
        monkeypatch.setenv("SCREENALL_KEEP_STRATEGY11", truthy)
        assert run._close_skip_strategies() is None
    for falsy in ("0", "", "no", "off"):
        monkeypatch.setenv("SCREENALL_KEEP_STRATEGY11", falsy)
        assert run._close_skip_strategies() == {STRAT11}


def test_apply_skip_removes_only_strategy11():
    """_apply_skip 用收盘裁剪集只剔策略11,其余策略原样保留(锁不误删)。"""
    screeners = [("策略0·多专家合议", 1), (STRAT11, 2), ("策略4·动量组合", 3), ("S05·最强选股", 4)]
    kept = [l for l, _ in run._apply_skip(screeners, run._close_skip_strategies())]
    assert STRAT11 not in kept
    assert kept == ["策略0·多专家合议", "策略4·动量组合", "S05·最强选股"]


def test_close_skip_label_matches_run_source():
    """裁剪 label 必须与 run_screen_all 里真实 screener label 一字不差(锁不漂移/防裁到不存在的策略)。"""
    src = inspect.getsource(run.run_screen_all)
    for label in run.CLOSE_SKIP_STRATEGIES:
        assert label in src, f"{label} 不在 run_screen_all 源码里,可能已改名"


def test_cmd_screenall_passes_close_skip(monkeypatch):
    """cmd_screenall 必须把 _close_skip_strategies() 下传给 run_screen_all(锁收盘真的裁,而非只定义没用)。"""
    from tools.collectors import universe

    captured = {}

    def _fake_screenall(codes_all, as_of, no_llm=False, no_fetch=False,
                        skip_strategies=None, lean=False):
        captured["skip"] = skip_strategies
        return {}

    monkeypatch.setattr(run, "run_screen_all", _fake_screenall)
    monkeypatch.setattr(universe, "universe_codes", lambda limit=None: ["000001"])
    monkeypatch.setattr(run.store, "set_active_date", lambda *a, **k: None)

    run.cmd_screenall([])
    assert captured["skip"] == {STRAT11}   # 默认收盘裁策略11

    # kill-switch 生效:cmd_screenall 也应据 env 回退不裁
    monkeypatch.setenv("SCREENALL_KEEP_STRATEGY11", "1")
    run.cmd_screenall([])
    assert captured["skip"] is None


# ————————————————————————————————————————————————
# ② I/O 并发默认值已上调 + 可 env 覆盖(直击 I/O 主体)
# ————————————————————————————————————————————————
# (LLM_EXTRACT_WORKERS 4→10 / FETCH_WORKERS 8→16 / 消息面回灌 6→12 / 消息面富集 4→10)
_CONC_ENVS = ["LLM_EXTRACT_WORKERS", "FETCH_WORKERS", "CANDMSG_WORKERS", "ENRICH_WORKERS"]


def _reload_config():
    import tools.config.settings as s
    import tools.config.strategy as st
    importlib.reload(s)
    importlib.reload(st)
    return s, st


def test_concurrency_defaults_raised():
    """无 env 时的真·默认值已按 profile 报告上调(锁不被重写调回稳妥值)。"""
    saved = {k: os.environ.pop(k, None) for k in _CONC_ENVS}
    try:
        s, st = _reload_config()
        assert s.LLM_EXTRACT_WORKERS == 10            # 4→10
        assert s.FETCH_WORKERS == 16                  # 8→16
        assert st.THRESHOLDS["消息面回灌"]["并发数"] == 12   # 6→12
        assert st.THRESHOLDS["消息面富集"]["并发数"] == 10   # 4→10
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
        _reload_config()   # 还原,不污染其余用例


def test_concurrency_env_overridable():
    """四处并发均可经 env 覆盖(别写死);CANDMSG/ENRICH=1 = kill-switch 退串行的入口。"""
    saved = {k: os.environ.get(k) for k in _CONC_ENVS}
    os.environ["LLM_EXTRACT_WORKERS"] = "3"
    os.environ["FETCH_WORKERS"] = "9"
    os.environ["CANDMSG_WORKERS"] = "1"
    os.environ["ENRICH_WORKERS"] = "5"
    try:
        s, st = _reload_config()
        assert s.LLM_EXTRACT_WORKERS == 3
        assert s.FETCH_WORKERS == 9
        assert st.THRESHOLDS["消息面回灌"]["并发数"] == 1    # kill-switch 入口(reader 钳 max(1,·) 退串行)
        assert st.THRESHOLDS["消息面富集"]["并发数"] == 5
    finally:
        for k in _CONC_ENVS:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]
        _reload_config()


def test_concurrency_readers_killswitch_serial():
    """reader 把并发=1 钳成串行(kill-switch 逐值等价旧串行路径);≥1 下限防零线程。"""
    import tools.run as _run
    from tools.pipeline import candidate_message as cm

    # _enrich_workers / _concurrency 都读 config「并发数」并 max(1, int(·)) 钳下限
    assert cm._concurrency({"并发数": 1}) == 1
    assert cm._concurrency({"并发数": 0}) == 1     # 误配 0/负 → 钳到 1,不致零线程
    assert cm._concurrency({"并发数": 12}) == 12
    assert _run._enrich_workers() >= 1
