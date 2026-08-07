"""流式 runner 单测(ops.progress_runner):返回 rc+全量输出,且逐行实时回显、合并 stderr。"""
import sys

from ops.progress_runner import streaming_runner


def test_returns_rc_and_full_output_and_echoes_each_line():
    seen = []
    rc, out = streaming_runner([sys.executable, "-c", "print('a'); print('b'); print('c')"],
                               echo=seen.append)
    assert rc == 0
    assert out.splitlines() == ["a", "b", "c"]     # 全量输出
    assert seen == ["a", "b", "c"]                  # 逐行回显(顺序一致)


def test_merges_stderr_into_stream():
    rc, out = streaming_runner(
        [sys.executable, "-c", "import sys; print('to-out'); print('to-err', file=sys.stderr)"],
        echo=lambda s: None)
    assert rc == 0
    assert "to-out" in out and "to-err" in out      # 日志走 stderr,也要被捕获/回显


def test_nonzero_rc_propagates():
    rc, _ = streaming_runner([sys.executable, "-c", "import sys; sys.exit(3)"], echo=lambda s: None)
    assert rc == 3


def test_forces_unbuffered_env():
    # 子进程能读到 PYTHONUNBUFFERED(runner 注入),证明防缓冲已生效
    rc, out = streaming_runner(
        [sys.executable, "-c", "import os; print(os.environ.get('PYTHONUNBUFFERED'))"],
        echo=lambda s: None)
    assert rc == 0 and out.strip() == "1"
