#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内容回退护栏(docs commit regression guard)：自动提交分析文档前，拦截
「用一份更旧的磁盘副本静默覆盖掉已提交内容」的回退，绝不把回退提交上去。

背景(本护栏根治的静默失效)
--------------------------
`ops/launchd/commit_analysis_docs.sh` 是每日多次跑的自动提交作业。它把常驻 worktree
`reset --hard origin/main` 后，用 `cp -f` 把**主仓工作树 / dailyjob worktree** 的磁盘副本
覆盖进去再 commit+push（设计上「同名以主仓为准」）。这条链隐含一个会破裂的假设：
**磁盘副本恒比 origin/main 新/权威**。

当某条经验/复盘结论是在**另一个 feature worktree** 里写好并入库到 origin/main 时
（本项目多窗口并发，这是常态），主仓工作树磁盘上留着的仍是**入库前的旧副本**。
`cp -f` 于是用旧副本盖掉了 origin/main 上刚提交的新内容 → 自动提交把这份回退 commit 上去
= **已验证结论被静默丢失**（实证：v2026-09-14 的 #31/#10「已验证」条目被自动提交
`082357d` 逐字节退回富化前状态；见 docs/每日分析/经验沉淀/v2026-09-14.md 文件史）。
这与词表漂移、防复发锁失效同属「静默失效」病；经验库是本项目的可移交记忆，
静默丢数据比测试红更严重。

本模块的职责
------------
在自动提交 commit **之前**，对每个「改动了 origin/main 已有内容」的白名单文档，
判断这次改动是不是**内容回退**（丢掉了已提交的内容而没有真正新增）：

  · 通用判据(所有白名单文档)：incoming 相对 committed 是**纯删除**——committed 有、
    incoming 没有的非空行存在，且 incoming 没有任何 committed 缺的新行（= 旧副本覆盖）。
  · 经验沉淀语义锁(路径含「经验沉淀」)：committed 里的 **§4 编号条目**(`**#N ·`)与
    **changelog 版本头**(`- **v<date>**`) 必须在 incoming **全部保留**，少任一条即回退
    （即使 incoming 另外新增了别的内容，也不许悄悄丢掉一条已验证结论）。
  · 暂存删除(status D)一个已提交的白名单文档：自动提交绝不该删文档 → 一律判回退。

命中处理：**不提交该文件**——用 `git checkout HEAD -- <file>` 把它还原成 origin/main 的
好版本（索引+工作树都回到已提交内容，这一步既保住内容、又让它不再作为改动被暂存），
其余安全文件照常提交；同时**大声告警**（stdout `!!! 护栏告警`，shell 侧落日志）并写
marker 文件供监控/人工扫描。绝不静默吞。
自愈：下一轮 worktree 仍 reset 到 origin/main 的好版本，磁盘副本仍旧 → 继续跳过，
内容始终不丢，直到人把磁盘副本同步上来（此时不再是纯删除/丢条目 → 正常放行）。

设计取舍：**跳过单文件 + 告警**，而不是整体中止——中止会连累同批合法新文档入库。

退出码(best-effort,任何内部异常都不外溢、不阻断自动提交)
  0  无回退，放行
  10 检测到回退并已中和(相关文件还原成已提交好版本)；调用方应落告警日志、继续提交其余
  1  内部异常(尽力而为，调用方按放行处理但落日志)

CLI:
  python -m tools.ops.docs_commit_guard --repo <worktree> \
      --paths "docs/每日分析/选股" "docs/每日分析/复盘" ... [--marker <path>] [--dry-run]

⚠️ 研究模拟，非投资建议。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# —— 经验沉淀语义锁的两个「不得丢失」实体 ——
# §4 编号条目头：形如 `**#31 · 连续普跌日…`（行首加粗井号编号加中点）。
#   只匹配条目头，不匹配正文里「强化 #26」这类内联引用（那些没有 `**` 包裹 + 中点）。
_ITEM_HEADER_RE = re.compile(r"\*\*#(\d+)\s*·")
# changelog 版本头：形如 `- **v2026-09-14**：…`
_CHANGELOG_VER_RE = re.compile(r"\*\*v(\d{4}-\d{2}-\d{2})\*\*")

EXPERIENCE_MARKER = "经验沉淀"


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=check
    )


def _git_bytes(repo: str, *args: str) -> Optional[bytes]:
    """跑 git 并返回原始 stdout 字节；非零退出返回 None（文件不存在等）。"""
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True)
    if p.returncode != 0:
        return None
    return p.stdout


def line_sets(text: str) -> set[str]:
    """一段文本里所有非空行(strip 后)的集合，用于纯删除判据。"""
    return {ln.strip() for ln in text.splitlines() if ln.strip()}


def item_headers(text: str) -> set[str]:
    """经验沉淀 §4 编号条目号集合(字符串形式，如 {'10','31'})。"""
    return set(_ITEM_HEADER_RE.findall(text))


def changelog_versions(text: str) -> set[str]:
    """经验沉淀 changelog 版本日期集合(如 {'2026-09-14'})。"""
    return set(_CHANGELOG_VER_RE.findall(text))


def is_experience_path(path: str) -> bool:
    return EXPERIENCE_MARKER in path


