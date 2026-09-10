"""闭环产出回写主仓(Phase-1·方案B)——把当日 analysis + scorecard + receipt 原子回写主仓。

背景:盘后闭环从 dailyjob worktree 跑,`settings.PROJECT_ROOT` = 代码所在 = dailyjob,
产出(analysis / market_forecast / scorecard / receipt)全写进 dailyjob 的 data/;而消费者
(选股门控 / eod-review / web)都在**主仓**读 → 读不到 → 选股天天误跳过。

本模块在闭环已调用的 python 步(tools.sync.upload.main 末尾)把当日产出**原子回写主仓绝对路径**,
恢复"主仓=消费侧单一真源"的不变量。随 dailyjob 每轮 `reset --hard origin/main` 自动部署,
不新增 wrapper bash 行。

**原子 + 顺序语义**:逐文件 temp+os.replace(消费者不读到写一半的文件),receipt **最后写**——
它是选股门控的"就绪信号",必须在 analysis 全部到位后才出现,否则门控可能在半成品上放行。

**git 安全**:只 copy 文件,绝不 git add/commit/symlink。当日 daily 目录在主仓本就未跟踪,
拷过去仍未跟踪。scorecard 只拷未跟踪的 `*forward_scorecard*.csv`,不整目录拷 backtest
(那目录有已跟踪 fixture,拷过去会脏主仓跟踪文件、挡 ff-only)。

免责:工程可靠性代码,非投资建议。
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

from tools.config import settings

logger = logging.getLogger("sync.mirror")

# 主仓根默认值(env / symlink 都拿不到时的兜底)。
DEFAULT_MAIN_REPO = Path("/Users/yqg/Documents/projects/stock_analysis")

# scorecard 滚存文件名匹配(均为未跟踪的每日累积 CSV;精确避开 backtest 里的已跟踪 fixture)。
_SCORECARD_GLOB = "*forward_scorecard*.csv"


def resolve_main_repo(explicit: str | Path | None = None) -> Path:
    """定位主仓根(回写目标)。不使用 PROJECT_ROOT——它在 dailyjob 跑时=dailyjob 自身。

    优先级:显式参数 > env STOCK_MAIN_REPO > data/master symlink 目标的祖父目录 > 默认。
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.getenv("STOCK_MAIN_REPO")
    if env:
        return Path(env).expanduser().resolve()
    # dailyjob 生产场景:data/master 是指向主仓 .../stock_analysis/data/master 的 symlink。
    dm = settings.DATA_MASTER
    try:
        if dm.is_symlink():
            # <主仓>/data/master → parents[1] = <主仓>
            return dm.resolve().parents[1]
    except OSError:
        pass
    return DEFAULT_MAIN_REPO


def _atomic_copy_file(src: Path, dst: Path) -> None:
    """原子拷单文件:写临时同目录文件再 os.replace(同卷 rename 原子,消费者不读到半成品)。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.mirror-{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp.write_bytes(src.read_bytes())
        os.replace(tmp, dst)   # 同目录 rename,原子覆盖
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _mirror_tree(src_dir: Path, dst_dir: Path) -> int:
    """把 src_dir 下所有文件(含子目录)逐文件原子拷到 dst_dir。返回拷贝文件数。"""
    if not src_dir.is_dir():
        return 0
    n = 0
    for root, _dirs, files in os.walk(src_dir):
        rel = Path(root).relative_to(src_dir)
        for fn in files:
            _atomic_copy_file(Path(root) / fn, dst_dir / rel / fn)
            n += 1
    return n


def mirror_daily_to_main(date: str, *, source_root: str | Path | None = None,
                         main_root: str | Path | None = None) -> dict:
    """把当日闭环产出原子回写主仓。返回结果 dict(供调用方记账/测试断言)。

    顺序(关键):analysis 整目录 → scorecard 滚存 → **receipt 最后**(就绪信号)。
    源==目标(在主仓自身跑)→ no-op,不自拷自。
    """
    src = Path(source_root).expanduser().resolve() if source_root else settings.PROJECT_ROOT.resolve()
    dst = resolve_main_repo(main_root)

    result: dict = {"date": date, "mirrored": False, "source": str(src), "target": str(dst),
                    "analysis_files": 0, "scorecard_files": 0, "receipt": False, "reason": ""}

    if src == dst:
        result["reason"] = "源==目标(在主仓自身跑),no-op"
        logger.info("回写 no-op:源==目标 %s", src)
        return result

    # 1) analysis/<date>/ 整目录(含 market_forecast.json / 估值位监控.json / per-stock / 策略视图 / 告警 marker)
    src_daily = src / "data" / "analysis" / date
    dst_daily = dst / "data" / "analysis" / date
    result["analysis_files"] = _mirror_tree(src_daily, dst_daily)

    # 2) scorecard 滚存(只拷未跟踪的 *forward_scorecard*.csv,不整目录拷 backtest)
    src_bt = src / "data" / "analysis" / "backtest"
    dst_bt = dst / "data" / "analysis" / "backtest"
    if src_bt.is_dir():
        for f in sorted(src_bt.glob(_SCORECARD_GLOB)):
            if f.is_file():
                _atomic_copy_file(f, dst_bt / f.name)
                result["scorecard_files"] += 1

    # 3) receipt <date>.json —— 最后写(门控就绪信号,必在 analysis 全套就位后才出现)
    src_receipt = src / "data" / "sync_receipts" / f"{date}.json"
    if src_receipt.is_file():
        _atomic_copy_file(src_receipt, dst / "data" / "sync_receipts" / f"{date}.json")
        result["receipt"] = True

    result["mirrored"] = bool(result["analysis_files"] or result["scorecard_files"] or result["receipt"])
    logger.info("回写主仓完成 %s → %s:analysis=%d scorecard=%d receipt=%s",
                src, dst, result["analysis_files"], result["scorecard_files"], result["receipt"])
    return result
