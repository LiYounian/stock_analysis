#!/bin/bash
# 本地采集机盘后闭环包装脚本(供 launchd 调用)。
# 作用:注入密钥/网关环境变量 → 跑全池流水线 + 签名上传。
# 密钥只放本机受限文件、不进 git:默认从 $HOME/.config/stock/sync.env 读(自己创建,chmod 600)。
# 仓库路径由脚本自身位置推出(ops/launchd/ 上两级),无需硬编用户名/绝对路径。
set -euo pipefail

ENV_FILE="${STOCK_SYNC_ENV:-$HOME/.config/stock/sync.env}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

# LLM_* 兜底:sync.env 未提供 LLM_API_KEY 时,让用户自己的登录 shell(默认 zsh)解析后喂进来。
# 为什么:launchd 是极简环境、读不到 ~/.zshrc;而 LLM_BASE_URL/LLM_API_KEY 常是指向其它变量的
# 别名(间接引用),只靠 sync.env 或 grep 单行取不全。这里让用户 shell 自行解析整条链,
# 密钥全程只在用户 shell 内解析、不落任何新文件、不进 git。
if [ -z "${LLM_API_KEY:-}" ]; then
  _LOGIN_SHELL="${STOCK_LOGIN_SHELL:-/bin/zsh}"
  eval "$("$_LOGIN_SHELL" -ic 'printf "export LLM_BASE_URL=%q\nexport LLM_API_KEY=%q\nexport LLM_MODEL=%q\n" "${LLM_BASE_URL:-}" "${LLM_API_KEY:-}" "${LLM_MODEL:-}"' 2>/dev/null || true)"
fi
export LLM_BASE_URL="${LLM_BASE_URL:-}" LLM_API_KEY="${LLM_API_KEY:-}" LLM_MODEL="${LLM_MODEL:-}"

# —— autopush 卫生(选项A):始终在专用常驻 worktree 里跑最新已合并 main 的代码 ——
# 背景:本项目多窗口并发,主仓 HEAD 常被别的会话 checkout 到旧 commit;若 autopush 从主仓
# 工作树跑,就会用到旧代码,导致当天新合并的策略漏产出。这里改为:只把一个专用 detached
# worktree 强制更到 origin/main 再跑,全程不碰主仓 HEAD/工作树(绝不 pull/checkout 主仓)。
MAIN_REPO="$(cd "$(dirname "$0")/../.." && pwd)"
# 专用 worktree(常驻;由部署时 `git worktree add --detach <此路径> origin/main` 建好)。
# data/raw、data/master、data/backtest_local 在该 worktree 内是指向主仓的只读 symlink(共享大文件缓存),
# data/analysis 由 worktree 自己新鲜产出。可用 STOCK_AUTOPUSH_WORKTREE 覆盖路径。
WORKTREE="${STOCK_AUTOPUSH_WORKTREE:-$HOME/Documents/projects/worktrees/stock_analysis/autopush}"

if [ ! -d "$WORKTREE/.git" ] && [ ! -f "$WORKTREE/.git" ]; then
  echo "[autopush] 致命:专用 worktree 不存在或未初始化:$WORKTREE" >&2
  echo "[autopush] 请先执行:git -C \"$MAIN_REPO\" worktree add --detach \"$WORKTREE\" origin/main" >&2
  exit 3
fi

# 拉取最新远端(对象库与主仓共享,fetch 走主仓 git 即可,不改主仓任何分支/工作树)。
git -C "$MAIN_REPO" fetch --quiet origin || echo "[autopush] 警告:git fetch origin 失败,将用本地已有 origin/main" >&2

# 选项D 保险:比对专用 worktree 当前 HEAD 与 origin/main,落后则打日志(纯观测,不改行为)。
_OLD="$(git -C "$WORKTREE" rev-parse HEAD 2>/dev/null || echo unknown)"
_NEW="$(git -C "$MAIN_REPO" rev-parse origin/main 2>/dev/null || echo unknown)"
if [ "$_OLD" != "$_NEW" ]; then
  echo "[autopush] 更新专用 worktree 到最新 main:$_OLD -> $_NEW" >&2
fi

# 只把专用 worktree 强制更到最新 main(不触碰主仓)。untracked 产物(data/analysis/<日期>/、
# data/* 的 symlink)不受 reset --hard 影响,只重置 tracked 文件到 origin/main。
git -C "$WORKTREE" reset --hard origin/main

REPO="$WORKTREE"
cd "$REPO"
# 解释器:优先 $STOCK_PYTHON(本机 conda 环境在 sync.env 里设),否则退回主仓 venv
# (worktree 内不建 venv,复用主仓 .venv;`-m ops.local_autopush` 的代码来自 cwd=worktree)。
PY="${STOCK_PYTHON:-$MAIN_REPO/.venv/bin/python3}"
exec "$PY" -m ops.local_autopush "$@"
