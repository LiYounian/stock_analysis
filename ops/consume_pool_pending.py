"""本地闭环:消化远端自选提案(方案2)。oneshot,由本地 launchd/pull_refresh 编排触发。

半环编排(只调用、不重写下层):
  ① pull:经 tools.sync.pull 从远端 ingest /pull?kind=pool_pending 拉走 status=pending 提案
     → 落本地缓冲 data/sync_receipts/pool_pending.json
  ② 裁决:读缓冲 + 本地池现状(stock_pool)→ tools.sync.pool_merge.plan_digestion(纯函数)
     产出 to_add / to_remove / noop_consumed_ids
  ③ 执行:对 to_add 调 pool_service.add_and_collect(本地有 raw,采集+重建 panel 不塌);
     对 to_remove 调 remove_and_cleanup。**仅执行成功的行**才追加进 consumed_ids
     (失败保留 pending,下轮重试);noop 立即 consumed。
  ④ 回执待发:把 consumed_ids 写 data/sync_receipts/pool_ack.json,由闭环脚本随下一次
     `upload_date(pool_ack_ids=...)` 顺带回推(回执方式 ii,不新开写口)→ 远端标 consumed。

本模块只产出 consumed_ids(职责单一,便于测试),不自己发 upload——upload 由 pull_refresh.sh 编排。
名单真源恒为本地 config/stock_pool.json;远端 pending 是提案队列。

CLI:python -m ops.consume_pool_pending [--dry-run] [--url ...]
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

from tools.config import settings

logger = logging.getLogger("ops.consume_pool_pending")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _receipt_dir() -> Path:
    return settings.PROJECT_ROOT / "data" / "sync_receipts"


def _buffer_path() -> Path:
    return _receipt_dir() / "pool_pending.json"


def _ack_path() -> Path:
    return _receipt_dir() / "pool_ack.json"


def _read_buffer(path: Path) -> list[dict]:
    """读 pull 落下的提案缓冲(rows 升序)。缺失/损坏 → 空列表(视为无提案,不报错)。"""
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("rows") or []
    except (ValueError, OSError):
        logger.warning("提案缓冲损坏或不可读:%s,按空处理", path)
        return []


def _write_ack(consumed_ids: list[int], path: Path) -> None:
    """把已消化 id 写待发回执文件(原子替换),供闭环脚本随 upload 顺带回推。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps({"generated_at": _now_iso(),
                               "consumed_ids": [int(i) for i in consumed_ids]},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_ack(path: Path | None = None) -> list[int]:
    """读待发回执文件里的 consumed_ids(供 upload 编排读取)。缺失/损坏 → 空。"""
    p = path or _ack_path()
    if not p.exists():
        return []
    try:
        return [int(i) for i in (json.loads(p.read_text(encoding="utf-8")).get("consumed_ids") or [])]
    except (ValueError, OSError):
        return []


def clear_ack(path: Path | None = None) -> None:
    """上传成功回推后清空待发回执文件(避免下轮重复回推;缺失即 no-op)。"""
    p = path or _ack_path()
    try:
        p.unlink()
    except OSError:
        pass


def consume(*, url: str, token: str, key: str, key_id: str,
            dry_run: bool = False, pull_fn=None) -> dict:
    """跑一次消化。返回 {pulled, added, removed, failed, consumed_ids}(dry_run 时另含裁决预览)。

    失败项(采集/删除抛异常)保留 pending 不进 consumed_ids,下轮重试;
    noop(幂等加/已删/被压制的旧删)立即进 consumed_ids。
    """
    from tools.config import stock_pool
    from tools.sync import pool_merge
    from tools.sync import pull as pullmod
    from tools import pool_service

    # ① pull → 缓冲
    pull_fn = pull_fn or pullmod.pull
    pres = pull_fn("pool_pending", url=url, token=token, key=key, key_id=key_id)
    if not (pres or {}).get("ok"):
        logger.error("拉取 pending 失败,跳过本轮消化:%s", pres)
        return {"ok": False, "error": (pres or {}).get("error"), "pulled": 0,
                "added": [], "removed": [], "failed": [], "consumed_ids": []}
    rows = _read_buffer(_buffer_path())

    # ② 裁决(纯函数)
    stock_pool.reload()
    local_index = pool_merge.local_index_from_pool(stock_pool.get_pool())
    plan = pool_merge.plan_digestion(rows, local_index)

    if dry_run:
        return {"ok": True, "dry_run": True, "pulled": len(rows),
                "to_add": [r["code"] for r in plan.to_add],
                "to_remove": [r["code"] for r in plan.to_remove],
                "noop_consumed_ids": list(plan.noop_consumed_ids)}

    # ③ 执行(仅成功才 consume;失败保留 pending 下轮重试)
    consumed: list[int] = list(plan.noop_consumed_ids)
    added: list[str] = []
    removed: list[str] = []
    failed: list[dict] = []
    for r in plan.to_add:
        try:
            pool_service.add_and_collect(r["code"], r.get("name", ""), r.get("industry", ""),
                                         r.get("sector", ""), market=r.get("market", "A"))
            consumed.append(r["id"])
            added.append(r["code"])
        except Exception as e:
            failed.append({"id": r["id"], "code": r["code"], "op": "add", "error": str(e)})
            logger.error("采集入池失败(保留 pending 重试)%s:%s", r["code"], e)
    for r in plan.to_remove:
        try:
            pool_service.remove_and_cleanup(r["code"])
            consumed.append(r["id"])
            removed.append(r["code"])
        except Exception as e:
            failed.append({"id": r["id"], "code": r["code"], "op": "remove", "error": str(e)})
            logger.error("出池清理失败(保留 pending 重试)%s:%s", r["code"], e)

    # ④ 回执待发
    _write_ack(consumed, _ack_path())
    logger.info("消化完成:拉 %d,加 %d,删 %d,失败 %d,回执 %d 条 → %s",
                len(rows), len(added), len(removed), len(failed), len(consumed), _ack_path())
    return {"ok": True, "pulled": len(rows), "added": added, "removed": removed,
            "failed": failed, "consumed_ids": consumed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地闭环:消化远端自选提案(pull→裁决→采集→回执待发)")
    ap.add_argument("--url", default=None, help="远端 /pull 地址;缺省从 SYNC_INGEST_URL 推导")
    ap.add_argument("--dry-run", action="store_true", help="只裁决打印,不采集不写回执")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from tools.sync import pull as pullmod
    missing = [n for n in ("SYNC_INGEST_TOKEN", "SYNC_SIGNING_KEY") if not getattr(settings, n)]
    if missing:
        print(f"缺少必需环境变量:{', '.join(missing)}(见 settings.py 同步配置块 / launchd env 文件)")
        return 2
    url = args.url or pullmod.derive_pull_url(settings.SYNC_INGEST_URL)
    if not url:
        print("无 /pull 地址:设 SYNC_INGEST_URL(.../ingest)或传 --url")
        return 2

    res = consume(url=url, token=settings.SYNC_INGEST_TOKEN, key=settings.SYNC_SIGNING_KEY,
                  key_id=settings.SYNC_KEY_ID, dry_run=args.dry_run)
    if args.dry_run:
        print(f"[dry-run] 拉 {res.get('pulled', 0)} 条:待加 {res.get('to_add')},"
              f"待删 {res.get('to_remove')},noop {len(res.get('noop_consumed_ids', []))}")
        return 0
    if not res.get("ok"):
        print(f"消化未完成:{res.get('error')}")
        return 1
    print(f"消化完成:拉 {res['pulled']},加 {res['added']},删 {res['removed']},"
          f"失败 {len(res['failed'])},回执 {len(res['consumed_ids'])} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
