"""午盘 Q · 大盘闸门流水线节点(launchd 触发)。

设计与 tools.pipeline.intraday_snapshot / market_breadth 同款:确定性节点,
在真实时点跑一次判定并落盘,不依赖 LLM 会话何时醒。

三段式:
    stage="1430"   → 读 14:30 快照+广度 → 落 gate_<date>.json.stage1_1430
    stage="1450"   → 读 14:50 快照+广度 → 落 gate_<date>.json.stage2_1450
    stage="final"  → 读上两段 → merge_two_stage → 落 gate_<date>.json.final + flipped

依赖(上游必须先落盘,由 launchd 时序保证:详见 M1_契约稿.md §六.3):
    · intraday_snapshot --slot 1430 / 1450
    · market_breadth --slot 1430 / 1450 (由 breadth 副本机制拷贝出带 slot 的文件名)

纪律(同 intraday_snapshot 家族):
    · 幂等:同 stage 已落 → 跳过 exit 0(除 --force)
    · 非交易日跳过 exit 0
    · 上游文件未就绪 → warn + exit 1(不落文件,下游按缺文件降级)
    · 独立日志 logs/midday_q_gate.log

用法:
    python -m tools.pipeline.midday_q_gate --stage 1430
    python -m tools.pipeline.midday_q_gate --stage 1450
    python -m tools.pipeline.midday_q_gate --stage final
    python -m tools.pipeline.midday_q_gate --stage 1430 --date 2026-09-08 --force

⚠️ 骨架期:函数体尚未实现(2026-09-07),仅锁 I/O 契约。M1 审阅后填实现。
⚠️ 测试环境研究用,非投资建议;只读行情、不下单。
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal

from tools.analysis.midday_q import gate as G
from tools.collectors import calendar as cal
from tools.config import settings

logger = logging.getLogger("pipeline.midday_q_gate")

SCRIPT_VERSION = "0.1.0"                                  # 骨架版本
LOG_PATH = settings.PROJECT_ROOT / "logs" / "midday_q_gate.log"

Stage = Literal["1430", "1450", "final"]
_STAGES: tuple[Stage, ...] = ("1430", "1450", "final")


# ────────────────────────────── 副本拷贝(§六.1 方案β) ──────────────────────────────

def _copy_breadth_snapshot(date: str, slot: str) -> Path | None:
    """把 data/breadth/<date>.json 拷贝出 <date>_T<slot>.json 副本。

    动机:市场广度采集器一天可能多次跑(1430/1450/1505),原路径 <date>.json 会互相覆盖。
    午盘 Q 需要"该 slot 那一刻"的广度快照 → 由本 pipeline 在读之前立即拷副本。

    - 副本已存在(如上一轮 pipeline 已拷过) → 直接返回其 Path,不重拷
    - 原文件缺失 → 返回 None(上游还没跑完;由 run() 判 exit 1)

    这样 market_breadth.py 无需任何改动(§六.1 方案 β)。
    """
    raise NotImplementedError("M1 骨架:审阅后填实现(shutil.copy2,存在跳过)")


# ────────────────────────────── 读写 ──────────────────────────────

def _load_gate_file(date: str) -> dict:
    """读现有 gate_<date>.json;缺失 → 返回 {"date": date}(供三段累加)。"""
    raise NotImplementedError("M1 骨架")


def _write_atomic(path: Path, payload: dict) -> None:
    """写临时文件 → rename(仿 intraday_snapshot._write_atomic 单一真源)。"""
    raise NotImplementedError("M1 骨架:与 intraday_snapshot._write_atomic 完全同款,可复制过来")


# ────────────────────────────── run() ──────────────────────────────

def run(stage: Stage, *, date: str | None = None, force: bool = False) -> int:
    """跑一段闸门。返回退出码(0=成功/跳过,1=上游缺失/失败)。

    stage:
        "1430" → compute_gate(as_of="14:30") → gate_<date>.stage1_1430
        "1450" → compute_gate(as_of="14:50") → gate_<date>.stage2_1450
        "final" → merge_two_stage(stage1, stage2) → gate_<date>.final + flipped

    幂等:
        - 目标段已存在于 gate JSON 且非 force → 跳过 exit 0

    上游未就绪:
        - stage=1430/1450:快照或广度副本缺失 → warn + exit 1
        - stage=final:stage1_1430 或 stage2_1450 缺失 → warn + exit 1

    非交易日:cal.is_trading_day(date) 假 → 跳过 exit 0
    """
    raise NotImplementedError("M1 骨架")


# ────────────────────────────── CLI ──────────────────────────────

def _setup_logging() -> None:
    """logs/midday_q_gate.log + stderr 双写(命名空间 pipeline.midday_q_gate)。

    与 intraday_snapshot._setup_logging 同款,复制过来即可(单一函数体,不抽公共)。
    """
    raise NotImplementedError("M1 骨架:复制 intraday_snapshot._setup_logging")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="午盘 Q 大盘闸门(确定性节点):按 stage 判定并落盘")
    ap.add_argument("--stage", required=True, choices=_STAGES,
                    help="闸门段:1430=首判 / 1450=复核 / final=合并")
    ap.add_argument("--date", default=None, help="覆盖日期 YYYY-MM-DD(默认今天)")
    ap.add_argument("--force", action="store_true", help="幂等失效,强制重跑覆盖")
    args = ap.parse_args(argv)

    _setup_logging()
    return run(args.stage, date=args.date, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
