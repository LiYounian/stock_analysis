"""展示端远端自动更新 / 自愈编排(可单测:命令执行器 runner + 健康检查 health 均可注入)。

一轮 run_update:
  ① 环境检查+自愈:仓库/venv/必需 env(STORE_BACKEND=db、密钥变量名)/DB 目录/端口配置;
     能补的补(建 DB 目录),补不了的(缺 venv、缺密钥 env)清晰报错退出。
  ② 更新:git fetch + ff-only 合并最新;有变更才 pip 装依赖。
  ③ 重启+验证:重启 web(8801)+ingest(8802);任一重启后健康检查不过 → 回滚到更新前 commit
     (需要则重装依赖)、重启、返回告警。
幂等、可重复跑;远端专有值(路径/分支/端口/密钥变量)走 RemoteConfig 参数或 env,不硬编。
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("ops.remote_update")


@dataclass(frozen=True)
class Service:
    name: str            # systemd unit 名
    port: int            # 健康检查端口
    start_cmd: str = ""  # nohup 兜底时的启动命令


@dataclass
class RemoteConfig:
    repo_dir: str
    branch: str = "main"
    python: str = ".venv/bin/python"          # 相对 repo_dir 的 venv python
    mode: str = "systemd"                      # systemd | nohup
    services: tuple[Service, ...] = (
        Service("stock-web", 8801),
        Service("stock-ingest", 8802),
    )
    required_env: tuple[str, ...] = ("STORE_BACKEND", "SYNC_INGEST_TOKEN", "SYNC_SIGNING_KEY")
    db_path: str = "data/app.db"              # 相对 repo_dir
    post_update: tuple[str, ...] = ()        # 有变更时、重启前执行的命令(如把 git 带来的产物导入 DB)

    def python_path(self) -> Path:
        p = Path(self.python)
        return p if p.is_absolute() else Path(self.repo_dir) / p


@dataclass
class Problem:
    key: str
    fixable: bool
    message: str
    fix: list[str] | None = None      # 可自愈时的修复命令


@dataclass
class Result:
    ok: bool
    step: str = ""
    changed: bool = False
    before: str = ""
    after: str = ""
    rolled_back_to: str = ""
    alert: str = ""
    problems: list[str] = field(default_factory=list)


# ————————————————————————————————————————————————
# 环境检查 + 自愈
# ————————————————————————————————————————————————
def check_env(cfg: RemoteConfig, env: dict | None = None) -> list[Problem]:
    """返回发现的问题列表(空=通过)。fixable 者带 fix 命令,由调用方执行自愈。"""
    env = os.environ if env is None else env
    probs: list[Problem] = []
    repo = Path(cfg.repo_dir)

    if not repo.exists():
        probs.append(Problem("repo", False, f"仓库目录不存在: {repo}"))
        return probs                                   # 后续检查都依赖仓库存在

    if not cfg.python_path().exists():
        probs.append(Problem("venv", False,
                             f"venv python 不存在: {cfg.python_path()};请先 python3.11 -m venv .venv"))

    for name in cfg.required_env:
        if not env.get(name):
            probs.append(Problem(f"env:{name}", False, f"缺必需环境变量 {name}"))
    if env.get("STORE_BACKEND") and env.get("STORE_BACKEND") != "db":
        probs.append(Problem("env:STORE_BACKEND", False,
                             f"展示端应 STORE_BACKEND=db,当前={env.get('STORE_BACKEND')!r}"))

    db_dir = (repo / cfg.db_path).parent
    if not db_dir.exists():                             # DB 目录可自愈:建目录即可
        probs.append(Problem("db_dir", True, f"DB 目录不存在: {db_dir}",
                             fix=["mkdir", "-p", str(db_dir)]))
    return probs


# ————————————————————————————————————————————————
# 命令构造(可单测)
# ————————————————————————————————————————————————
def build_restart_cmd(svc: Service, mode: str) -> list[str]:
    """构造重启命令:systemd 用 systemctl restart;nohup 兜底 pkill+nohup 重拉。"""
    if mode == "systemd":
        return ["sudo", "systemctl", "restart", svc.name]
    if mode == "nohup":
        if not svc.start_cmd:
            raise ValueError(f"nohup 模式需为 {svc.name} 配 start_cmd")
        return ["bash", "-lc",
                f"pkill -f {svc.name!r} || true; nohup {svc.start_cmd} "
                f">logs/{svc.name}.log 2>&1 &"]
    raise ValueError(f"未知 mode: {mode!r}(支持 systemd|nohup)")


def build_rollback_cmd(repo_dir: str, to_commit: str) -> list[str]:
    return ["git", "-C", repo_dir, "reset", "--hard", to_commit]


# ————————————————————————————————————————————————
# 主编排(runner/health 注入 → 可单测)
# ————————————————————————————————————————————————
def _rev(cfg: RemoteConfig, runner) -> str:
    rc, out = runner(["git", "-C", cfg.repo_dir, "rev-parse", "HEAD"])
    return out.strip()


def _install_deps(cfg: RemoteConfig, runner) -> None:
    runner([str(cfg.python_path()), "-m", "pip", "install", "-q", "-r",
            str(Path(cfg.repo_dir) / "requirements.txt")])


def _restart_all(cfg: RemoteConfig, runner) -> None:
    for svc in cfg.services:
        runner(build_restart_cmd(svc, cfg.mode))


def run_update(cfg: RemoteConfig, *, runner, health_check, env: dict | None = None) -> Result:
    """执行一轮自动更新+自愈。runner(cmd)->(rc,out);health_check(svc)->bool。"""
    env = os.environ if env is None else env

    # ① 环境检查 + 自愈
    probs = check_env(cfg, env)
    for p in probs:
        if p.fixable and p.fix:
            logger.info("自愈:%s", p.message)
            runner(p.fix)
    blockers = [p for p in check_env(cfg, env) if not p.fixable]
    if blockers:
        for b in blockers:
            logger.error("环境不满足(需人工):%s", b.message)
        return Result(ok=False, step="env", problems=[b.message for b in blockers])

    # ② 更新:码仓没变 → 直接返回,不重启(定时轮询只在有变更时动手)
    before = _rev(cfg, runner)
    runner(["git", "-C", cfg.repo_dir, "fetch", "origin"])
    runner(["git", "-C", cfg.repo_dir, "merge", "--ff-only", f"origin/{cfg.branch}"])
    after = _rev(cfg, runner)
    changed = before != after
    if not changed:
        return Result(ok=True, step="nochange", changed=False, before=before, after=after)

    # ③ 有变更:装依赖 →(更新后步骤,如导入 DB)→ 重启 + 健康检查(失败回滚到更新前)
    logger.info("代码/数据更新 %s → %s,安装依赖并重启", before[:8], after[:8])
    _install_deps(cfg, runner)
    if cfg.post_update:
        logger.info("更新后步骤:%s", " ".join(cfg.post_update))
        runner(list(cfg.post_update))
    for svc in cfg.services:
        runner(build_restart_cmd(svc, cfg.mode))
        if not health_check(svc):
            logger.error("%s 重启后不健康,回滚到 %s", svc.name, before[:8])
            runner(build_rollback_cmd(cfg.repo_dir, before))
            _install_deps(cfg, runner)
            _restart_all(cfg, runner)
            return Result(ok=False, step="restart", before=before, after=after,
                          rolled_back_to=before,
                          alert=f"{svc.name} 重启失败,已回滚到 {before[:8]}")

    return Result(ok=True, step="done", changed=True, before=before, after=after)


# ————————————————————————————————————————————————
# 真实执行器 + 健康检查 + CLI
# ————————————————————————————————————————————————
def subprocess_runner(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def http_health(svc: Service) -> bool:
    """本机健康检查:/health 或 / 返回 <500 视为存活。"""
    import urllib.request
    for path in ("/health", "/"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{svc.port}{path}", timeout=5) as r:
                if r.status < 500:
                    return True
        except Exception:
            continue
    return False


def parse_services(spec: str) -> tuple[Service, ...]:
    """解析 "name:port,name2:port2" → Service 元组。端口缺省 0(仅按名重启,不健康检查端口)。"""
    out: list[Service] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, port = part.partition(":")
        out.append(Service(name.strip(), int(port) if port.strip() else 0))
    return tuple(out)


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="展示端远端自动更新+自愈(拉取最新→有变更装依赖+重启→失败回滚)")
    ap.add_argument("--repo-dir", default=os.getenv("REMOTE_REPO_DIR", "/srv/stock_analysis"))
    ap.add_argument("--branch", default=os.getenv("REMOTE_BRANCH", "main"))
    ap.add_argument("--python", default=os.getenv("REMOTE_PYTHON", ".venv/bin/python"))
    ap.add_argument("--mode", default=os.getenv("REMOTE_MODE", "systemd"), choices=["systemd", "nohup"])
    # 要重启+健康检查的服务(逗号分隔 name:port);只跑展示端时设 "stock-web:8801"
    ap.add_argument("--services", default=os.getenv("REMOTE_SERVICES", "stock-web:8801,stock-ingest:8802"))
    # 必需环境变量名(逗号分隔);未部署 ingest 的机器设 "STORE_BACKEND" 即可
    ap.add_argument("--required-env", default=os.getenv("REMOTE_REQUIRED_ENV",
                    "STORE_BACKEND,SYNC_INGEST_TOKEN,SYNC_SIGNING_KEY"))
    # 有变更时、重启前执行的命令(展示端:把 git 带来的产物导入 DB)。整条命令字符串,shlex 拆分
    ap.add_argument("--post-update", default=os.getenv("REMOTE_POST_UPDATE", ""))
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的命令,不真正执行")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    import shlex
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    required = tuple(e.strip() for e in args.required_env.split(",") if e.strip())
    cfg = RemoteConfig(repo_dir=args.repo_dir, branch=args.branch, python=args.python,
                       mode=args.mode, services=parse_services(args.services), required_env=required,
                       post_update=tuple(shlex.split(args.post_update)) if args.post_update else ())

    if args.dry_run:
        def runner(cmd):
            logger.info("[dry-run] %s", " ".join(cmd))
            return 0, ""
        res = run_update(cfg, runner=runner, health_check=lambda svc: True)
    else:
        res = run_update(cfg, runner=subprocess_runner, health_check=http_health)

    if res.ok:
        logger.info("更新完成:changed=%s %s→%s", res.changed, res.before[:8], res.after[:8])
        return 0
    logger.error("更新未成功(step=%s):%s %s", res.step, res.alert, "; ".join(res.problems))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
