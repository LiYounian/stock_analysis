#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全快进（safe fast-forward）：把主仓工作树的 local main 追平 origin/main，
只清理「伪冲突」文件，绝不丢任何真未提交改动。

背景（ff-sync 根治）
--------------------
本项目多窗口 + 定时任务并发。commitdocs 等任务在**独立 worktree** 里把每日分析
docs（以及分析师会话产出的 data/analysis 等被跟踪文件）提交并 push 到 origin/main，
**绕过主仓工作树的 local main**。主仓工作树自己也留着这些同名文件的未跟踪/已改副本
→ 主仓 local main 落后 origin/main，`git merge --ff-only origin/main` 报
「untracked working tree files would be overwritten」/「local changes would be
overwritten」而 abort。这些撞车文件**内容与 origin/main 一致或本地更旧（无独有行）**，
是**伪冲突**——不是真改动丢失。结果:主仓长期落后几十提交,每次 load 新 job 都要手动清。

本模块的职责
------------
在**只清伪冲突、绝不删真改动**的铁律下,把挡 ff 的文件安全清掉,再 `merge --ff-only`。

核心不变量(单测锁死)
--------------------
一个撞车文件只有满足「伪冲突」判据才会被清理:
  - 与 origin/main 版本**逐字节一致**(最常见:commitdocs 从 worktree push 的副本),或
  - 本地副本**没有任何一行是 origin/main 版本所缺的**(「0 独有行」——本地是更旧/子集版本)。
只要有**任一**撞车文件不满足上述判据(即本地含 origin 没有的行 = 真改动),
就**整体拒绝**:不清任何文件、不 ff,记录明细留待人工核查。

其余未跟踪文件(origin/main 不跟踪的、即真正的本地新增,如新日期的 data/analysis/)
**不是** ff 的阻挡项(ff 不会覆盖它们),本模块**永不触碰**。

用法
----
  python -m tools.ops.safe_ff --repo /path/to/主仓            # 预演(只诊断,不动)
  python -m tools.ops.safe_ff --repo /path/to/主仓 --apply    # 清伪冲突 + ff
best-effort:任何内部异常都不外溢(退出码语义见 main),绝不因追平失败而拖垮调用它的任务。

⚠️ 研究模拟,非投资建议。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Optional

REMOTE_REF = "origin/main"


def _git(repo: str, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    """跑一条 git 子命令,返回 CompletedProcess(text 模式,bytes 用 _git_bytes)。"""
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, check=check,
    )


def _git_bytes(repo: str, *args: str) -> Optional[bytes]:
    """取原始字节(用于逐字节比对 blob 内容);失败/不存在返回 None。"""
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True)
    if p.returncode != 0:
        return None
    return p.stdout


def origin_blob(repo: str, path: str) -> Optional[bytes]:
    """origin/main 版本的文件字节;origin 不跟踪该路径(如已删)→ None。"""
    return _git_bytes(repo, "show", f"{REMOTE_REF}:{path}")


def local_unique_lines(local: bytes, origin: bytes) -> int:
    """本地相对 origin 的「独有行」条数(multiset 差)。
    0 = 本地每一行都在 origin 版本里出现过(伪冲突;本地是一致或更旧的子集)。
    >0 = 本地含 origin 没有的行 = 真改动,不可清。
    """
    lo = Counter(local.splitlines())
    og = Counter(origin.splitlines())
    diff = lo - og  # multiset 差:只保留 lo 比 og 多出来的
    return sum(diff.values())


def is_pseudo_conflict(local: bytes, origin: bytes) -> bool:
    """伪冲突判据:逐字节一致,或本地无独有行(0 独有行)。"""
    if local == origin:
        return True
    return local_unique_lines(local, origin) == 0


@dataclass
class Blocker:
    path: str
    wt_state: str          # "untracked" | "modified"
    verdict: str = ""      # "pseudo" | "genuine" | "skip"
    reason: str = ""


