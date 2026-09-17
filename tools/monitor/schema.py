"""盯盘监控数据结构(纯数据·无 IO 副作用,除显式 load/save)。

Watchlist ─▶ WatchItem ─▶ Trigger  三层;Alert 是引擎命中触发后的产物。
schema_version 冻结为 "1.0"(见方案 §1.3/§6);新增触发类型只加 Trigger.kind,不改结构。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from tools.config import settings

SCHEMA_VERSION = "1.0"
WATCH_DIR = settings.PROJECT_ROOT / "data" / "watch"

# 东八区(A股本地时区),created_at/时刻判断统一用它,避免 UTC 误判交易时段。
CST = timezone(timedelta(hours=8))

# 引擎可求值的行情字段(对齐 tools.collectors.gtimg_quote 返回键)。
QUOTE_FIELDS = {"price", "pct_chg", "turnover", "vol_ratio", "amount_wan", "volume", "high", "low"}
# 比较算子。cross_* 需上一轮值判穿越(去抖)。
OPS = {"<=", ">=", "<", ">", "cross_down", "cross_up"}


def _now_cst_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


@dataclass
class Trigger:
    """一条监测触发条件。见方案 §1.3 Trigger 通用结构。"""
    id: str
    kind: str                       # entry_limit/stop_loss/take_profit/pct_move/vol_spike/time/...
    action: str = ""                # 命中动作的人读描述(进通知文案)
    field: str = "price"            # 监测字段(QUOTE_FIELDS 之一);time 类忽略
    op: str = "<="                  # OPS 之一;time 类忽略
    value: Optional[float] = None   # 阈值;time 类忽略
    at: Optional[str] = None        # (time 类)触发时刻 "HH:MM"
    once: bool = True               # 命中一次后本交易日不再重复(去抖核心)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict) -> "Trigger":
        allowed = {f for f in cls.__dataclass_fields__}          # 向后兼容:忽略未知键
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass
class WatchItem:
    code: str
    name: str = ""
    role: str = "自选"              # 买入 | 观察 | 规避 | 自选
    ref_price: Optional[float] = None
    triggers: list[Trigger] = field(default_factory=list)
    gates: list[Trigger] = field(default_factory=list)          # 时间类闸门(如收盘了结)
    meta: dict[str, Any] = field(default_factory=dict)

    def all_conditions(self) -> list[Trigger]:
        return list(self.triggers) + list(self.gates)

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name, "role": self.role,
            "ref_price": self.ref_price,
            "triggers": [t.to_dict() for t in self.triggers],
            "gates": [t.to_dict() for t in self.gates],
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WatchItem":
        return cls(
            code=str(d["code"]), name=d.get("name", ""), role=d.get("role", "自选"),
            ref_price=d.get("ref_price"),
            triggers=[Trigger.from_dict(t) for t in d.get("triggers", [])],
            gates=[Trigger.from_dict(t) for t in d.get("gates", [])],
            meta=d.get("meta", {}) or {},
        )


@dataclass
class Watchlist:
    date: str                                     # YYYY-MM-DD(交易日)
    items: list[WatchItem] = field(default_factory=list)
    source: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
    created_at: str = field(default_factory=_now_cst_iso)

    def codes(self) -> list[str]:
        return [it.code for it in self.items]

    def get(self, code: str) -> Optional[WatchItem]:
        return next((it for it in self.items if it.code == code), None)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "date": self.date,
            "source": self.source,
            "items": [it.to_dict() for it in self.items],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Watchlist":
        return cls(
            date=d["date"],
            items=[WatchItem.from_dict(x) for x in d.get("items", [])],
            source=d.get("source", {}) or {},
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            created_at=d.get("created_at", _now_cst_iso()),
        )

    # ── 持久化 ──────────────────────────────────────────────
    @staticmethod
    def path_for(date: str, *, root: Optional[Path] = None) -> Path:
        return (root or WATCH_DIR) / f"{date}.json"

    def save(self, *, root: Optional[Path] = None) -> Path:
        p = self.path_for(self.date, root=root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    @classmethod
    def load(cls, date: str, *, root: Optional[Path] = None) -> Optional["Watchlist"]:
        p = cls.path_for(date, root=root)
        if not p.exists():
            return None
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))


@dataclass
class Alert:
    """引擎命中一条触发后的产物。落 alerts.jsonl 并交 notifier。"""
    code: str
    name: str
    trigger_id: str
    kind: str
    action: str
    price: Optional[float]
    value: Optional[float]
    fired_at: str = field(default_factory=_now_cst_iso)

    def to_dict(self) -> dict:
        return asdict(self)

    def title(self) -> str:
        return f"盯盘·{self.name or self.code} {self.kind}"

    def body(self) -> str:
        px = f"现价{self.price}" if self.price is not None else "现价—"
        thr = f" (阈值{self.value})" if self.value is not None else ""
        return f"{px}{thr} → {self.action}"
