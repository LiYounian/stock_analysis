"""流式子进程执行器:逐行实时回显 + 返回 (rc, 全量输出)。

与 ops.remote_update.subprocess_runner **同签名**(runner(cmd)->(rc,out)),但不再把输出
攒到最后——用 Popen 逐行读、即时 print/flush,让前台跑流水线时能实时看到日志滚动。
关键点:
  - 子进程强制无缓冲(PYTHONUNBUFFERED=1),否则 Python 日志会被块缓冲、迟迟不出来;
  - stderr 合并进 stdout(logging 默认写 stderr,合并后才能一起实时滚动);
  - echo 可注入(默认 print+flush),便于单测捕获逐行回显。
用途:local_autopush 前台跑 tools.run 时用它;remote_update 的自动更新仍用捕获版 subprocess_runner。
"""
from __future__ import annotations

import os
import subprocess


def _emit(line: str) -> None:
    print(line, flush=True)


def streaming_runner(cmd: list[str], *, echo=_emit, env: dict | None = None) -> tuple[int, str]:
    """跑 cmd,逐行实时回显(stdout+stderr 合并),返回 (返回码, 全量输出字符串)。"""
    e = dict(os.environ if env is None else env)
    e.setdefault("PYTHONUNBUFFERED", "1")          # 子进程行缓冲,日志即时出
    lines: list[str] = []
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1, env=e)
    assert p.stdout is not None
    for line in iter(p.stdout.readline, ""):
        line = line.rstrip("\n")
        lines.append(line)
        echo(line)
    p.stdout.close()
    rc = p.wait()
    return rc, "\n".join(lines)
