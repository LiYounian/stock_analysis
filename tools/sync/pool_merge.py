"""自选名单合并裁决(方案2:远端提案 pending → 本地名单)的**纯函数**层。

名单是集合语义,冲突比行内容易收敛。策略 = "两端并集 + 操作时间戳裁决删除":
  · 加(add):并集。任一端 add 的票进入最终名单;已在池中则幂等跳过。
  · 删(remove):以时间戳裁决——远端 remove 的 requested_at 晚于本地该票最近一次
    add 才执行删除;否则(被更晚的 add 压制)忽略,防"本地刚加、远端旧删记录误删"。
  · 权威:本地 config/stock_pool.json 恒为唯一真源,远端 pending 只是提案队列。

本模块**无任何 IO**(不碰 DB / 文件 / 网络),只做裁决计算,便于单测锁死语义
(约法6:测试断言锁住"为什么改",防未来重写误删规则)。IO 与执行由
ops/consume_pool_pending.py 承担。

关键不变式:每条 pending 恰被裁决一次 —— to_add ∪ to_remove ∪ noop_consumed_ids
覆盖全部输入行,无遗漏、无重复。这保证"裁决后无残留 pending 被无限重拉"
(noop 立即可标 consumed;to_add/to_remove 待执行成功后由调用方标 consumed)。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DigestionPlan:
    """一次消化的裁决结果。

    to_add / to_remove:需要 add_and_collect / remove_and_cleanup 的 pending 行
      (调用方**仅在执行成功后**才把其 id 标 consumed;失败保留 pending 下轮重试)。
    noop_consumed_ids:无需任何动作、已裁决完毕、可**立即**标 consumed 的行 id
      (幂等重复 add / 已不在池的 remove / 被更晚 add 压制的旧 remove)。
    """
    to_add: list[dict] = field(default_factory=list)
    to_remove: list[dict] = field(default_factory=list)
    noop_consumed_ids: list[int] = field(default_factory=list)


def _key(row: dict) -> tuple[str, str]:
    """(code, market) 组合键;market 缺省 A、大写归一,与 stock_pool 判重口径一致。"""
    return ((row.get("code") or "").strip(),
            ((row.get("market") or "A").strip().upper() or "A"))


def plan_digestion(pending_rows: list[dict],
                   local_index: dict[tuple[str, str], str]) -> DigestionPlan:
    """裁决:给定远端 pending 行(须按 requested_at 升序)+ 本地名单现状,产出执行计划。

    参数
      pending_rows: pool_pending_store.list_pending() 的结果(升序)。每行含
                    id/code/name/industry/sector/market/op/requested_at 等。
      local_index:  {(code, market): added_at_iso} —— 本地池现状。added_at 为空串
                    表示"很早"(远端 remove 可正常裁决:老票早于时间戳跟踪期)。
                    本函数**不修改**入参(内部拷贝一份推进)。

    裁决规则(按 requested_at 升序逐条,让更晚的操作压制更早的):
      op=add:
        · key 已在 index → noop_consumed(幂等,已存在)
        · 否则 → to_add;并把 key 记入 index(requested_at)使后续更早的 remove 被压制
      op=remove:
        · key 不在 index → noop_consumed(已不在池,无需删)
        · key 在且 requested_at > index[key] → to_remove;从 index 删 key
        · key 在但 requested_at <= index[key] → noop_consumed(被更晚 add 压制,防误删)
      其它 op → noop_consumed(未知操作,不执行但裁决完毕,避免无限重拉)
    """
    index = dict(local_index)   # 不改入参
    plan = DigestionPlan()
    for row in pending_rows:
        rid = row.get("id")
        op = (row.get("op") or "").strip().lower()
        key = _key(row)
        req = str(row.get("requested_at") or "")
        if op == "add":
            if key in index:
                plan.noop_consumed_ids.append(rid)
            else:
                plan.to_add.append(row)
                index[key] = req
        elif op == "remove":
            if key not in index:
                plan.noop_consumed_ids.append(rid)
            elif req > (index.get(key) or ""):
                plan.to_remove.append(row)
                del index[key]
            else:
                plan.noop_consumed_ids.append(rid)   # 被更晚的 add 压制,忽略删除
        else:
            plan.noop_consumed_ids.append(rid)        # 未知 op:裁决完毕,不执行
    return plan


def local_index_from_pool(pool) -> dict[tuple[str, str], str]:
    """从 stock_pool.get_pool() 的 Stock 列表构造 local_index。

    added_at 缺省 ""(向后兼容:旧 JSON 无此字段的票视为"很早")。
    """
    return {((s.code or "").strip(), (getattr(s, "market", "A") or "A").strip().upper() or "A"):
            (getattr(s, "added_at", "") or "")
            for s in pool}
