"""展示端 ingest 的审计 / 防重放 / 快照表(SQLAlchemy,与 store 同一个 DB_URL)。

三张表(B 期自持,**不放进 tools/store/**,避免动合作者地基):
  ingest_audit(id, at, source, key_id, date, rows, verify_ok, result, msg)  -- 每次 ingest 一条(含失败)
  seen_nonce(nonce PK, at)                                                   -- 防重放:见过的 nonce
  snapshot(date PK, generated_at, source, ingested_at)                       -- 供"旧 generated_at 不许盖新"判断

用独立 engine 连同一个 `settings.DB_URL`(SQLite 允许多连接);建表幂等。测试可
monkeypatch settings.DB_URL 后调 reset_engine() 隔离。
"""
from __future__ import annotations

import threading
from datetime import datetime

from sqlalchemy import (Column, Integer, MetaData, String, Table, Text,
                        create_engine, delete, insert, select, update)

from tools.config import settings

_LOCK = threading.RLock()
_engine = None
_meta = MetaData()

ingest_audit_t = Table(
    "ingest_audit", _meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", String(40), nullable=False),
    Column("source", String(64)),
    Column("key_id", String(64)),
    Column("date", String(16)),
    Column("rows", Integer),
    Column("verify_ok", Integer),          # 0/1
    Column("result", String(32)),          # ok / auth_fail / sig_fail / replay / stale / invalid / error
    Column("msg", Text),
)
seen_nonce_t = Table(
    "seen_nonce", _meta,
    Column("nonce", String(128), primary_key=True),
    Column("at", String(40), nullable=False),
)
snapshot_t = Table(
    "snapshot", _meta,
    Column("date", String(16), primary_key=True),
    Column("generated_at", String(40)),
    Column("source", String(64)),
    Column("ingested_at", String(40)),
)


def _get_engine():
    global _engine
    with _LOCK:
        if _engine is None:
            url = settings.DB_URL
            if url.startswith("sqlite:///"):
                from pathlib import Path
                Path(url.replace("sqlite:///", "", 1)).parent.mkdir(parents=True, exist_ok=True)
            _engine = create_engine(url, future=True)
            _meta.create_all(_engine)
        return _engine


def reset_engine() -> None:
    """丢弃 engine 缓存(切库 / 测试隔离用)。"""
    global _engine
    with _LOCK:
        if _engine is not None:
            _engine.dispose()
        _engine = None


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# —— 审计 ——
def record_audit(*, source: str | None, key_id: str | None, date: str | None,
                 rows: int | None, verify_ok: bool, result: str, msg: str = "") -> None:
    """落一条 ingest 审计(成功或失败都要调)。"""
    eng = _get_engine()
    with eng.begin() as c:
        c.execute(insert(ingest_audit_t).values(
            at=_now_iso(), source=source, key_id=key_id, date=date, rows=rows,
            verify_ok=1 if verify_ok else 0, result=result, msg=msg[:2000]))


def audit_count() -> int:
    """审计条数(测试/巡检用)。"""
    eng = _get_engine()
    with eng.connect() as c:
        return len(c.execute(select(ingest_audit_t.c.id)).all())


def last_audit() -> dict | None:
    """最近一条审计(测试/巡检用),无则 None。"""
    eng = _get_engine()
    with eng.connect() as c:
        row = c.execute(select(ingest_audit_t).order_by(ingest_audit_t.c.id.desc())
                        .limit(1)).mappings().first()
        return dict(row) if row else None


def recent_audits(limit: int = 100) -> list[dict]:
    """最近 N 条审计(时间倒序),供审计查询页/接口用。"""
    limit = max(1, min(int(limit), 1000))
    eng = _get_engine()
    with eng.connect() as c:
        rows = c.execute(select(ingest_audit_t).order_by(ingest_audit_t.c.id.desc())
                         .limit(limit)).mappings().all()
    return [dict(r) for r in rows]


# —— 防重放 ——
def nonce_seen(nonce: str) -> bool:
    eng = _get_engine()
    with eng.connect() as c:
        return c.execute(select(seen_nonce_t.c.nonce)
                         .where(seen_nonce_t.c.nonce == nonce)).scalar() is not None


def remember_nonce(nonce: str) -> None:
    eng = _get_engine()
    with eng.begin() as c:
        c.execute(insert(seen_nonce_t).values(nonce=nonce, at=_now_iso()))


def purge_old_nonces(before_iso: str) -> int:
    """清理 at < before_iso 的 nonce(定时运维用),返回删除条数。"""
    eng = _get_engine()
    with eng.begin() as c:
        r = c.execute(delete(seen_nonce_t).where(seen_nonce_t.c.at < before_iso))
        return r.rowcount or 0


def purge_expired(keep_s: int | None = None, now: datetime | None = None) -> int:
    """清理早于 (now - keep_s) 的 nonce。keep_s 缺省取 settings.SYNC_NONCE_KEEP_S。
    keep_s 只要 > 防重放窗口即安全(更早的 nonce 其时间戳已超窗,不可能再被接受)。"""
    from datetime import timedelta
    keep_s = keep_s if keep_s is not None else getattr(settings, "SYNC_NONCE_KEEP_S", 86400)
    now = now or datetime.now().astimezone()
    cutoff = (now - timedelta(seconds=keep_s)).isoformat(timespec="seconds")
    return purge_old_nonces(cutoff)


def main(argv=None) -> int:
    """nonce 清理入口(供 systemd timer / cron 调用):python -m tools.sync.audit"""
    import argparse
    ap = argparse.ArgumentParser(description="清理过期 nonce(防重放表定期瘦身)")
    ap.add_argument("--keep-seconds", type=int, default=None,
                    help="保留多少秒内的 nonce;缺省取 SYNC_NONCE_KEEP_S")
    args = ap.parse_args(argv)
    n = purge_expired(keep_s=args.keep_seconds)
    print(f"已清理过期 nonce:{n} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# —— 快照(旧盖新判断)——
def get_snapshot_generated_at(date: str) -> str | None:
    eng = _get_engine()
    with eng.connect() as c:
        return c.execute(select(snapshot_t.c.generated_at)
                         .where(snapshot_t.c.date == date)).scalar()


def upsert_snapshot(date: str, generated_at: str, source: str | None) -> None:
    eng = _get_engine()
    with eng.begin() as c:
        exists = c.execute(select(snapshot_t.c.date)
                           .where(snapshot_t.c.date == date)).scalar() is not None
        if exists:
            c.execute(update(snapshot_t).where(snapshot_t.c.date == date).values(
                generated_at=generated_at, source=source, ingested_at=_now_iso()))
        else:
            c.execute(insert(snapshot_t).values(
                date=date, generated_at=generated_at, source=source, ingested_at=_now_iso()))
