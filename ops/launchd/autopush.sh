#!/bin/bash
# 本地采集机盘后闭环包装脚本(供 launchd 调用)。
# 作用:注入密钥/网关环境变量 → 跑全池流水线 + 签名上传。
# 密钥只放本机受限文件、不进 git:默认从 $HOME/.config/stock/sync.env 读(自己创建,chmod 600)。
# 仓库路径由脚本自身位置推出(ops/launchd/ 上两级),无需硬编用户名/绝对路径。
set -euo pipefail

ENV_FILE="${STOCK_SYNC_ENV:-$HOME/.config/stock/sync.env}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
exec "$REPO/.venv/bin/python3" -m ops.local_autopush "$@"