@dataclass
class Result:
    ok: bool = True                 # 流程本身有没有异常(best-effort:一般恒 True)
    ffd: bool = False               # 是否真的执行了 ff(推进了 local main)
    reason: str = ""                # 顶层结论(already-synced / diverged / ffd / refused / clean-ff)
    head: str = ""
    origin: str = ""
    blockers: list = field(default_factory=list)   # list[Blocker]
    cleaned: list = field(default_factory=list)     # 实际清理的路径
    refused: list = field(default_factory=list)     # 阻断 ff 的真改动路径


def _porcelain_dirty(repo: str) -> dict:
    """解析工作树脏状态 → {path: "untracked"|"modified"}。含已暂存的改动。"""
    p = _git(repo, "status", "--porcelain=v1", "--untracked-files=all", "-z")
    out = p.stdout
    dirty: dict = {}
    # -z:记录以 NUL 分隔;重命名条目会多带一个 NUL(旧名),这里只关心新名。
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        rec = tokens[i]
        if not rec:
            i += 1
            continue
        xy, path = rec[:2], rec[3:]
        if xy == "??":
            dirty[path] = "untracked"
        elif "R" in xy or "C" in xy:
            # 重命名/拷贝:下一 token 是来源旧名,消费掉;新名(path)视为已改
            dirty[path] = "modified"
            i += 1  # 跳过来源
        else:
            dirty[path] = "modified"
        i += 1
    return dirty


def compute_blockers(repo: str, head: str, origin: str) -> list:
    """算出会挡 `merge --ff-only origin/main` 的撞车文件并逐个判定伪/真。

    撞车集 = (HEAD..origin 会改动的路径) ∩ (工作树脏路径)。
    origin 侧删除(D)的路径即便本地有未跟踪副本也不会被 ff 覆盖 → 跳过。
    """
    ns = _git(repo, "diff", "--name-status", "-z", head, origin).stdout
    incoming: dict = {}   # path -> status(A/M/D/...)
    toks = ns.split("\0")
    i = 0
    while i < len(toks):
        st = toks[i]
        if not st:
            i += 1
            continue
        if st[0] in ("R", "C"):
            # 重命名/拷贝:后跟 src、dst 两个路径
            src = toks[i + 1] if i + 1 < len(toks) else ""
            dst = toks[i + 2] if i + 2 < len(toks) else ""
            if src:
                incoming[src] = "D"   # 旧名被移走,等价删除
            if dst:
                incoming[dst] = "A"
            i += 3
            continue
        path = toks[i + 1] if i + 1 < len(toks) else ""
        if path:
            incoming[path] = st[0]
        i += 2

    dirty = _porcelain_dirty(repo)
    blockers: list = []
    for path, state in dirty.items():
        if path not in incoming:
            continue  # 该脏文件不在 ff 改动范围 → 不挡 ff,永不触碰
        if incoming[path] == "D":
            continue  # origin 删除该文件,本地副本不会被覆盖
        origin_bytes = origin_blob(repo, path)
        if origin_bytes is None:
            # 理论上 A/M 应有 origin blob;取不到则保守当真冲突(不清)
            blockers.append(Blocker(path, state, "genuine", "origin blob 取不到,保守拒清"))
            continue
        local_path = os.path.join(repo, path)
        try:
            with open(local_path, "rb") as fh:
                local_bytes = fh.read()
        except OSError as e:
            blockers.append(Blocker(path, state, "genuine", f"读本地副本失败:{e}"))
            continue
        if is_pseudo_conflict(local_bytes, origin_bytes):
            reason = "逐字节一致" if local_bytes == origin_bytes else "本地无独有行(更旧子集)"
            blockers.append(Blocker(path, state, "pseudo", reason))
        else:
            n = local_unique_lines(local_bytes, origin_bytes)
            blockers.append(Blocker(path, state, "genuine", f"本地含 {n} 行 origin 没有的内容(真改动)"))
    blockers.sort(key=lambda b: b.path)
    return blockers


