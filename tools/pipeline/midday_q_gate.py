"""午盘 Q · 大盘闸门流水线节点(launchd 触发)。

设计与 tools.pipeline.intraday_snapshot / market_breadth 同款:确定性节点,
在真实时点跑一次判定并落盘,不依赖 LLM 会话何时醒。

三段式:
    stage="1430"   → 拷 breadth 副本 → compute_gate("14:30") → 落 gate.stage1_1430
    stage="1450"   → 拷 breadth 副本 → compute_gate("14:50") → 落 gate.stage2_1450
    stage="final"  → 读上两段 → merge_two_stage → 补 gate.final + flipped

依赖(上游必须先落盘,由 launchd 时序保证:详见 M1_契约稿.md §六.3):
    · market_breadth --slot 1430 / 1450 → data/breadth/<date>.json(原路径,可能被后跑覆盖)
      本 pipeline 会立即把它拷成 <date>_T<slot>.json 作副本(§六.1 方案β,零改 breadth)

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

⚠️ 测试环境研究用,非投资建议;只读行情、不下单。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from tools.analysis.midday_q import gate as G
from tools.collectors import calendar as cal
from tools.config import settings

logger = logging.getLogger("pipeline.midday_q_gate")

SCRIPT_VERSION = "0.1.0"
LOG_PATH = settings.PROJECT_ROOT / "logs" / "midday_q_gate.log"

Stage = Literal["1430", "1450", "final"]
_STAGES: tuple[Stage, ...] = ("1430", "1450", "final")

# stage → 对应的 as_of(final 阶段不用)
_STAGE_AS_OF: dict[str, str] = {"1430": "14:30", "1450": "14:50"}

# stage → gate JSON 里的段名
_STAGE_KEY: dict[str, str] = {
    "1430": "stage1_1430",
    "1450": "stage2_1450",
    "final": "final",
}


# ────────────────────────────── 副本拷贝(§六.1 方案β) ──────────────────────────────

def _copy_breadth_snapshot(date: str, slot: str) -> Path | None:
    """把 data/breadth/<date>.json 拷贝出 <date>_T<slot>.json 副本。幂等:副本已在则不重拷。

    动机:market_breadth 一天可能多次跑(1430/1450/1505),原路径 <date>.json 会被后跑覆盖。
    午盘 Q 需要"该 slot 那一刻"的广度快照 → 本 pipeline 在读之前立即拷副本。

    返回:
        - 副本 Path(已存在或本次新拷)
        - None:原文件缺失(上游还没跑;由 run() 判 exit 1)
    """
    src = settings.PROJECT_ROOT / "data" / "breadth" / f"{date}.json"
    dst = G.breadth_path_for(date, slot)
    if dst.exists():
        return dst
    if not src.exists():
        logger.warning("breadth 原文件缺失:%s(需要先跑 market_breadth --slot %s)", src, slot)
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    logger.info("拷 breadth 副本:%s → %s", src.name, dst.name)
    return dst


# ────────────────────────────── 读写 ──────────────────────────────

def _load_gate_file(date: str) -> dict:
    """读现有 gate_<date>.json;缺失 → 返回 {"date": date}(供三段累加)。"""
    p = G.gate_path(date)
    if not p.exists():
        return {"date": date}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if "date" not in data:
            data["date"] = date
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("gate 文件损坏 %s: %s → 视为空重来", p, e)
        return {"date": date}


def _write_atomic(path: Path, payload: dict) -> None:
    """写临时文件 → rename(仿 intraday_snapshot._write_atomic 单一真源同款)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".gate-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ────────────────────────────── run() ──────────────────────────────

def run(stage: Stage, *, date: str | None = None, force: bool = False) -> int:
    """跑一段闸门。契约见模块 docstring / M1_契约稿.md §五。"""
    if stage not in _STAGES:
        logger.error("非法 stage %r,只支持 %s", stage, _STAGES)
        return 1

    date = date or datetime.now().strftime("%Y-%m-%d")

    if not cal.is_trading_day(date):
        logger.info("跳过:%s 非 A 股交易日(stage=%s)", date, stage)
        return 0

    key = _STAGE_KEY[stage]
    gate = _load_gate_file(date)
    if key in gate and not force:
        logger.info("跳过:gate.%s 已存在,不覆盖(要重跑加 --force)", key)
        return 0

    if stage in ("1430", "1450"):
        # 拷副本 → 判定
        copied = _copy_breadth_snapshot(date, stage)
        if copied is None:
            logger.error("上游 breadth 未就绪:%s(需要先跑 market_breadth --slot %s)",
                         G.breadth_path_for(date, stage), stage)
            return 1
        as_of = _STAGE_AS_OF[stage]
        result = G.compute_gate(date, as_of)
        gate[key] = dict(result)
        gate["meta"] = {
            **(gate.get("meta") or {}),
            "script": "tools.pipeline.midday_q_gate",
            "script_version": SCRIPT_VERSION,
            "breadth_copy": copied.name,
        }
        _write_atomic(G.gate_path(date), gate)
        logger.info("落 gate.%s:state=%s allowed=%s pos=%.2f",
                    key, result["state"], result["allowed_strategies"], result["position_pct"])
        return 0

    # stage == "final":合并两段
    s1 = gate.get("stage1_1430")
    s2 = gate.get("stage2_1450")
    if not s1 or not s2:
        logger.error("final 阶段缺前置段:stage1_1430=%s stage2_1450=%s",
                     bool(s1), bool(s2))
        return 1
    merged = G.merge_two_stage(s1, s2)   # merged 已含 stage1/2 + final + flipped
    gate.update(merged)                   # 覆盖 date/stage1/stage2/final/flipped
    gate["meta"] = {
        **(gate.get("meta") or {}),
        "script": "tools.pipeline.midday_q_gate",
        "script_version": SCRIPT_VERSION,
    }
    _write_atomic(G.gate_path(date), gate)
    logger.info("落 gate.final:allowed=%s pos=%.2f flipped=%s",
                merged["final"]["allowed_strategies"],
                merged["final"]["position_pct"],
                merged["flipped"])
    return 0


# ────────────────────────────── CLI ──────────────────────────────

def _setup_logging() -> None:
    """logs/midday_q_gate.log + stderr 双写(命名空间 pipeline.midday_q_gate)。

    与 intraday_snapshot._setup_logging 同款——同款函数体复用,不抽公共基座(单函数,
    抽出反而增加跨模块耦合;宪法 §4 简单优先)。
    """
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


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
