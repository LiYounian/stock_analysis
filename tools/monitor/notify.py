"""通知后端(可插拔·③接口)。

本轮实现 DesktopNotifier(macOS osascript 桌面小卡片,零依赖)。
其他后端(bot/webhook/邮件)只留占位接口,本轮不实现。
非 macOS 或 osascript 缺失 → 降级为日志/stdout,绝不炸引擎。
"""
from __future__ import annotations

import logging
import platform
import shutil
import subprocess

from tools.monitor.schema import Alert

logger = logging.getLogger("monitor.notify")


class Notifier:
    """通知抽象。引擎命中触发后调 notify(alert)。"""
    name = "base"

    def notify(self, alert: Alert) -> None:      # pragma: no cover - 抽象
        raise NotImplementedError


class LogNotifier(Notifier):
    """兜底后端:只打日志/stdout。任何平台可用,做降级目标。"""
    name = "log"

    def notify(self, alert: Alert) -> None:
        logger.info("[ALERT] %s | %s", alert.title(), alert.body())
        print(f"[盯盘提示] {alert.title()} | {alert.body()}", flush=True)


class DesktopNotifier(Notifier):
    """macOS 桌面小卡片(osascript display notification)。

    非 darwin / osascript 不可用 → 自动降级到 LogNotifier(不炸)。
    文案做转义,防标的名/动作里的引号破坏 AppleScript。
    """
    name = "desktop"

    def __init__(self) -> None:
        self._fallback = LogNotifier()
        self._enabled = platform.system() == "Darwin" and shutil.which("osascript") is not None
        if not self._enabled:
            logger.warning("DesktopNotifier 不可用(非macOS或无osascript),降级为日志输出")

    @staticmethod
    def _esc(s: str) -> str:
        return (s or "").replace("\\", "\\\\").replace('"', '\\"')

    def notify(self, alert: Alert) -> None:
        # 始终也打一条日志,便于复盘/无人值守留痕。
        self._fallback.notify(alert)
        if not self._enabled:
            return
        title, body = self._esc(alert.title()), self._esc(alert.body())
        script = f'display notification "{body}" with title "{title}" sound name "Glass"'
        try:
            subprocess.run(["osascript", "-e", script], check=False, timeout=5)
        except Exception as e:                    # 通知失败绝不影响盯盘主循环
            logger.warning("osascript 弹窗失败(已降级日志):%s", e)


class MultiNotifier(Notifier):
    """广播到多个后端(留给未来:桌面+bot 同时发)。"""
    name = "multi"

    def __init__(self, backends: list[Notifier]) -> None:
        self._backends = backends

    def notify(self, alert: Alert) -> None:
        for b in self._backends:
            try:
                b.notify(alert)
            except Exception as e:
                logger.warning("后端 %s 通知失败:%s", getattr(b, "name", "?"), e)


# ── 注册表/工厂 ────────────────────────────────────────────
# 留占位:bot/webhook/email 后续实现后在此注册,config MONITOR_NOTIFIER 即可切换。
_REGISTRY: dict[str, type[Notifier]] = {
    "desktop": DesktopNotifier,
    "log": LogNotifier,
}


def get_notifier(name: str = "desktop") -> Notifier:
    cls = _REGISTRY.get(name)
    if cls is None:
        logger.warning("未知通知后端 %r,回落 desktop", name)
        cls = DesktopNotifier
    return cls()
