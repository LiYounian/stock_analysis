# -*- coding: utf-8 -*-
"""锁死 safe_ff 的核心不变量:**只清伪冲突,绝不丢真改动**。

用真 git 仓搭「origin ⇢ 主仓 local main 落后 + 工作树留副本」的现场,断言:
  ① 未跟踪伪冲突(逐字节 = origin):被清 + ff 成功 + local main 追平;
  ② 未跟踪伪冲突(本地更旧、0 独有行):被清 + ff 成功;
  ③ 未跟踪真改动(本地含 origin 没有的行):**拒绝**——不清、不 ff、文件与内容原样保留;
  ④ 已跟踪文件本地改动 = 真改动:同样拒绝、零丢失;
  ⑤ origin 不跟踪的本地新增(新日期产出):**不是**撞车,永不触碰,ff 照常成功;
  ⑥ 混入 1 个真改动时,连同其它伪冲突**一并拒绝**(整体不 ff),真改动零丢失;
  ⑦ already-synced / diverged 的边界行为。
⚠️ 研究模拟,非投资建议。
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tools.ops import safe_ff  # noqa: E402


def _run(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True)


def _commit_all(repo, msg):
    _run(repo, "add", "-A")
    _run(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", msg)


def _head(repo):
    return _run(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def world(tmp_path):
    """搭 origin(裸)+ 主仓 clone;主仓 local main 停在旧提交、origin 领先一提交。
    返回 (main_repo, origin_head_sha, base_sha)。
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "seed"
    main = tmp_path / "main"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)

    # seed:建 base 提交并 push
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    _run(str(work), "config", "core.quotepath", "false")
    d = work / "docs" / "每日分析" / "选股"
    d.mkdir(parents=True)
    (d / "base.md").write_text("base\n", encoding="utf-8")
    _commit_all(str(work), "base")
    _run(str(work), "branch", "-M", "main")
    _run(str(work), "remote", "add", "origin", str(origin))
    _run(str(work), "push", "-q", "origin", "main")

    # 主仓 clone 到 base（此刻 local main == origin）
    subprocess.run(["git", "clone", "-q", str(origin), str(main)], check=True)
    _run(str(main), "config", "core.quotepath", "false")
    base = _head(str(main))

    # origin 前进一步:新增 origin 版 docs 并 push（主仓不 pull → local main 落后）
    (d / "2026-09-16.md").write_text("line-a\nline-b\nline-c\n", encoding="utf-8")
    (work / "docs" / "每日分析" / "复盘").mkdir(parents=True)
    (work / "docs" / "每日分析" / "复盘" / "2026-09-16.md").write_text("review-x\nreview-y\n", encoding="utf-8")
    _commit_all(str(work), "origin advance")
    _run(str(work), "push", "-q", "origin", "main")
    _run(str(main), "fetch", "-q", "origin")
    origin_head = _run(str(main), "rev-parse", "origin/main").stdout.strip()
    return str(main), origin_head, base


