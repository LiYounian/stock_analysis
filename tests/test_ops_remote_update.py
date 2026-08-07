"""远端自动更新/自愈单测(ops.remote_update)。

锁住本地可验证的逻辑:环境检查判定 / 缺配置报错 / DB 目录自愈 / 重启命令构造 /
更新有变更装依赖 / 无变更不装 / 重启健康检查失败→回滚 / 参数解析。
runner 与 health_check 全注入,不碰真实服务器 / systemd / 网络。
"""
import pytest

from ops.remote_update import (Problem, RemoteConfig, Service, build_restart_cmd,
                               build_rollback_cmd, check_env, parse_args,
                               parse_services, run_update)

GOOD_ENV = {"STORE_BACKEND": "db", "SYNC_INGEST_TOKEN": "t", "SYNC_SIGNING_KEY": "k"}


@pytest.fixture
def repo(tmp_path):
    """造一个"健康"的仓库布局:venv python + data 目录都在。"""
    r = tmp_path / "repo"
    (r / ".venv" / "bin").mkdir(parents=True)
    (r / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    (r / "data").mkdir()
    return r


class FakeRunner:
    """记录所有命令;对 git rev-parse 依次返回预设 HEAD,其它返回 (0, '')。"""
    def __init__(self, revs=()):
        self.revs = list(revs)
        self.calls: list[list[str]] = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if "rev-parse" in cmd:
            return 0, (self.revs.pop(0) if self.revs else "HEAD0") + "\n"
        return 0, ""

    def ran(self, *subs) -> bool:
        return any(all(s in " ".join(c) for s in subs) for c in self.calls)


# —— 环境检查 ——
def test_check_env_ok(repo):
    assert check_env(RemoteConfig(repo_dir=str(repo)), GOOD_ENV) == []


def test_check_env_missing_env_not_fixable(repo):
    probs = check_env(RemoteConfig(repo_dir=str(repo)), {"STORE_BACKEND": "db"})
    keys = {p.key for p in probs}
    assert "env:SYNC_INGEST_TOKEN" in keys and "env:SYNC_SIGNING_KEY" in keys
    assert all(not p.fixable for p in probs)          # 缺密钥需人工,不可自愈


def test_check_env_wrong_store_backend(repo):
    probs = check_env(RemoteConfig(repo_dir=str(repo)), {**GOOD_ENV, "STORE_BACKEND": "file"})
    assert any(p.key == "env:STORE_BACKEND" for p in probs)


def test_check_env_missing_repo(tmp_path):
    probs = check_env(RemoteConfig(repo_dir=str(tmp_path / "nope")), GOOD_ENV)
    assert probs and probs[0].key == "repo" and not probs[0].fixable


def test_check_env_db_dir_is_fixable(tmp_path):
    r = tmp_path / "repo"
    (r / ".venv" / "bin").mkdir(parents=True)
    (r / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    # 不建 data 目录 → 应报可自愈问题 + 带 mkdir 修复命令
    probs = check_env(RemoteConfig(repo_dir=str(r)), GOOD_ENV)
    db = [p for p in probs if p.key == "db_dir"]
    assert db and db[0].fixable and db[0].fix[0] == "mkdir"


# —— 命令构造 ——
def test_build_restart_systemd():
    assert build_restart_cmd(Service("stock-web", 8801), "systemd") == \
        ["sudo", "systemctl", "restart", "stock-web"]


def test_build_restart_nohup_needs_start_cmd():
    cmd = build_restart_cmd(Service("stock-web", 8801, "uvicorn web.app:app"), "nohup")
    assert cmd[0] == "bash" and "nohup uvicorn web.app:app" in cmd[2]
    with pytest.raises(ValueError):
        build_restart_cmd(Service("x", 1), "nohup")       # 缺 start_cmd


def test_build_restart_unknown_mode():
    with pytest.raises(ValueError):
        build_restart_cmd(Service("x", 1), "weird")


def test_build_rollback():
    assert build_rollback_cmd("/srv/app", "abc123") == \
        ["git", "-C", "/srv/app", "reset", "--hard", "abc123"]


# —— 主编排 ——
def test_run_update_changed_installs_and_restarts(repo):
    runner = FakeRunner(revs=["BEFORE", "AFTER"])       # 有变更
    res = run_update(RemoteConfig(repo_dir=str(repo)), runner=runner,
                     health_check=lambda svc: True, env=GOOD_ENV)
    assert res.ok and res.changed and res.before == "BEFORE" and res.after == "AFTER"
    assert runner.ran("pip", "install")                 # 变更→装依赖
    assert runner.ran("systemctl", "restart", "stock-web")
    assert runner.ran("systemctl", "restart", "stock-ingest")


def test_run_update_no_change_does_nothing(repo):
    runner = FakeRunner(revs=["SAME", "SAME"])          # 无变更
    res = run_update(RemoteConfig(repo_dir=str(repo)), runner=runner,
                     health_check=lambda svc: True, env=GOOD_ENV)
    assert res.ok and res.changed is False and res.step == "nochange"
    assert not runner.ran("pip", "install")             # 无变更→不装依赖
    assert not runner.ran("systemctl", "restart")       # 无变更→不重启(等下次检测)


def test_run_update_env_blocker_aborts_before_git(repo):
    runner = FakeRunner(revs=["X", "Y"])
    res = run_update(RemoteConfig(repo_dir=str(repo)), runner=runner,
                     health_check=lambda svc: True, env={"STORE_BACKEND": "db"})  # 缺密钥
    assert not res.ok and res.step == "env" and res.problems
    assert not runner.ran("fetch")                      # 环境不过,不动 git


def test_run_update_restart_fail_rolls_back(repo):
    runner = FakeRunner(revs=["BEFORE", "AFTER"])
    res = run_update(RemoteConfig(repo_dir=str(repo)), runner=runner,
                     health_check=lambda svc: False, env=GOOD_ENV)   # 重启后不健康
    assert not res.ok and res.step == "restart"
    assert res.rolled_back_to == "BEFORE" and "回滚" in res.alert
    assert runner.ran("reset", "--hard", "BEFORE")      # 回滚到更新前


def test_run_update_db_dir_self_healed(tmp_path):
    r = tmp_path / "repo"
    (r / ".venv" / "bin").mkdir(parents=True)
    (r / ".venv" / "bin" / "python").write_text("", encoding="utf-8")   # 无 data 目录
    runner = FakeRunner(revs=["A", "A"])
    res = run_update(RemoteConfig(repo_dir=str(r)), runner=runner,
                     health_check=lambda svc: True, env=GOOD_ENV)
    assert res.ok                                       # db 目录被自愈,不再是 blocker
    assert runner.ran("mkdir", "-p")


# —— 参数解析 ——
def test_parse_args_defaults():
    a = parse_args([])
    assert a.branch == "main" and a.mode == "systemd" and a.dry_run is False


def test_parse_args_overrides():
    a = parse_args(["--repo-dir", "/x", "--branch", "dev", "--mode", "nohup", "--dry-run"])
    assert a.repo_dir == "/x" and a.branch == "dev" and a.mode == "nohup" and a.dry_run is True


# —— 可配置服务 / 必需 env(适配"只跑展示端、未部署 ingest"的机器)——
def test_parse_services():
    svcs = parse_services("stock-web:8801, stock-ingest:8802")
    assert svcs == (Service("stock-web", 8801), Service("stock-ingest", 8802))
    assert parse_services("stock-web:8801") == (Service("stock-web", 8801),)
    assert parse_services("") == ()


def test_run_update_post_update_runs_only_when_changed(repo):
    post = ("/x/.venv/bin/python3", "-m", "tools.sync.import_to_db")
    cfg = RemoteConfig(repo_dir=str(repo), services=(Service("stock-web", 8801),),
                       required_env=("STORE_BACKEND",), post_update=post)
    # 有变更 → 更新后步骤(导入 DB)执行,且在重启之前
    r1 = FakeRunner(revs=["A", "B"])
    run_update(cfg, runner=r1, health_check=lambda svc: True, env={"STORE_BACKEND": "db"})
    assert r1.ran("tools.sync.import_to_db")
    # 无变更 → 更新后步骤不执行
    r2 = FakeRunner(revs=["A", "A"])
    run_update(cfg, runner=r2, health_check=lambda svc: True, env={"STORE_BACKEND": "db"})
    assert not r2.ran("tools.sync.import_to_db")


def test_run_update_a_phase_only_no_ingest(repo):
    """只跑 stock-web、只需 STORE_BACKEND 的机器:缺 sync 密钥不应阻塞,且只重启 web。"""
    cfg = RemoteConfig(repo_dir=str(repo), services=(Service("stock-web", 8801),),
                       required_env=("STORE_BACKEND",))
    runner = FakeRunner(revs=["BEFORE", "AFTER"])       # 有变更
    res = run_update(cfg, runner=runner, health_check=lambda svc: True,
                     env={"STORE_BACKEND": "db"})       # 无 SYNC_* 也 OK
    assert res.ok and res.changed
    assert runner.ran("systemctl", "restart", "stock-web")
    assert not runner.ran("systemctl", "restart", "stock-ingest")   # 不碰未部署的 ingest