def safe_ff(repo: str, apply: bool = False, log: Callable[[str], None] = print) -> Result:
    """主流程:诊断撞车 → (可选)清伪冲突 + ff。绝不 reset/不 clean 非撞车项。"""
    res = Result()
    repo = os.path.abspath(repo)

    # fetch(与主仓共享对象库;失败用现有 origin/main,不硬失败)
    if _git(repo, "fetch", "--quiet", "origin").returncode != 0:
        log("!! git fetch 失败,用现有 origin/main")

    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    origin = _git(repo, "rev-parse", REMOTE_REF).stdout.strip()
    res.head, res.origin = head, origin
    if not head or not origin:
        res.ok = False
        res.reason = "取 HEAD/origin 失败"
        log(f"!! {res.reason}")
        return res

    if head == origin:
        res.reason = "already-synced"
        log(f"local main 已是 origin/main({origin[:7]}),无需 ff")
        return res

    # 必须 HEAD 是 origin/main 祖先才可 ff(分叉则拒绝,绝不 reset)
    if _git(repo, "merge-base", "--is-ancestor", head, origin).returncode != 0:
        res.reason = "diverged"
        log(f"!! local main({head[:7]})与 origin/main({origin[:7]})分叉,拒绝(不 reset,交人工)")
        return res

    blockers = compute_blockers(repo, head, origin)
    res.blockers = blockers
    genuine = [b for b in blockers if b.verdict == "genuine"]
    pseudo = [b for b in blockers if b.verdict == "pseudo"]

    for b in blockers:
        log(f"  撞车[{b.wt_state}] {b.path} → {b.verdict}({b.reason})")

    if genuine:
        res.refused = [b.path for b in genuine]
        res.reason = "refused"
        log(f"!! 检出 {len(genuine)} 个真改动撞车,整体拒绝清理与 ff(留人工核查):{res.refused}")
        return res

    if not apply:
        res.reason = "dry-run"
        log(f"预演:{len(pseudo)} 个伪冲突可清、0 真改动;加 --apply 执行 ff")
        return res

    # 只清伪冲突(逐文件已核 0 独有行)
    for b in pseudo:
        target = os.path.join(repo, b.path)
        if b.wt_state == "untracked":
            try:
                os.remove(target)
                res.cleaned.append(b.path)
            except OSError as e:
                res.ok = False
                res.reason = f"清未跟踪副本失败:{b.path}:{e}"
                log(f"!! {res.reason}")
                return res
        else:  # modified:恢复到 HEAD,让 ff 能推进
            if _git(repo, "checkout", "HEAD", "--", b.path).returncode != 0:
                res.ok = False
                res.reason = f"checkout 恢复失败:{b.path}"
                log(f"!! {res.reason}")
                return res
            res.cleaned.append(b.path)

    ff = _git(repo, "merge", "--ff-only", origin)
    if ff.returncode == 0:
        res.ffd = True
        res.reason = "ffd"
        log(f"✅ 已 ff local main:{head[:7]} -> {origin[:7]}(清理 {len(res.cleaned)} 伪冲突)")
    else:
        res.ok = False
        res.reason = "ff-failed"
        log(f"!! 清理后 ff 仍失败(可能有本模块未覆盖的阻挡):{ff.stderr.strip()}")
    return res


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="安全 ff 主仓 local main 到 origin/main(只清伪冲突)")
    ap.add_argument("--repo", required=True, help="主仓工作树根目录")
    ap.add_argument("--apply", action="store_true", help="执行清理+ff(缺省只预演诊断)")
    args = ap.parse_args(argv)

    res = safe_ff(args.repo, apply=args.apply)
    # 退出码:0=正常(含 already-synced/ffd/dry-run/refused 均属「流程正常」,不外溢);
    #         2=refused(有真改动,给调用方一个可判别信号但不当作致命);3=内部异常。
    if not res.ok:
        return 3
    if res.reason == "refused":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
