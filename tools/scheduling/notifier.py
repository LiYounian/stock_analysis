"""可插拔主动告警(填补全仓零告警的最大空白)。

设计要点:
- 接口 Notifier.notify(event):渠道无关;失败/漏跑/心跳都走它。
- 默认 LogNotifier:始终可用、无外部依赖,把事件写日志。
- WebhookNotifier:飞书/钉钉/通用 webhook;**凭证走 env 变量名**(配置里写 url_env 而非 url),
  运行时解析;env 缺失 → fail-soft(记日志不崩、绝不打印明文 url),绝不入库。
- MultiNotifier:并联多个渠道,单渠道抛错不连累其它(告警本身不能拖垮调度)。
- build_notifier():按 env 组装(LogNotifier 始终在;配了 webhook 就并联)。

⚠️ 网络发送封装在 _post();单测通过子类/monkeypatch 覆盖 _post,绝不真发。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from abc import ABC, abstractmethod

from tools.scheduling.models import NotifyEvent

logger = logging.getLogger("scheduling.notifier")

# env 变量名(存的是"去哪读凭证",不是凭证本身)
ENV_CHANNEL = "STOCK_ALERT_CHANNEL"        # log | feishu | dingtalk | generic | none(可逗号并联)
ENV_WEBHOOK_ENV = "STOCK_ALERT_WEBHOOK_ENV"  # 指向真正存 webhook url 的 env 变量名
DEFAULT_WEBHOOK_ENV = "STOCK_ALERT_WEBHOOK"  # 该变量名缺省值


class Notifier(ABC):
    """告警渠道接口。实现只需管好 notify(event) 幂等、异常自吞。"""

    @abstractmethod
    def notify(self, event: NotifyEvent) -> None: ...


class LogNotifier(Notifier):
    """默认渠道:把事件写日志(failure/timeout → error,misfire → warning,余 info)。"""

    def notify(self, event: NotifyEvent) -> None:
        lvl = {"failure": logging.ERROR, "timeout": logging.ERROR,
               "misfire": logging.WARNING}.get(event.kind, logging.INFO)
        logger.log(lvl, "ALERT %s", event.as_text())


class WebhookNotifier(Notifier):
    """飞书/钉钉/通用 webhook。url 从 env 变量名解析,值不入库、不落日志。"""

    def __init__(self, kind: str = "generic", url_env: str = DEFAULT_WEBHOOK_ENV,
                 timeout: float = 5.0) -> None:
        if kind not in ("feishu", "dingtalk", "generic"):
            raise ValueError(f"未知 webhook 类型:{kind}")
        self.kind = kind
        self.url_env = url_env
        self.timeout = timeout

    def _payload(self, event: NotifyEvent) -> dict:
        text = event.as_text()
        if self.kind == "feishu":
            return {"msg_type": "text", "content": {"text": text}}
        if self.kind == "dingtalk":
            return {"msgtype": "text", "text": {"content": text}}
        return {"text": text, "task_id": event.task_id, "kind": event.kind}

    def _post(self, url: str, payload: dict) -> None:
        """实际 HTTP POST(单测覆盖此方法即可完全离线)。"""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            resp.read()

    def notify(self, event: NotifyEvent) -> None:
        url = os.getenv(self.url_env, "").strip()
        if not url:
            # fail-soft:凭证没配就退化为不发,只提示去哪配(不打印 url 本身)
            logger.warning("webhook 未配置(env %s 为空),跳过外发:%s",
                           self.url_env, event.title())
            return
        try:
            self._post(url, self._payload(event))
            logger.info("webhook 已发送(%s):%s", self.kind, event.title())
        except Exception as e:  # noqa: BLE001 —— 告警失败绝不能拖垮调度
            logger.error("webhook 发送失败(%s,%s):%s", self.kind, type(e).__name__, e)


class MultiNotifier(Notifier):
    """并联多个渠道;任一渠道抛错不影响其它。"""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def notify(self, event: NotifyEvent) -> None:
        for n in self.notifiers:
            try:
                n.notify(event)
            except Exception as e:  # noqa: BLE001
                logger.error("渠道 %s 告警异常:%s", type(n).__name__, e)


def build_notifier(channel: str | None = None) -> Notifier:
    """按 env 组装告警器。LogNotifier 始终在场;声明了 webhook 类型则并联对应 WebhookNotifier。

    channel 取 env STOCK_ALERT_CHANNEL(可逗号并联,如 "log,feishu");缺省仅 log。
    webhook url 的 env 变量名取 STOCK_ALERT_WEBHOOK_ENV(缺省 STOCK_ALERT_WEBHOOK)。
    """
    raw = (channel if channel is not None else os.getenv(ENV_CHANNEL, "log"))
    wanted = [c.strip().lower() for c in raw.split(",") if c.strip()]
    url_env = os.getenv(ENV_WEBHOOK_ENV, DEFAULT_WEBHOOK_ENV)
    chans: list[Notifier] = [LogNotifier()]  # 日志兜底始终在
    for c in wanted:
        if c in ("feishu", "dingtalk", "generic"):
            chans.append(WebhookNotifier(kind=c, url_env=url_env))
        elif c in ("log", "none", ""):
            continue
        else:
            logger.warning("未知告警渠道 %r,忽略", c)
    return chans[0] if len(chans) == 1 else MultiNotifier(chans)
