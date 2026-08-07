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

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
# 解释器:优先 $STOCK_PYTHON(本机 conda 环境在 sync.env 里设),否则退回 venv
PY="${STOCK_PYTHON:-$REPO/.venv/bin/python3}"
exec "$PY" -m ops.local_autopush "$@"
