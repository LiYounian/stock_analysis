"""任务注册表加载与热重载(SSOT = tasks.yaml)。

加载 = 读 YAML → 逐条 TaskSpec.from_dict(fail-loud)→ 校验全局约束(id 唯一、依赖不悬空)。
热重载 = 记录文件 mtime,`changed()` 判是否需重读;runtime 据此 add_job(replace_existing=True),
改 cron 无需重启进程即生效。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tools.scheduling.models import SpecError, TaskSpec

logger = logging.getLogger("scheduling.registry")

# 默认注册表路径:与本模块同目录的 tasks.yaml(随 git、可改配置热生效)
DEFAULT_REGISTRY_PATH = Path(__file__).with_name("tasks.yaml")


class RegistryError(SpecError):
    """注册表级错误(文件缺失/顶层结构错/id 重复/依赖悬空)。"""


@dataclass
class Registry:
    """一份已解析、已校验的任务注册表(带来源路径与 mtime,支持热重载)。"""

    path: Path
    tasks: list[TaskSpec] = field(default_factory=list)
    _mtime: float = 0.0

    def by_id(self) -> dict[str, TaskSpec]:
        return {t.id: t for t in self.tasks}

    def enabled(self) -> list[TaskSpec]:
        return [t for t in self.tasks if t.enabled]

    def changed(self) -> bool:
        """磁盘文件是否比上次加载更新(mtime 变化)。文件消失视为未变(交由 reload 报错)。"""
        try:
            return os.path.getmtime(self.path) != self._mtime
        except OSError:
            return False

    def reload(self) -> "Registry":
        """重新从磁盘解析(就地更新 tasks/_mtime)。解析失败**保留旧表**并抛,避免热更把好表冲坏。"""
        fresh = load_registry(self.path)
        self.tasks = fresh.tasks
        self._mtime = fresh._mtime
        return self


def _validate_global(tasks: list[TaskSpec], path: Path) -> None:
    """全局约束:id 唯一、depends_on 指向存在的任务。"""
    ids = [t.id for t in tasks]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise RegistryError(f"{path}: 任务 id 重复:{dup}")
    idset = set(ids)
    for t in tasks:
        missing = [d for d in t.depends_on if d not in idset]
        if missing:
            raise RegistryError(f"{path}: [{t.id}] depends_on 悬空(无此任务):{missing}")


def load_registry(path: str | os.PathLike | None = None) -> Registry:
    """加载并校验注册表。任何结构/schema/依赖错误一律抛 RegistryError(fail-loud)。"""
    p = Path(path) if path else DEFAULT_REGISTRY_PATH
    if not p.exists():
        raise RegistryError(f"注册表文件不存在:{p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise RegistryError(f"{p}: YAML 解析失败:{e}") from e
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or "tasks" not in raw:
        raise RegistryError(f"{p}: 顶层需为含 'tasks' 键的映射,实得 {type(raw).__name__}")
    items = raw["tasks"]
    if not isinstance(items, list):
        raise RegistryError(f"{p}: 'tasks' 需为列表,实得 {type(items).__name__}")

    tasks: list[TaskSpec] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise RegistryError(f"{p}: tasks[{i}] 需为映射,实得 {type(item).__name__}")
        try:
            tasks.append(TaskSpec.from_dict(item))
        except SpecError as e:
            raise RegistryError(f"{p}: tasks[{i}] 非法:{e}") from e

    _validate_global(tasks, p)
    reg = Registry(path=p, tasks=tasks, _mtime=os.path.getmtime(p))
    logger.info("注册表加载成功:%s(%d 条,%d 启用)", p, len(tasks), len(reg.enabled()))
    return reg
