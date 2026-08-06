"""编排层:显式 DAG 串各任务。骨架待填(见 需求.md)。

失败隔离:某 stage 失败,下游读上次成功产物。run.py 退化为薄 CLI。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Stage:
    name: str
    fn: Callable
    deps: list[str] = field(default_factory=list)


def run_pipeline(name: str) -> None:
    """拓扑排序执行管线,失败隔离。"""
    raise NotImplementedError("规划:见 需求.md + 架构 §8.1")