def test_pseudo_identical_untracked_cleaned_and_ff(world):
    """① 未跟踪副本与 origin 逐字节一致 → 清 + ff 追平。"""
    main, origin_head, base = world
    p = os.path.join(main, "docs/每日分析/选股/2026-09-16.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("line-a\nline-b\nline-c\n")  # == origin
    res = safe_ff.safe_ff(main, apply=True, log=lambda *_: None)
    assert res.ffd is True and res.reason == "ffd"
    assert _head(main) == origin_head
    assert "docs/每日分析/选股/2026-09-16.md" in res.cleaned
    assert not res.refused


def test_pseudo_older_subset_untracked_cleaned(world):
    """② 未跟踪副本是更旧子集(0 独有行)→ 清 + ff。"""
    main, origin_head, _ = world
    p = os.path.join(main, "docs/每日分析/选股/2026-09-16.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("line-a\nline-b\n")  # origin 有 a/b/c,本地只 a/b → 无独有行
    res = safe_ff.safe_ff(main, apply=True, log=lambda *_: None)
    assert res.ffd is True
    assert _head(main) == origin_head


def test_genuine_untracked_refused_no_loss(world):
    """③ 未跟踪副本含 origin 没有的行 = 真改动 → 拒绝,文件与内容原样保留。"""
    main, origin_head, base = world
    p = os.path.join(main, "docs/每日分析/选股/2026-09-16.md")
    payload = "line-a\nline-b\nline-c\nLOCAL-UNIQUE-真改动\n"
    with open(p, "w", encoding="utf-8") as f:
        f.write(payload)
    res = safe_ff.safe_ff(main, apply=True, log=lambda *_: None)
    assert res.ffd is False and res.reason == "refused"
    assert "docs/每日分析/选股/2026-09-16.md" in res.refused
    assert _head(main) == base                 # local main 未推进
    assert os.path.exists(p)                    # 文件仍在
    with open(p, encoding="utf-8") as f:
        assert f.read() == payload              # 内容零丢失
    assert res.cleaned == []                    # 什么都没清


def test_genuine_modified_tracked_refused_no_loss(world):
    """④ 已跟踪文件的本地改动含独有行 = 真改动 → 拒绝,零丢失。"""
    main, origin_head, base = world
    p = os.path.join(main, "docs/每日分析/选股/base.md")
    # 让 origin 也改了 base.md,制造 HEAD..origin 对 base.md 的改动 → 本地改动才成撞车
    # 用另一 clone 推 origin 对 base.md 的修改
    tmp2 = os.path.join(os.path.dirname(main), "seed2")
    subprocess.run(["git", "clone", "-q", os.path.join(os.path.dirname(main), "origin.git"), tmp2], check=True)
    with open(os.path.join(tmp2, "docs/每日分析/选股/base.md"), "w", encoding="utf-8") as f:
        f.write("base\norigin-added\n")
    _commit_all(tmp2, "origin edits base")
    _run(tmp2, "push", "-q", "origin", "main")
    _run(main, "fetch", "-q", "origin")
    origin_head2 = _run(main, "rev-parse", "origin/main").stdout.strip()
    # 主仓本地对 base.md 做真改动(含独有行)
    with open(p, "w", encoding="utf-8") as f:
        f.write("base\nLOCAL-真改动\n")
    res = safe_ff.safe_ff(main, apply=True, log=lambda *_: None)
    assert res.reason == "refused" and res.ffd is False
    assert "docs/每日分析/选股/base.md" in res.refused
    with open(p, encoding="utf-8") as f:
        assert f.read() == "base\nLOCAL-真改动\n"   # 未被 checkout 覆盖


def test_local_new_untracked_not_a_blocker(world):
    """⑤ origin 不跟踪的本地新增(新日期产出)不是撞车 → 永不触碰,ff 照常成功。"""
    main, origin_head, _ = world
    newdir = os.path.join(main, "data", "analysis", "2026-09-17")
    os.makedirs(newdir)
    keep = os.path.join(newdir, "picks.json")
    with open(keep, "w", encoding="utf-8") as f:
        f.write('{"local":"only"}\n')
    # 同时让本该 ff 引入的 origin docs 以一致副本存在(伪冲突),确保 ff 真能跑
    with open(os.path.join(main, "docs/每日分析/选股/2026-09-16.md"), "w", encoding="utf-8") as f:
        f.write("line-a\nline-b\nline-c\n")
    _rv = os.path.join(main, "docs/每日分析/复盘/2026-09-16.md")
    os.makedirs(os.path.dirname(_rv), exist_ok=True)
    with open(_rv, "w", encoding="utf-8") as f:
        f.write("review-x\nreview-y\n")
    res = safe_ff.safe_ff(main, apply=True, log=lambda *_: None)
    assert res.ffd is True
    assert _head(main) == origin_head
    assert os.path.exists(keep)                          # 本地新增文件仍在
    with open(keep, encoding="utf-8") as f:
        assert f.read() == '{"local":"only"}\n'          # 内容不变
    assert all("2026-09-17" not in c for c in res.cleaned)


def test_mixed_one_genuine_blocks_all(world):
    """⑥ 一个伪冲突 + 一个真改动 → 整体拒绝,伪的也不清、真的零丢失。"""
    main, origin_head, base = world
    pseudo_p = os.path.join(main, "docs/每日分析/选股/2026-09-16.md")
    with open(pseudo_p, "w", encoding="utf-8") as f:
        f.write("line-a\nline-b\nline-c\n")              # 伪冲突
    genuine_p = os.path.join(main, "docs/每日分析/复盘/2026-09-16.md")
    os.makedirs(os.path.dirname(genuine_p), exist_ok=True)
    with open(genuine_p, "w", encoding="utf-8") as f:
        f.write("review-x\nreview-y\nLOCAL-真改动\n")     # 真改动
    res = safe_ff.safe_ff(main, apply=True, log=lambda *_: None)
    assert res.reason == "refused" and res.ffd is False
    assert _head(main) == base
    assert res.cleaned == []                              # 伪的也没清
    assert os.path.exists(pseudo_p)
    with open(genuine_p, encoding="utf-8") as f:
        assert "LOCAL-真改动" in f.read()


def test_already_synced_noop(world):
    """⑦a local main 已等于 origin/main → already-synced,不动。"""
    main, origin_head, _ = world
    _run(main, "merge", "--ff-only", "origin/main")       # 先手动追平
    res = safe_ff.safe_ff(main, apply=True, log=lambda *_: None)
    assert res.reason == "already-synced" and res.ffd is False


def test_diverged_refused(world):
    """⑦b 主仓 local main 分叉(有独立提交)→ 拒绝,绝不 reset。"""
    main, origin_head, base = world
    with open(os.path.join(main, "docs/每日分析/选股/base.md"), "w", encoding="utf-8") as f:
        f.write("diverge\n")
    _commit_all(main, "local diverge commit")
    res = safe_ff.safe_ff(main, apply=True, log=lambda *_: None)
    assert res.reason == "diverged" and res.ffd is False


def test_unique_lines_helper():
    """单元:local_unique_lines / is_pseudo_conflict 判据。"""
    assert safe_ff.local_unique_lines(b"a\nb\n", b"a\nb\nc\n") == 0
    assert safe_ff.local_unique_lines(b"a\nX\n", b"a\nb\nc\n") == 1
    assert safe_ff.is_pseudo_conflict(b"a\nb\n", b"a\nb\n") is True
    assert safe_ff.is_pseudo_conflict(b"a\nb\n", b"a\nb\nc\n") is True
    assert safe_ff.is_pseudo_conflict(b"a\nX\n", b"a\nb\nc\n") is False
