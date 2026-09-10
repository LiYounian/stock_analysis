#!/bin/bash
# 部署卫生 ③b · deploy worktree 与 bootstrap 的一次性/幂等 provisioning。
# 设计:docs/计划/2026-09-10_部署卫生根治_设计.md / _实现规格.md
#
# 做两件事(都幂等):
#   1. 建/刷新专用 **deploy worktree**(detached origin/main,纯 wrapper 本体来源,无数据 symlink)。
#   2. 从 deploy worktree 把薄 bootstrap 安装到 **repo 外固定位置**(plist 钉死处),解引导悖论。
#
# **本脚本不装载 launchd**(unload/load 是人工步骤,见 docs/参考/部署卫生_launchd装载runbook.md)。
# **只对 deploy worktree 操作,绝不碰主仓 HEAD/工作树/stash 栈。**
# 幂等:重复跑安全——worktree 已在则 fetch+reset,bootstrap 覆盖安装。
set -uo pipefail

MAIN_REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DEPLOY_WT="${STOCK_DEPLOY_WORKTREE:-$HOME/Documents/projects/worktrees/stock_analysis/deploy}"
BOOT_DIR="${STOCK_LAUNCHD_BIN:-$HOME/.local/state/stock/bin}"
BOOT_DST="$BOOT_DIR/stock-launchd-bootstrap.sh"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') provision_deploy: $*"; }

# 1) fetch(共享对象库,不碰主仓分支/工作树)
git -C "$MAIN_REPO" fetch --quiet origin || log "!! git fetch 失败,用现有 origin/main 兜底"

# 2) 建/刷新 deploy worktree
if [ -e "$DEPLOY_WT/.git" ]; then
  git -C "$DEPLOY_WT" reset --hard origin/main >/dev/null 2>&1 || { log "!! deploy worktree reset 失败: $DEPLOY_WT"; exit 3; }
  log "deploy worktree 已刷新到 origin/main: $DEPLOY_WT ($(git -C "$DEPLOY_WT" rev-parse --short HEAD))"
else
  mkdir -p "$(dirname "$DEPLOY_WT")"
  git -C "$MAIN_REPO" worktree add --detach "$DEPLOY_WT" origin/main >/dev/null 2>&1 || { log "!! deploy worktree 创建失败: $DEPLOY_WT"; exit 3; }
  log "deploy worktree 已创建: $DEPLOY_WT ($(git -C "$DEPLOY_WT" rev-parse --short HEAD))"
fi

# 3) 从 deploy worktree 安装 bootstrap 到 repo 外固定位置(plist 钉死处)。
#    从 deploy 拷(而非主仓)=拿到最新 origin/main 版本,连 bootstrap 自身也经 deploy 部署。
BOOT_SRC="$DEPLOY_WT/ops/launchd/_bootstrap.sh"
if [ ! -f "$BOOT_SRC" ]; then
  log "!! bootstrap 源不存在: $BOOT_SRC(deploy worktree 未含该文件?检查 origin/main)"; exit 4
fi
mkdir -p "$BOOT_DIR"
cp -f "$BOOT_SRC" "$BOOT_DST"
chmod +x "$BOOT_DST"
log "bootstrap 已安装: $BOOT_DST"

log "完成。下一步(人工):按 runbook launchctl unload/load 各 com.stock.* plist。"
log "  plist ProgramArguments 应指向: $BOOT_DST"
