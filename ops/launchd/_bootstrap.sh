#!/bin/bash
# 部署卫生 ③b · 通用薄 bootstrap(供所有 com.stock.* launchd job 调用)。
# 设计:docs/计划/2026-09-10_部署卫生根治_设计.md / _实现规格.md
#
# 职责(唯一):让 launchd 每次触发时执行的 wrapper **本体**恒等于最新 origin/main,
#   与主仓工作树是否脏彻底无关。做法:只对一个专用 **deploy worktree** 做 fetch+reset --hard
#   origin/main,再 exec 该 worktree 内的真 wrapper。**绝不碰主仓 HEAD/工作树/stash 栈/别的会话 WIP**。
#
# 引导悖论:本脚本被 plist "钉死"在一个 **repo 外固定位置**(由 ops/launchd/provision_deploy.sh 安装),
#   故 plist 入口不依赖主仓 git 状态。本脚本内容近乎不变;真要更新(罕见)= 重跑 provision(从 deploy
#   worktree=最新 origin/main 拷贝)——连 bootstrap 自身也经 deploy worktree 部署。
#
# 用法:  _bootstrap.sh <wrapper-basename.sh> [透传给真 wrapper 的参数...]
#   例:   _bootstrap.sh pull_refresh.sh
# 退出码: 3=deploy worktree 缺失(fail-loud,不静默跑旧码);4=目标 wrapper 缺失;其余=真 wrapper 的退出码。
# 定时参数(INTRADAY_SLOT/BREADTH_SLOT/PULL_FETCH/...)由 plist EnvironmentVariables 注入,exec 自动继承。
set -uo pipefail

WRAPPER="${1:-}"
if [ -z "$WRAPPER" ]; then
  echo "$(date) 致命: 未传 wrapper basename(用法: _bootstrap.sh <wrapper.sh> [args])" >&2
  exit 2
fi
shift

# deploy worktree:纯粹"最新 wrapper 本体"来源,无数据 symlink。可用 STOCK_DEPLOY_WORKTREE 覆盖。
# 由 provision_deploy.sh 以 `git worktree add --detach <此路径> origin/main` 建好。
DEPLOY_WT="${STOCK_DEPLOY_WORKTREE:-$HOME/Documents/projects/worktrees/stock_analysis/deploy}"

if [ ! -e "$DEPLOY_WT/.git" ]; then
  echo "$(date) 致命: deploy worktree 不存在: $DEPLOY_WT" >&2
  echo "         请先跑 ops/launchd/provision_deploy.sh(或 git worktree add --detach \"$DEPLOY_WT\" origin/main)" >&2
  exit 3
fi

# 只对 deploy worktree 更新——与主仓/别的 worktree/stash 栈完全隔离。
# fetch 失败(离线)不致命:用该 worktree 现有 origin/main 兜底 fail-loud 交给 reset 后的存在性检查。
git -C "$DEPLOY_WT" fetch --quiet origin || echo "$(date) 警告: git fetch origin 失败,用 deploy worktree 现有 origin/main" >&2
git -C "$DEPLOY_WT" reset --hard origin/main >/dev/null 2>&1 || echo "$(date) 警告: reset --hard origin/main 失败,用 deploy worktree 当前代码" >&2

TARGET="$DEPLOY_WT/ops/launchd/$WRAPPER"
if [ ! -f "$TARGET" ]; then
  echo "$(date) 致命: 目标 wrapper 不存在: $TARGET(basename=$WRAPPER)——拒绝静默跑旧码" >&2
  exit 4
fi

# exec 进程替换:真 wrapper 接管本进程,退出码即真 wrapper 的退出码。参数透传(通常为空,定时参数走 env)。
exec /bin/bash "$TARGET" "$@"
