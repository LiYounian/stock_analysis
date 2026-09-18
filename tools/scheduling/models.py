"""定时任务框架的数据模型与 schema 校验(fail-loud)。

TaskSpec = 注册表里一条任务的声明式描述;NotifyEvent = 告警渠道收到的事件。
校验策略:任何缺字段 / 类型错 / 坏 cron 一律在加载期抛 SpecError,绝不静默吞——
调度是生产基础设施,宁可启动即失败,也不要带病常驻到点不跑。
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

# 合法的告警触发时机(notify_on 取值域)
NOTIFY_KINDS = ("failure", "misfire", "success")
# NotifyEvent.kind 的全集(含框架自身事件)
EVENT_KINDS = ("failure", "misfire", "success", "heartbeat", "startup", "timeout")


class SpecError(ValueError):
    """任务声明非法(缺字段/类型错/坏 cron/依赖悬空)。加载期 fail-loud。"""


@dataclass(frozen=True)
class TaskSpec:
    """注册表中的单条任务声明(声明式 SSOT 的一行)。

    字段:
        id                任务唯一标识(对应 launchd Label 尾段,如 intraday_screen)
        cmd               命令 argv 列表(如 ["python","-m","tools.run","screenall"])
        cron              标准 5 段 crontab 表达式:分 时 日 月 周(周 0=周日,1-5=周一到五)。
                          也可为 **crontab 字符串列表**表达"一天多个不规则时刻"(如 commitdocs
                          每工作日 13:30 + 22:40——两时刻分钟不同,单条 cron 无法表达);
                          列表时每条各注册一个 trigger,合起来即该任务的全部触发时刻。
        timeout_sec       单次执行超时秒;None=不限
        retries           失败后额外重试次数(0=不重试)
        retry_backoff_sec 重试指数退避基数秒(第 i 次退避 = base * 2**(i-1))
        depends_on        依赖的其它任务 id(P1 仅登记不强制编排;供后续 DAG 用)
        notify_on         触发告警的时机子集(NOTIFY_KINDS)
        enabled           是否注册进调度(false=声明保留但不排期)
        tags              自由标签(local/remote/intraday/eod… 供过滤与远端适配器分发)
        description       人读说明
    """

    id: str
    cmd: list[str]
    cron: str | list[str]
    timeout_sec: int | None = None
    retries: int = 0
    retry_backoff_sec: float = 5.0
    depends_on: list[str] = field(default_factory=list)
    notify_on: list[str] = field(default_factory=lambda: ["failure", "misfire"])
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    description: str = ""

    def __post_init__(self) -> None:
        if not self.id or not isinstance(self.id, str):
            raise SpecError(f"任务 id 非法(需非空字符串):{self.id!r}")
        if not isinstance(self.cmd, list) or not self.cmd or not all(
            isinstance(x, str) and x for x in self.cmd
        ):
            raise SpecError(f"[{self.id}] cmd 需为非空字符串列表:{self.cmd!r}")
        # cron 允许单 str 或 str 列表(多时刻);逐条按标准 5 段校验(fail-loud)
        if isinstance(self.cron, list):
            if not self.cron or not all(isinstance(c, str) for c in self.cron):
                raise SpecError(f"[{self.id}] cron 列表需为非空字符串列表:{self.cron!r}")
        elif not isinstance(self.cron, str):
            raise SpecError(f"[{self.id}] cron 需为 5 段 crontab 字符串或其列表:{self.cron!r}")
        for c in self.crons:
            if len(c.split()) != 5:
                raise SpecError(
                    f"[{self.id}] cron 需为标准 5 段 crontab 字符串:{c!r}"
                    + (f"(在 {self.cron!r} 中)" if isinstance(self.cron, list) else ""))
        if self.timeout_sec is not None and (
            not isinstance(self.timeout_sec, int) or self.timeout_sec <= 0
        ):
            raise SpecError(f"[{self.id}] timeout_sec 需为正整数或 None:{self.timeout_sec!r}")
        if not isinstance(self.retries, int) or self.retries < 0:
            raise SpecError(f"[{self.id}] retries 需为非负整数:{self.retries!r}")
        if self.retry_backoff_sec < 0:
            raise SpecError(f"[{self.id}] retry_backoff_sec 需 >=0:{self.retry_backoff_sec!r}")
        bad = [k for k in self.notify_on if k not in NOTIFY_KINDS]
        if bad:
            raise SpecError(f"[{self.id}] notify_on 含非法值 {bad},仅允许 {NOTIFY_KINDS}")
        # cron 可解析性在此校验(fail-loud):坏表达式 build_trigger 会抛
        self.build_trigger()

    @property
    def crons(self) -> list[str]:
        """cron 归一为列表:单 str→[str],list→原样。多条=该任务的多个触发时刻。"""
        return [self.cron] if isinstance(self.cron, str) else list(self.cron)

    @property
    def cron_display(self) -> str:
        """人读单行字符串(list 用 ' | ' 连接);供 CLI 表格/日志,避免对 list 套用 :Ns 格式。"""
        return self.cron if isinstance(self.cron, str) else " | ".join(self.cron)

    def build_trigger(self):
        """把 cron 编译成 APScheduler 触发器(坏表达式即抛,供校验复用)。

        单 cron → CronTrigger(与历史行为完全一致,单 cron 任务零变化);
        多 cron → OrTrigger(取各子 cron 的最早下次触发,合起来即全部触发时刻)。
        """
        from apscheduler.triggers.cron import CronTrigger

        trigs = []
        for c in self.crons:
            try:
                trigs.append(CronTrigger.from_crontab(c))
            except Exception as e:  # noqa: BLE001
                raise SpecError(f"[{self.id}] cron 表达式无法解析:{c!r} ({e})") from e
        if len(trigs) == 1:
            return trigs[0]
        from apscheduler.triggers.combining import OrTrigger
        return OrTrigger(trigs)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskSpec":
        """从注册表字典构造;未知字段直接报错(防拼写漏配)。"""
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise SpecError(f"任务 {d.get('id')!r} 含未知字段 {sorted(unknown)};合法字段 {sorted(known)}")
        return cls(**d)


@dataclass
class NotifyEvent:
    """一次需要外发的调度事件(失败/漏跑/成功/心跳/启动)。"""

    task_id: str
    kind: str                       # EVENT_KINDS 之一
    message: str
    ts: _dt.datetime = field(default_factory=lambda: _dt.datetime.now())
    returncode: int | None = None
    duration_sec: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise SpecError(f"NotifyEvent.kind 非法:{self.kind!r},仅允许 {EVENT_KINDS}")

    def title(self) -> str:
        icon = {"failure": "🔴", "timeout": "⏱️", "misfire": "🟡",
                "success": "🟢", "heartbeat": "💓", "startup": "🚀"}.get(self.kind, "ℹ️")
        return f"{icon} [stock-scheduler] {self.task_id} {self.kind}"

    def as_text(self) -> str:
        parts = [self.title(), self.message]
        if self.returncode is not None:
            parts.append(f"returncode={self.returncode}")
        if self.duration_sec is not None:
            parts.append(f"duration={self.duration_sec:.1f}s")
        parts.append(self.ts.strftime("%Y-%m-%d %H:%M:%S"))
        return " | ".join(str(p) for p in parts)
