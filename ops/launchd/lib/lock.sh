# shellcheck shell=bash
# 单实例锁公共库(供 pull_refresh / strong_refresh / intraday_screen 等日更 wrapper 复用)。
#
# 修复的隐患(A3,诊断-market_forecast停更 §6.2):原各 wrapper 用 `mkdir 锁 + trap EXIT rmdir`,
#   在 SIGKILL / 断电 / 强杀下 trap **不执行** → 锁目录残留 → 之后每个交易日都命中
#   "已有实例在跑,跳过"、**永久停摆直到人工 rmdir**。这是尚未触发但严重的 latent 大雷。
#
# 方案:锁目录内写持有者 PID + 启动 epoch;抢锁失败时判定持有者是否"陈旧"——
#   ① 进程已不存在(kill -0 失败),或 ② 超龄(age ≥ STOCK_LOCK_STALE_SEC,默认 6h)——
#   陈旧则清理抢占重跑;**正常运行中的活实例(进程在、未超龄)不误抢**。
#   trap 释放时再校验 owner.pid == $$,避免误删被别人抢占后重建的锁。
#
# 用法:
#   . "$REPO/ops/launchd/lib/lock.sh"
#   acquire_lock_or_exit "$LOCK" "$LOG"        # 成功=占锁+注册释放trap;有活实例=写日志 exit 0
#
# 可调 env:
#   STOCK_LOCK_STALE_SEC   持有者超龄阈值秒(默认 21600 = 6h)
#   STOCK_LOCK_MKDIR_GRACE 无 owner 文件时的建锁宽限秒(默认 30),窗口内视为活实例不抢(闭 TOCTOU 竞态)

# —— owner 文件读写 ——
_lock_write_owner() {
  local lock="$1"
  printf 'pid=%s\nstart=%s\nhost=%s\n' \
    "$$" "$(date +%s)" "$(hostname 2>/dev/null || echo '?')" \
    > "$lock/owner" 2>/dev/null || true
}

_lock_field() {   # 从 owner 文件读某字段值(不存在则空)
  local lock="$1" key="$2"
  [ -f "$lock/owner" ] || return 0
  sed -n "s/^${key}=//p" "$lock/owner" 2>/dev/null | head -1
}

_lock_dir_age() {   # 锁目录已存在秒数(mtime);取不到则空
  local lock="$1" mtime now
  # macOS: stat -f %m;Linux: stat -c %Y
  mtime="$(stat -f %m "$lock" 2>/dev/null || stat -c %Y "$lock" 2>/dev/null || echo '')"
  [ -n "$mtime" ] || return 0
  now="$(date +%s)"
  echo $(( now - mtime ))
}

# —— 陈旧判定:返回 0=陈旧可抢占,1=活实例(勿抢) ——
_lock_is_stale() {
  local lock="$1" stale="$2"
  local pid start now age ldir_age
  pid="$(_lock_field "$lock" pid)"
  start="$(_lock_field "$lock" start)"

  if [ -z "$pid" ]; then
    # 无 owner 文件:可能是①老格式残留锁(隐患源),或②另一实例刚 mkdir 尚未写 owner(微秒级竞态)。
    # 用锁目录年龄区分:老残留 age 必大;刚建的 age 极小 → 宽限期内视为活实例不抢(闭 TOCTOU)。
    ldir_age="$(_lock_dir_age "$lock")"
    if [ -n "$ldir_age" ] && [ "$ldir_age" -lt "${STOCK_LOCK_MKDIR_GRACE:-30}" ]; then
      return 1
    fi
    return 0
  fi

  # 进程已不存在 → 陈旧(SIGKILL 下 trap 没跑、锁残留的典型态)
  kill -0 "$pid" 2>/dev/null || return 0

  # 进程还在但超龄 → 陈旧(可能挂死;阈值内不动活实例)
  if [ -n "$start" ]; then
    now="$(date +%s)"
    age=$(( now - start ))
    [ "$age" -ge "$stale" ] && return 0
  fi
  return 1
}

# —— 释放:只清理自己持有的锁 ——
_lock_release() {
  local lock="$1" pid
  pid="$(_lock_field "$lock" pid)"
  if [ -z "$pid" ] || [ "$pid" = "$$" ]; then
    rm -rf "$lock" 2>/dev/null
  fi
}

_lock_arm_trap() {
  local lock="$1"
  # 单引号包 $lock、双引号求值展开路径,$$ 留到 trap 触发时求值(始终=本 shell)
  trap "_lock_release '$lock'" EXIT
}

# —— 主入口 ——
# acquire_lock_or_exit <lockdir> <logfile> [stale_sec]
acquire_lock_or_exit() {
  local lock="$1" log="$2"
  local stale="${3:-${STOCK_LOCK_STALE_SEC:-21600}}"

  if mkdir "$lock" 2>/dev/null; then
    _lock_write_owner "$lock"
    _lock_arm_trap "$lock"
    return 0
  fi

  # 锁已存在:陈旧则清理抢占
  if _lock_is_stale "$lock" "$stale"; then
    echo "$(date) 检测到陈旧锁(持有者死亡或超龄 ≥${stale}s),清理抢占:$lock" >> "$log"
    rm -rf "$lock" 2>/dev/null
    if mkdir "$lock" 2>/dev/null; then
      _lock_write_owner "$lock"
      _lock_arm_trap "$lock"
      return 0
    fi
    # 抢占竞态失败(另一实例同刻抢到)→ 按有活实例处理
  fi

  echo "$(date) 已有实例在跑,跳过" >> "$log"
  exit 0
}
