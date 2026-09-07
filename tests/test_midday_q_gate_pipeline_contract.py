"""午盘 Q · M1 · 闸门 pipeline 契约测试(骨架期)。

断言 pipeline 层入口/CLI 契约在,行为断言待实现后放开。
"""
from __future__ import annotations

import pytest

from tools.pipeline import midday_q_gate as P


def test_stage_choices():
    """CLI --stage 只接受三档:1430 / 1450 / final。"""
    assert P._STAGES == ("1430", "1450", "final")


def test_public_api_exists():
    assert callable(P.run)
    assert callable(P.main)


def test_run_signature_accepts_kwargs():
    """签名 run(stage, *, date=None, force=False)——kwargs 只有 date/force。"""
    import inspect
    sig = inspect.signature(P.run)
    params = sig.parameters
    assert "stage" in params
    assert "date" in params and params["date"].default is None
    assert "force" in params and params["force"].default is False


# ────────────────────────────── 骨架期:NotImplementedError ──────────────────────────────

def test_run_is_stub():
    with pytest.raises(NotImplementedError):
        P.run("1430")


def test_copy_breadth_is_stub():
    with pytest.raises(NotImplementedError):
        P._copy_breadth_snapshot("2026-09-07", "1430")


# ────────────────────────────── 契约细节:实现后要保留 ──────────────────────────────

@pytest.mark.skip(reason="M1 骨架期;实现后:非交易日 → exit 0 且不落文件")
def test_non_trading_day_skips():
    pass


@pytest.mark.skip(reason="M1 骨架期;实现后:目标段已存在且非 force → exit 0 跳过")
def test_idempotent_skip():
    pass


@pytest.mark.skip(reason="M1 骨架期;实现后:上游快照/广度副本缺失 → exit 1 且不落 gate 文件")
def test_missing_upstream_exits_1():
    pass


@pytest.mark.skip(reason="M1 骨架期;实现后:copy_breadth 副本已存在 → 不重拷,幂等")
def test_copy_breadth_idempotent():
    pass


@pytest.mark.skip(reason="M1 骨架期;实现后:三段各自补齐 gate JSON 对应字段,不覆盖其他段")
def test_stages_append_not_overwrite():
    pass
