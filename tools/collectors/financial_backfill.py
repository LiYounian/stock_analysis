"""全A 财报三大表**增量回填**编排(把 collectors.financial 从"仅 picks"扩到全A)。

背景:日常盘后闭环只对 `news_subset`(自选 ∪ 每策略 topN,约 175 票)采财报,导致离线全A
策略0(screen_council)绝大多数票无 as_of 财报块 → 财报质地专家恒弃权、红旗判定在全A排序不生效。
本模块提供一个**可后台长跑、幂等、断点续采、增量新鲜度门控**的回填入口,让全A票也拿到财报 raw。

设计(约法5:先框架后实现;不改 collectors.financial 已有逻辑,只做编排):
  1. 票池:codes=None → universe.universe_codes(全A,默认排除北交所);或显式传 codes。
  2. **增量 / 幂等 / 断点续采**(三合一,靠新鲜度门控,无需独立 checkpoint 文件):
     对每票用 store.is_stale('financial_report', code, max_age_days) 判断——
       · 从未采 → 视为陈旧 → 纳入 need;
       · 已采且距上次采集 age ≤ max_age_days → 跳过(季报季度级更新,窗内不重采,省接口)。
     被中断后重跑:本轮已成功采的票 meta 新鲜 → 自动跳过,天然续采;失败票无 raw → 下轮重试。
  3. 采集:剩余 need 交 collectors.financial.fetch_financial(need, as_of) 批量跑
     (自带 0.5s sleep / 45s 硬超时 / 单票降级不炸整批);分块推进 + 进度日志,便于长跑观测。
  4. 落盘:store.put_raw('financial_report', ...) 落到本轮活动日期分区(set_active_date)。

**防未来函数(红线)**:采集**不**按 as_of 截断——三大表全期数连同**披露日**(NOTICE_DATE)一并落盘;
可见性过滤在消费侧(analysis.financial.analyzer 强制 disclosure_date ≤ as_of)。故回填当天采到的
最新财报,在历史回测某个更早 as_of 复算时,若其披露日 > 该 as_of 会被自动过滤,不产生未来函数。
as_of 参数仅作写入日期/元数据锚,不改采集内容。

规模/耗时(工程估算,非承诺):全A ≈ 5000 票 × 3 表,每表 sleep 0.5s ⇒ 纯 sleep ≈ 5000×3×0.5 ≈
125 分钟,叠加网络往返(每表 0.3~1s)总耗时约 3~6 小时;首采一次,之后每季度增量只重采到期票。
接口为 akshare/东财 per-stock,无官方配额,但高并发易被限流 → 本模块串行 + 礼貌 sleep,稳为先。

⚠️ 非投资建议。全量回填是重操作,由统筹决定何时后台跑(见 CLI --run)。
"""
from __future__ import annotations

import logging
import time

from tools.collectors import financial as fin
from tools.collectors import universe
from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.financial_backfill")

# —— 增量门控默认:季报季度级更新,窗内不重采(可 CLI/参数覆盖)——
DEFAULT_MAX_AGE_DAYS = 45.0
# —— 分块推进:每块跑完落一次进度日志(长跑可观测;块内仍是 fetch_financial 串行)——
DEFAULT_CHUNK = 200


def _today() -> str:
    import datetime as _dt
    return _dt.date.today().strftime("%Y-%m-%d")


def select_pending(codes: list[str], max_age_days: float = DEFAULT_MAX_AGE_DAYS,
                   force: bool = False) -> list[str]:
    """从 codes 里挑出**需要采**的票(增量门控)。

    force=True → 全部纳入(忽略新鲜度);否则 store.is_stale(未采 / 超 max_age_days)才纳入。
    保序去重(同票只留一次)。纯函数式筛选,不触网、不落盘。
    """
    seen: set[str] = set()
    out: list[str] = []
    for c in codes:
        c = str(c).zfill(6)
        if c in seen:
            continue
        seen.add(c)
        if force or store.is_stale("financial_report", c, max_age_days):
            out.append(c)
    return out


