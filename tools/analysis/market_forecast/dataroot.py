"""数据根解析(worktree 兼容)——大盘预测统一从这里拿主档 / analysis 数据根。

隔离 worktree 的 data/ 是空的(gitignore),历史数据只在主仓 data/。仿
`tools.backtest.validate_adaptive_rr` 的做法:解析真实数据根(含 master/kline),
monkeypatch `store._MASTER_DIR`/`_RAW_DIR`,并暴露 analysis 目录给消息面模块直读。

优先级:显式 root > 环境变量 STOCK_DATA_ROOT > store 默认(主档非空)> 自动探测主仓(git-common-dir)。
"""
from __future__ import annotations

import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


def resolve_data_root(cli_root: str | None = None) -> Path:
    """返回含 master/kline 的真实数据根 Path。找不到则抛 FileNotFoundError。"""
    from tools.config import settings

    for cand in (cli_root, os.getenv("STOCK_DATA_ROOT")):
        if cand:
            p = Path(cand).expanduser().resolve()
            if (p / "master" / "kline").exists():
                return p
            print(f"[warn] 指定 data-root 无 master/kline:{p}", file=sys.stderr)

    default_master = settings.DATA_MASTER / "kline"
    if default_master.exists() and any(default_master.glob("*.parquet")):
        return settings.DATA_MASTER.parent  # data/

    try:  # worktree:git 公共目录父级 = 主仓根
        common = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(settings.PROJECT_ROOT), text=True,
        ).strip()
        cand = Path(common).resolve().parent / "data"
        if (cand / "master" / "kline").exists():
            print(f"[info] worktree 无本地主档,自动使用主仓数据根:{cand}", file=sys.stderr)
            return cand
    except Exception as e:  # pragma: no cover
        print(f"[warn] 自动探测主仓失败:{e!r}", file=sys.stderr)

    raise FileNotFoundError(
        "无法解析含 master/kline 的数据根;请传 --data-root 或设 STOCK_DATA_ROOT。")


def apply_to_store(root: Path) -> None:
    """把 store 的主档/raw 目录指到给定数据根(worktree 场景必调)。"""
    from tools.store import repo as store
    store._MASTER_DIR = root / "master"
    store._RAW_DIR = root / "raw"


def analysis_dir(root: Path) -> Path:
    """该数据根下的 analysis 目录(消息面 sentiment_policy.json 所在)。"""
    return root / "analysis"


@lru_cache(maxsize=1)
def ensure_data_root(cli_root: str | None = None) -> Path:
    """解析数据根 + 落到 store,返回 Path。带缓存,重复调用零成本。"""
    root = resolve_data_root(cli_root)
    apply_to_store(root)
    return root
