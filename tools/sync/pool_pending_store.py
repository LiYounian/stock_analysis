"""远端自选提案表 pool_pending(方案2:远端只登记名单增量,采集/重建回本地)。

远端展示端点"加/删自选"时**不写** config/stock_pool.json(那是 git 跟踪文件,会被
autoupdate 的 `git reset --hard` 5min 一次抹掉),改写这张 DB 表——DB 不随 git,
天然持久。本地闭环经 ingest `/pull?kind=pool_pending` 拉走 status=pending 的提案,
合并裁决后 add_and_collect / remove_and_cleanup(本地有 raw,panel 不塌),再经 upload
带回执把对应行标 consumed。名单真源恒为本地 config/stock_pool.json,本表只是**提案队列**。

与 tools/sync/audit.py 同构:独立 engine 连同一个 settings.DB_URL(SQLite 允许多连接),
独立 MetaData、建表幂等,不放进 tools/store/(避免动合作者地基)。测试可 monkeypatch
settings.DB_URL 后调 reset_engine() 隔离。

**安全红线**:本表只存名单元数据,绝不存密钥/配置;`/pull` 回该表时只返下列列。
"""
from __future__ import annotations

import threading
from datetime import datetime

from sqlalchemy import (Column, Integer, MetaData, String, Table,
                        create_engine, insert, select, update)

from tools.config import settings

_LOCK = threading.RLock()
_engine = None
_meta = MetaData()

# 一行 = 一条远端提案。id 自增,作幂等键(回执按 id 标 consumed,重复回执无副作用)。
pool_pending_t = Table(
    "pool_pending", _meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("code", String(8), nullable=False),       # A股6位 / 港股5位
    Column("name", String(64)),                       # 加票时前端传;删票可空
    Column("industry", String(64)),                   # 可空
    Column("sector", String(64)),                     # 加票必填(下游聚合需要);删票可空
    Column("market", String(4)),                      # "A"/"HK",默认 "A"
    Column("op", String(8), nullable=False),          # "add" / "remove"
    Column("source", String(32)),                     # 提案来源标识,默认 "remote"
    Column("requested_at", String(40), nullable=False),  # ISO 时间戳(入队时刻,删除裁决用)
    Column("status", String(16), nullable=False),     # "pending" / "consumed"
    Column("consumed_at", String(40)),                # 消化回执时刻,nullable
)

_VALID_OPS = ("add", "remove")


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


def enqueue(*, code: str, name: str = "", industry: str = "", sector: str = "",
            market: str = "A", op: str, source: str = "remote",
            requested_at: str | None = None) -> int:
    """入队一条提案(status=pending),返回新行 id。

    轻校验:op∈{add,remove}、code 非空。格式/重复的深校验留给本地消化时的
    stock_pool.add_stock(它才是名单真源的裁决者)——这里只负责"登记提案"。
    requested_at 缺省=now(ISO,带时区,秒精度)。
    """
    op = (op or "").strip().lower()
    if op not in _VALID_OPS:
        raise ValueError(f"op 须为 {_VALID_OPS} 之一:{op!r}")
    code = (code or "").strip()
    if not code:
        raise ValueError("code 不能为空")
    market = (market or "A").strip().upper() or "A"
    eng = _get_engine()
    with eng.begin() as c:
        r = c.execute(insert(pool_pending_t).values(
            code=code, name=(name or "").strip(), industry=(industry or "").strip(),
            sector=(sector or "").strip(), market=market, op=op,
            source=(source or "remote").strip(),
            requested_at=requested_at or _now_iso(),
            status="pending", consumed_at=None))
        return int(r.inserted_primary_key[0])


def list_pending(status: str = "pending", limit: int = 1000) -> list[dict]:
    """按 requested_at 升序返回该 status 的行(dict,列名同表结构)。

    升序是删除裁决(pool_merge.plan_digestion)的前置约定:按提案时间先后逐条裁决。
    """
    limit = max(1, min(int(limit), 10000))
    eng = _get_engine()
    with eng.connect() as c:
        rows = c.execute(
            select(pool_pending_t)
            .where(pool_pending_t.c.status == status)
            .order_by(pool_pending_t.c.requested_at.asc(), pool_pending_t.c.id.asc())
            .limit(limit)).mappings().all()
    return [dict(r) for r in rows]


def mark_consumed(ids: list[int], consumed_at: str | None = None) -> int:
    """把这些 id 的 status 置 consumed + 写 consumed_at。返回受影响行数。

    幂等:已 consumed 的行再标无副作用(只更新时间戳)。空 ids → 0。
    """
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return 0
    eng = _get_engine()
    with eng.begin() as c:
        r = c.execute(update(pool_pending_t)
                      .where(pool_pending_t.c.id.in_(ids))
                      .values(status="consumed", consumed_at=consumed_at or _now_iso()))
        return r.rowcount or 0


def count(status: str | None = None) -> int:
    """行数(测试/巡检用);status=None 统计全部。"""
    eng = _get_engine()
    with eng.connect() as c:
        stmt = select(pool_pending_t.c.id)
        if status is not None:
            stmt = stmt.where(pool_pending_t.c.status == status)
        return len(c.execute(stmt).all())