def backfill_financial(codes: list[str] | None = None, as_of: str | None = None,
                       max_age_days: float = DEFAULT_MAX_AGE_DAYS, limit: int | None = None,
                       force: bool = False, exclude_bj: bool = True,
                       chunk: int = DEFAULT_CHUNK, dry_run: bool = False) -> dict:
    """全A(或指定 codes)财报三大表增量回填。返回统计 dict。

    Args:
        codes: 目标票池;None → universe.universe_codes(全A,exclude_bj 排除北交所)。
        as_of: 写入活动日期 + fetch 元数据锚(缺省今天)。采集内容不按 as_of 截断(防未来函数在消费侧)。
        max_age_days: 增量门控——已采且新鲜(age≤此)的票跳过;None/负 → 视为总是重采。
        limit: 仅取票池前 N 只(联调/试跑用);None=全量。
        force: 忽略新鲜度,全部重采(慎用,重操作)。
        exclude_bj: codes=None 时,全A 是否排除北交所(默认 True,与其余管线一致)。
        chunk: 分块大小(每块跑完落进度日志)。
        dry_run: 只算 need/skipped 规模、不实际采集(估算耗时用)。
    Returns:
        {as_of, universe, fresh_skipped, need, ok, failed, dry_run, mode}。
    """
    as_of = as_of or _today()
    store.set_active_date(as_of)
    if codes is None:
        codes = universe.universe_codes(limit=limit, exclude_bj=exclude_bj)
        logger.info("回填票池:全A %d 只(排除北交所=%s%s)", len(codes), exclude_bj,
                    f",limit={limit}" if limit else "")
    else:
        codes = [str(c).zfill(6) for c in codes]
        if limit:
            codes = codes[:limit]
        logger.info("回填票池:显式 %d 只", len(codes))

    need = select_pending(codes, max_age_days=max_age_days, force=force)
    fresh_skipped = len(codes) - len(need)
    logger.info("增量门控:need=%d / 新鲜跳过=%d(max_age_days=%s,force=%s)",
                len(need), fresh_skipped, max_age_days, force)

    stat = {"as_of": as_of, "universe": len(codes), "fresh_skipped": fresh_skipped,
            "need": len(need), "ok": 0, "failed": 0, "dry_run": dry_run,
            "mode": "force" if force else "incremental"}
    if dry_run:
        # 纯 sleep 下限估算(每票 3 表 × sleep;网络往返另计)
        est_min = len(need) * 3 * settings.FETCH_SLEEP_SEC / 60.0
        stat["est_sleep_minutes"] = round(est_min, 1)
        logger.info("dry_run:需采 %d 只,纯 sleep 下限≈%.1f 分钟(网络往返另计)", len(need), est_min)
        return stat

    t0 = time.time()
    for i in range(0, len(need), max(1, chunk)):
        part = need[i:i + max(1, chunk)]
        out = fin.fetch_financial(part, as_of=as_of)      # 自带 sleep/超时/降级;返回 {code: payload}
        ok = len(out)
        stat["ok"] += ok
        stat["failed"] += (len(part) - ok)
        logger.info("回填进度:%d/%d(本块 ok=%d/%d,累计 ok=%d failed=%d,用时 %.0fs)",
                    min(i + len(part), len(need)), len(need), ok, len(part),
                    stat["ok"], stat["failed"], time.time() - t0)
    logger.info("全A财报回填完成:need=%d ok=%d failed=%d 新鲜跳过=%d(as_of=%s,用时 %.0fs)",
                len(need), stat["ok"], stat["failed"], fresh_skipped, as_of, time.time() - t0)
    return stat


def _main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="全A 财报三大表增量回填(可后台长跑,幂等/断点续采)")
    ap.add_argument("--universe", type=int, metavar="N", help="全A前 N 只(试跑;不传=全量)")
    ap.add_argument("--codes", help="逗号分隔的指定代码(优先于 --universe)")
    ap.add_argument("--date", help="as_of / 写入日期 YYYY-MM-DD(默认今天)")
    ap.add_argument("--max-age-days", type=float, default=DEFAULT_MAX_AGE_DAYS,
                    help=f"增量门控:新鲜票跳过阈值天数(默认 {DEFAULT_MAX_AGE_DAYS})")
    ap.add_argument("--chunk", type=int, default=DEFAULT_CHUNK, help="分块大小(进度日志粒度)")
    ap.add_argument("--force", action="store_true", help="忽略新鲜度,全部重采(重操作,慎用)")
    ap.add_argument("--include-bj", action="store_true", help="全A 纳入北交所(默认排除)")
    ap.add_argument("--dry-run", action="store_true", help="只估算需采规模/耗时,不实际采集")
    a = ap.parse_args(argv)

    codes = [c.strip() for c in a.codes.split(",") if c.strip()] if a.codes else None
    stat = backfill_financial(
        codes=codes, as_of=a.date, max_age_days=a.max_age_days, limit=a.universe,
        force=a.force, exclude_bj=not a.include_bj, chunk=a.chunk, dry_run=a.dry_run)
    logger.info("完成:%s", stat)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
