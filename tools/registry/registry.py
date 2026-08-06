"""能力注册表:能力登记为 JSON-in/out 工具,供编排/Agent 发现调用。骨架待填(见 需求.md)。"""
from __future__ import annotations

from typing import Callable

_CAPS: dict[str, dict] = {}


def capability(name: str, description: str, in_schema: dict, out_schema: dict) -> Callable:
    """装饰器:登记一个能力(name/描述/入参schema/出参schema)。"""
    raise NotImplementedError("规划:见 需求.md + 架构 §6.4")


def list_capabilities() -> list[dict]:
    raise NotImplementedError


def call(name: str, args: dict) -> dict:
    """按 schema 校验入参 → 调用 → 校验出参。"""
    raise NotImplementedError