def detect_regression(path: str, committed: str, incoming: str) -> list[str]:
    """返回回退原因列表(空 = 非回退，放行)。committed/incoming 为文本内容。"""
    reasons: list[str] = []

    # 经验沉淀语义锁：已提交的 §4 条目 / changelog 版本头一条都不许丢。
    if is_experience_path(path):
        miss_items = item_headers(committed) - item_headers(incoming)
        if miss_items:
            reasons.append(
                "丢失已提交 §4 编号条目 #" + ",#".join(sorted(miss_items, key=int))
            )
        miss_vers = changelog_versions(committed) - changelog_versions(incoming)
        if miss_vers:
            reasons.append("丢失已提交 changelog 版本 " + ",".join(sorted(miss_vers)))

    # 通用纯删除判据：committed 有、incoming 没有的非空行存在，且 incoming 无任何新增行。
    c_set, i_set = line_sets(committed), line_sets(incoming)
    removed = c_set - i_set
    added = i_set - c_set
    if removed and not added:
        reasons.append(f"纯删除 {len(removed)} 行且无任何新增(疑似旧副本覆盖已提交内容)")

    return reasons


@dataclass
class Finding:
    path: str
    status: str  # 'M' | 'D'
    reasons: list[str]


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def has_regression(self) -> bool:
        return bool(self.findings)


def _staged_changes(repo: str, paths: list[str]) -> list[tuple[str, str]]:
    """返回暂存区里白名单路径下的 (status, path) 列表；status 取首字母(A/M/D/R…)。"""
    out = _git(repo, "diff", "--cached", "--name-status", "-z", "--", *paths).stdout
    tokens = out.split("\0")
    changes: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        status = tok[0]
        if status == "R":  # 重命名：status 后跟 旧路径、新路径两个字段
            new_path = tokens[i + 2] if i + 2 < len(tokens) else ""
            changes.append(("M", new_path))
            i += 3
        else:
            path = tokens[i + 1] if i + 1 < len(tokens) else ""
            changes.append((status, path))
            i += 2
    return changes


def guard(repo: str, paths: list[str], restore: bool = True) -> Report:
    """核心：扫暂存区，找出内容回退的白名单文档；restore=True 时还原成 HEAD 好版本。"""
    report = Report()
    try:
        changes = _staged_changes(repo, paths)
    except subprocess.CalledProcessError as e:  # git 异常，best-effort 不外溢
        report.error = f"读取暂存区失败: {e.stderr or e}"
        return report

    for status, path in changes:
        if not path:
            continue
        if status == "A":
            continue  # 新增文件无已提交基线，不可能回退
        committed_bytes = _git_bytes(repo, "show", f"HEAD:{path}")
        if committed_bytes is None:
            continue  # HEAD 无此文件 → 视作新增，放行
        committed = committed_bytes.decode("utf-8", errors="replace")

        if status == "D":
            reasons = ["自动提交暂存了对已提交白名单文档的删除"]
        else:
            fp = Path(repo) / path
            try:
                incoming = fp.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                report.error = f"读取工作树文件失败 {path}: {e}"
                continue
            reasons = detect_regression(path, committed, incoming)

        if reasons:
            report.findings.append(Finding(path=path, status=status, reasons=reasons))
            if restore:
                try:
                    _git(repo, "checkout", "HEAD", "--", path)
                    report.restored.append(path)
                except subprocess.CalledProcessError as e:
                    report.error = f"还原 {path} 失败: {e.stderr or e}"

    return report


def _write_marker(marker: Path, report: Report) -> None:
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "alarm": "docs_commit_regression",
        "restored": report.restored,
        "findings": [
            {"path": f.path, "status": f.status, "reasons": f.reasons}
            for f in report.findings
        ],
        "note": (
            "自动提交检测到内容回退：这些白名单文档的磁盘副本会丢失 origin/main 已提交内容，"
            "已跳过并还原为已提交好版本。请把磁盘副本同步到最新后再让其入库。"
        ),
    }
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass  # best-effort，marker 写不了不阻断


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="分析文档自动提交·内容回退护栏")
    ap.add_argument("--repo", required=True, help="要检查的 git worktree(已暂存待提交)")
    ap.add_argument("--paths", nargs="+", required=True, help="白名单目录/文件")
    ap.add_argument("--marker", help="命中回退时写入的 marker JSON 路径")
    ap.add_argument(
        "--dry-run", action="store_true", help="只诊断不还原(仍打印告警、写 marker)"
    )
    args = ap.parse_args(argv)

    try:
        report = guard(args.repo, args.paths, restore=not args.dry_run)
    except Exception as e:  # noqa: BLE001 —— best-effort，任何异常都不外溢阻断自动提交
        print(f"!! 护栏内部异常(放行，best-effort): {e}", file=sys.stderr)
        return 1

    if report.error:
        print(f"!! 护栏内部异常(放行，best-effort): {report.error}", file=sys.stderr)
        # 有 finding 就仍按回退处理，否则放行
        if not report.has_regression:
            return 1

    if not report.has_regression:
        return 0

    print("!!! 护栏告警：自动提交检测到内容回退，已跳过并保护已提交内容")
    for f in report.findings:
        action = "已还原为已提交好版本" if f.path in report.restored else (
            "仅诊断(dry-run)" if args.dry_run else "还原失败(见上)"
        )
        print(f"  · [{f.status}] {f.path} —— {'; '.join(f.reasons)} → {action}")

    if args.marker:
        _write_marker(Path(args.marker), report)

    return 10


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
