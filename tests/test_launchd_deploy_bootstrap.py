"""部署卫生 ③b · 薄 bootstrap + deploy worktree 的行为契约测试。

锁住"为什么改"的语义(防未来 prompt/代码重写无意破坏):
  1. 部署闭环:origin/main 上 wrapper 从 v1→v2 后,bootstrap 下一跑 exec 到 **v2**
     (被执行的 wrapper 本体恒等最新 origin/main,与主仓工作树无关)。
  2. 主仓脏不阻断 + WIP 保护:存在一个"主仓"含未跟踪+已跟踪本地改动时,bootstrap 照常
     跑最新 wrapper,且**绝不触碰该主仓目录**。
  3. fail-loud:deploy worktree 缺失 → exit 3;目标 wrapper 缺失 → exit 4(拒绝静默跑旧码)。
  4. 无裸 stash / 不碰主仓:bootstrap+provision 源码静态不含裸 `git stash`,只对 deploy worktree reset。

设计:docs/计划/2026-09-10_部署卫生根治_设计.md / _实现规格.md
注:设计 §六 第 3 条(ff-only 不被分析文档挡)属 ①a 范畴,本轮不实现,留下轮。
自包含:仅用 git + bash,不依赖项目重依赖。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO_ROOT / "ops" / "launchd" / "_bootstrap.sh"
PROVISION = REPO_ROOT / "ops" / "launchd" / "provision_deploy.sh"


def _git(cwd: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t.t",
        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t.t",
    )
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env=env,
        check=True, capture_output=True, text=True,
    ).stdout


def _write_wrapper(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/bash\necho 'WRAPPER_MARKER={marker}'\nexit 0\n")


@pytest.fixture()
def deploy_env(tmp_path: Path):
    """搭一套:bare origin + seed 克隆 + deploy worktree(detached origin/main)。

    返回 (run_bootstrap, deploy_wt, seed, origin, push_new_wrapper_version)。
    """
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--quiet", "--bare", str(origin))

    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "--quiet", str(origin), str(seed))
    _write_wrapper(seed / "ops" / "launchd" / "faketest.sh", "v1")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "wrapper v1")
    _git(seed, "push", "--quiet", "origin", "HEAD:main")
    _git(seed, "fetch", "--quiet", "origin")

    deploy_wt = tmp_path / "deploy"
    _git(seed, "worktree", "add", "--detach", str(deploy_wt), "origin/main")

    def push_new_wrapper_version(marker: str) -> None:
        _write_wrapper(seed / "ops" / "launchd" / "faketest.sh", marker)
        _git(seed, "add", "-A")
        _git(seed, "commit", "--quiet", "-m", f"wrapper {marker}")
        _git(seed, "push", "--quiet", "origin", "HEAD:main")

    def run_bootstrap(wrapper: str = "faketest.sh", deploy: Path | None = None):
        env = dict(os.environ)
        env["STOCK_DEPLOY_WORKTREE"] = str(deploy if deploy is not None else deploy_wt)
        return subprocess.run(
            ["/bin/bash", str(BOOTSTRAP), wrapper],
            env=env, capture_output=True, text=True,
        )

    return run_bootstrap, deploy_wt, seed, origin, push_new_wrapper_version


def test_deploy_closed_loop_execs_latest_origin_main(deploy_env):
    """origin/main 上 wrapper v1→v2 后,bootstrap 下一跑 exec 到 v2。"""
    run_bootstrap, _deploy, _seed, _origin, push_new = deploy_env

    r1 = run_bootstrap()
    assert r1.returncode == 0, r1.stderr
    assert "WRAPPER_MARKER=v1" in r1.stdout

    push_new("v2")  # 只推到 origin/main,不碰 deploy worktree 工作树

    r2 = run_bootstrap()
    assert r2.returncode == 0, r2.stderr
    assert "WRAPPER_MARKER=v2" in r2.stdout, "bootstrap 未 reset 到最新 origin/main → 跑了旧 wrapper"


def test_main_repo_untouched_and_wip_protected(deploy_env, tmp_path: Path):
    """存在含未跟踪+本地改动的'主仓'时,bootstrap 照常跑且绝不触碰该主仓目录。"""
    run_bootstrap, _deploy, _seed, origin, _push = deploy_env

    # 造一个独立"主仓":有已跟踪本地改动 + 未跟踪文件(模拟别的会话 WIP + 分析文档)
    main_repo = tmp_path / "main_repo"
    _git(tmp_path, "clone", "--quiet", str(origin), str(main_repo))
    (main_repo / "ops" / "launchd" / "faketest.sh").write_text("#!/bin/bash\n# LOCAL WIP EDIT\n")
    (main_repo / "docs_analysis_untracked.md").write_text("未跟踪分析文档 WIP\n")
    status_before = _git(main_repo, "status", "--porcelain")
    head_before = _git(main_repo, "rev-parse", "HEAD")

    r = run_bootstrap()
    assert r.returncode == 0, r.stderr

    status_after = _git(main_repo, "status", "--porcelain")
    head_after = _git(main_repo, "rev-parse", "HEAD")
    assert status_before == status_after, "bootstrap 改动了主仓工作树(应完全隔离)"
    assert head_before == head_after, "bootstrap 移动了主仓 HEAD(应完全隔离)"


def test_fail_loud_when_deploy_worktree_missing(deploy_env, tmp_path: Path):
    run_bootstrap, *_ = deploy_env
    r = run_bootstrap(deploy=tmp_path / "does_not_exist")
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert "deploy worktree 不存在" in r.stderr


def test_fail_loud_when_wrapper_missing(deploy_env):
    run_bootstrap, *_ = deploy_env
    r = run_bootstrap(wrapper="no_such_wrapper.sh")
    assert r.returncode == 4, (r.returncode, r.stderr)
    assert "目标 wrapper 不存在" in r.stderr


def test_no_bare_stash_and_only_touches_deploy():
    """静态断言:bootstrap+provision 不含裸 git stash;git 写操作只对 deploy worktree。"""
    for script in (BOOTSTRAP, PROVISION):
        src = script.read_text()
        assert "git stash" not in src, f"{script.name} 含裸 git stash(禁,共享栈会误伤别的会话 WIP)"
    boot = BOOTSTRAP.read_text()
    # bootstrap 的 reset --hard 必须限定在 DEPLOY_WT 上(git -C "$DEPLOY_WT")
    assert 'git -C "$DEPLOY_WT" reset --hard origin/main' in boot
    # bootstrap 绝不出现对主仓的 checkout/merge/pull
    assert "git checkout" not in boot and "git merge" not in boot and "git pull" not in boot
