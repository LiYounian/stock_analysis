"""统一定时任务框架(架构⑥ P1-P2)。

单一声明式任务注册表(SSOT,`tasks.yaml`)+ APScheduler 常驻执行器 + 可插拔主动告警。
本包**只建框架**:默认 dry-run(影子)模式,不真跑生产命令、不碰 live launchd;
与旧 `tools/scheduler.py` 并存,P3 人工 gated 步骤才决定收编(见 docs/参考 runbook)。

关键模块:
    models    —— TaskSpec / NotifyEvent 数据模型 + schema 校验(fail-loud)
    registry  —— 加载 tasks.yaml、热重载(mtime diff)
    notifier  —— Notifier 接口 + LogNotifier/WebhookNotifier(凭证走 env 变量名)
    runtime   —— build_scheduler:锁/超时/重试/misfire/告警包装 + 心跳 + 热reload
    __main__  —— CLI: validate / list / next / run [--dry-run|--live]
"""
from __future__ import annotations

from tools.scheduling.models import NotifyEvent, TaskSpec
from tools.scheduling.registry import Registry, RegistryError, load_registry

__all__ = [
    "TaskSpec",
    "NotifyEvent",
    "Registry",
    "RegistryError",
    "load_registry",
]
