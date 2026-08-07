"""store 的 DB 后端(SQLAlchemy Core;SQLite 起步,可换 Postgres/MySQL)。

与文件后端**同签名**的分析侧读写:record / view / code_view + iter_records / list_dates
+ delete_stock。上层(serialize/panel/chart/screen 经 repo,web 经 data_access)零改动,
仅由 settings.STORE_BACKEND 切换。schema 真源仍锚定 tools/contracts/,本层只换存储介质。

表结构(按 日期 × code/name 组织,记录整体存 JSON 列,换库不改字段拆分):
  record(date, code, json)              PK(date, code)          -- 个股中心记录
  view(date, name, json)                PK(date, name)          -- panel/screen 池级视图
  code_view(date, name, code, json)     PK(date, name, code)    -- chart/news_ai/sentiment 按票视图

raw 采集缓存(kline/fundamental…)不在本层,恒走文件后端(见 repo.py)。
"""
from __future__ import annotations

import json
import threading

from sqlalchemy import (Column, MetaData, String, Table, Text, create_engine,
                        delete, distinct, insert, select)

from tools.config import settings

_LOCK = threading.RLock()
_engine = None
_meta = MetaData()

record_t = Table(
    "record", _meta,
    Column("date", String(16), primary_key=True),
    Column("code", String(16), primary_key=True),
    Column("json", Text, nullable=False),
)
view_t = Table(
    "view", _meta,
    Column("date", String(16), primary_key=True),
    Column("name", String(64), primary_key=True),
    Column("json", Text, nullable=False),
)
code_view_t = Table(
    "code_view", _meta,
    Column("date", String(16), primary_key=True),
    Column("name", String(64), primary_key=True),
    Column("code", String(16), primary_key=True),
    Column("json", Text, nullable=False),
)


def _get_engine():
    """惰性建引擎并建表(首次调用)。测试可 monkeypatch settings.DB_URL 后调 reset_engine。"""
    global _engine
    with _LOCK:
        if _engine is None:
            url = settings.DB_URL
            if url.startswith("sqlite:///"):                    # 确保 sqlite 文件目录存在
                from pathlib import Path
                p = url.replace("sqlite:///", "", 1)
                Path(p).parent.mkdir(parents=True, exist_ok=True)
            _engine = create_engine(url, future=True)
            _meta.create_all(_engine)
        return _engine


def reset_engine() -> None:
    """丢弃引擎缓存(切换 DB_URL / 测试隔离用)。"""
    global _engine
    with _LOCK:
        if _engine is not None:
            _engine.dispose()
        _engine = None


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _latest_date() -> str | None:
    """全局最新分析日期 = record/view/code_view 三表中最大的 date(与文件后端"最新目录"语义一致)。"""
    eng = _get_engine()
    with eng.connect() as c:
        dates = []
        for t in (record_t, view_t, code_view_t):
            m = c.execute(select(t.c.date).order_by(t.c.date.desc()).limit(1)).scalar()
            if m:
                dates.append(m)
        return max(dates) if dates else None


def _resolve_read_date(date: str | None) -> str | None:
    """读取日期:具体日期直接用;None/"latest" → 全局最新。"""
    if date and date != "latest":
        return date
    return _latest_date()


def _upsert(table, pk: dict, payload) -> None:
    """按主键幂等写:先删同主键行再插(跨 SQLite/MySQL/PG 通用)。"""
    eng = _get_engine()
    with eng.begin() as c:
        cond = [table.c[k] == v for k, v in pk.items()]
        c.execute(delete(table).where(*cond))
        c.execute(insert(table).values(**pk, json=_dumps(payload)))


# ————————————————————————————————————————————————
# 中心记录
# ————————————————————————————————————————————————
def get_record(code: str, date: str | None = "latest") -> dict:
    d = _resolve_read_date(date)
    if d is None:
        raise FileNotFoundError(f"{code} 无结构化记录(DB 无任何日期),请先 serialize")
    eng = _get_engine()
    with eng.connect() as c:
        row = c.execute(select(record_t.c.json)
                        .where(record_t.c.date == d, record_t.c.code == code)).scalar()
    if row is None:
        raise FileNotFoundError(f"{code} 无结构化记录(date={d}),请先 serialize")
    return json.loads(row)


def put_record(rec: dict, date: str) -> str:
    """写中心记录(date 为已解析的具体日期,由 repo 传入)。返回逻辑标识。"""
    code = (rec.get("meta") or {}).get("code")
    if not code:
        raise ValueError("记录缺 meta.code,无法确定主键")
    _upsert(record_t, {"date": date, "code": str(code)}, rec)
    return f"db:record/{date}/{code}"


def iter_records(date: str | None = "latest"):
    d = _resolve_read_date(date)
    if d is None:
        return
    eng = _get_engine()
    with eng.connect() as c:
        rows = c.execute(select(record_t.c.json).where(record_t.c.date == d)).scalars().all()
    for r in rows:
        yield json.loads(r)


# ————————————————————————————————————————————————
# 池级视图 panel / screen
# ————————————————————————————————————————————————
def get_view(name: str, date: str | None = "latest"):
    d = _resolve_read_date(date)
    if d is None:
        raise FileNotFoundError(f"无视图 {name}(DB 无任何日期),请先生成")
    eng = _get_engine()
    with eng.connect() as c:
        row = c.execute(select(view_t.c.json)
                        .where(view_t.c.date == d, view_t.c.name == name)).scalar()
    if row is None:
        raise FileNotFoundError(f"无视图 {name}(date={d}),请先生成")
    return json.loads(row)


def put_view(name: str, obj, date: str) -> str:
    _upsert(view_t, {"date": date, "name": name}, obj)
    return f"db:view/{date}/{name}"


# ————————————————————————————————————————————————
# 按票视图 chart / news_ai / sentiment
# ————————————————————————————————————————————————
def get_code_view(name: str, code: str, date: str | None = "latest") -> dict:
    d = _resolve_read_date(date)
    if d is None:
        raise FileNotFoundError(f"{code} 无 {name} 视图(DB 无任何日期)")
    eng = _get_engine()
    with eng.connect() as c:
        row = c.execute(select(code_view_t.c.json).where(
            code_view_t.c.date == d, code_view_t.c.name == name,
            code_view_t.c.code == code)).scalar()
    if row is None:
        raise FileNotFoundError(f"{code} 无 {name} 视图(date={d})")
    return json.loads(row)


def put_code_view(name: str, code: str, obj, date: str) -> str:
    _upsert(code_view_t, {"date": date, "name": name, "code": code}, obj)
    return f"db:code_view/{date}/{name}/{code}"


# ————————————————————————————————————————————————
# 日期列表 / 删除
# ————————————————————————————————————————————————
def list_dates() -> list[str]:
    """所有分析日期(record/view/code_view 并集),升序。"""
    eng = _get_engine()
    with eng.connect() as c:
        ds: set[str] = set()
        for t in (record_t, view_t, code_view_t):
            ds.update(c.execute(select(distinct(t.c.date))).scalars().all())
    return sorted(ds)


def delete_stock(code: str) -> list[str]:
    """删除某票在 DB 里的所有分析数据(全部日期的 record + code_view)。返回被删标识。"""
    eng = _get_engine()
    removed: list[str] = []
    with eng.begin() as c:
        for t, label in ((record_t, "record"), (code_view_t, "code_view")):
            rows = c.execute(select(t.c.date).where(t.c.code == code)).scalars().all()
            if rows:
                c.execute(delete(t).where(t.c.code == code))
                removed += [f"db:{label}/{d}/{code}" for d in rows]
    return removed
