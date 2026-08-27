"""自选名单合并裁决纯函数单测(tools.sync.pool_merge.plan_digestion)。

锁死"为什么改"的语义(约法6),防未来重写误删规则:
  · 并集加、重复加跳过(幂等)
  · remove 更晚于本地 add → 执行删除
  · remove 更早、被更晚 add 压制 → 忽略(防"本地刚加、远端旧删"误删)
  · remove 不在池 → 跳过
  · 未知 op → 裁决完毕不执行
**核心不变式**:每条 pending 恰被裁决一次 → to_add ∪ to_remove ∪ noop 的 id
覆盖全部输入行(无遗漏、无重复)→ 保证消化后无残留被无限重拉。
"""
from tools.sync.pool_merge import DigestionPlan, plan_digestion, local_index_from_pool


def _row(rid, code, op, req, market="A", **extra):
    return {"id": rid, "code": code, "op": op, "requested_at": req, "market": market, **extra}


def _assert_covers_all(plan: DigestionPlan, rows):
    """不变式:所有输入行的 id 恰好被裁决一次(无遗漏、无重复)。"""
    got = ([r["id"] for r in plan.to_add]
           + [r["id"] for r in plan.to_remove]
           + list(plan.noop_consumed_ids))
    assert sorted(got) == sorted(r["id"] for r in rows)        # 覆盖全部
    assert len(got) == len(set(got))                           # 无重复


def test_add_new_goes_to_to_add():
    rows = [_row(1, "600000", "add", "2026-08-27T09:00:00+08:00")]
    plan = plan_digestion(rows, {})
    assert [r["id"] for r in plan.to_add] == [1]
    assert plan.to_remove == [] and plan.noop_consumed_ids == []
    _assert_covers_all(plan, rows)


def test_add_existing_is_noop_idempotent():
    rows = [_row(1, "600000", "add", "2026-08-27T09:00:00+08:00")]
    idx = {("600000", "A"): "2026-08-20T00:00:00+08:00"}       # 已在池
    plan = plan_digestion(rows, idx)
    assert plan.to_add == []
    assert plan.noop_consumed_ids == [1]                       # 幂等跳过
    _assert_covers_all(plan, rows)


def test_add_dup_within_batch_second_is_noop():
    rows = [_row(1, "600000", "add", "2026-08-27T09:00:00+08:00"),
            _row(2, "600000", "add", "2026-08-27T09:05:00+08:00")]
    plan = plan_digestion(rows, {})
    assert [r["id"] for r in plan.to_add] == [1]               # 首个入 to_add
    assert plan.noop_consumed_ids == [2]                       # 同批重复被记入 index 后跳过
    _assert_covers_all(plan, rows)


def test_remove_newer_than_added_executes():
    rows = [_row(1, "600000", "remove", "2026-08-27T10:00:00+08:00")]
    idx = {("600000", "A"): "2026-08-20T00:00:00+08:00"}       # 本地更早加的
    plan = plan_digestion(rows, idx)
    assert [r["id"] for r in plan.to_remove] == [1]            # 删票晚于加票 → 执行
    _assert_covers_all(plan, rows)


def test_remove_older_suppressed_by_local_add():
    rows = [_row(1, "600000", "remove", "2026-08-19T10:00:00+08:00")]
    idx = {("600000", "A"): "2026-08-20T00:00:00+08:00"}       # 本地更晚才加
    plan = plan_digestion(rows, idx)
    assert plan.to_remove == []
    assert plan.noop_consumed_ids == [1]                       # 旧删被更晚 add 压制 → 忽略
    _assert_covers_all(plan, rows)


def test_remove_absent_is_noop():
    rows = [_row(1, "600000", "remove", "2026-08-27T10:00:00+08:00")]
    plan = plan_digestion(rows, {})                            # 池里没有
    assert plan.to_remove == []
    assert plan.noop_consumed_ids == [1]                       # 已不在池,无需删
    _assert_covers_all(plan, rows)


def test_remove_old_stock_empty_added_at_executes():
    """老票 added_at="" 视为"很早",远端 remove 可正常裁决(req > "" 恒真)。"""
    rows = [_row(1, "600000", "remove", "2026-08-27T10:00:00+08:00")]
    idx = {("600000", "A"): ""}
    plan = plan_digestion(rows, idx)
    assert [r["id"] for r in plan.to_remove] == [1]
    _assert_covers_all(plan, rows)


def test_add_then_remove_later_removes():
    """同批:先 add(记入 index)再更晚 remove → 删除(remove 晚于 add)。"""
    rows = [_row(1, "600000", "add", "2026-08-27T09:00:00+08:00"),
            _row(2, "600000", "remove", "2026-08-27T10:00:00+08:00")]
    plan = plan_digestion(rows, {})
    assert [r["id"] for r in plan.to_add] == [1]
    assert [r["id"] for r in plan.to_remove] == [2]
    _assert_covers_all(plan, rows)


def test_unknown_op_is_noop():
    rows = [_row(1, "600000", "weird", "2026-08-27T09:00:00+08:00")]
    plan = plan_digestion(rows, {})
    assert plan.to_add == [] and plan.to_remove == []
    assert plan.noop_consumed_ids == [1]                       # 未知 op 裁决完毕不执行
    _assert_covers_all(plan, rows)


def test_market_distinguishes_keys():
    """同 code 不同 market 视为不同票,不相互幂等。"""
    rows = [_row(1, "00700", "add", "2026-08-27T09:00:00+08:00", market="HK")]
    idx = {("00700", "A"): "2026-08-20T00:00:00+08:00"}        # A 股有,港股没有
    plan = plan_digestion(rows, idx)
    assert [r["id"] for r in plan.to_add] == [1]               # HK 是新票
    _assert_covers_all(plan, rows)


def test_does_not_mutate_input_index():
    rows = [_row(1, "600000", "add", "2026-08-27T09:00:00+08:00")]
    idx = {}
    plan_digestion(rows, idx)
    assert idx == {}                                           # 入参不被修改


def test_local_index_from_pool():
    class S:
        def __init__(self, code, market="A", added_at=""):
            self.code, self.market, self.added_at = code, market, added_at
    pool = [S("600000", "A", "2026-08-20T00:00:00+08:00"), S("00700", "HK", "")]
    idx = local_index_from_pool(pool)
    assert idx == {("600000", "A"): "2026-08-20T00:00:00+08:00", ("00700", "HK"): ""}
