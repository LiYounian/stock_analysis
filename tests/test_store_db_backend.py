"""store DB 后端单测:与文件后端行为等价 + 按日期快照 + 删除。

锁定《展示端与数据同步》验收 A1/A2/A3:换后端上层零改动、语义不变。
隔离:tmp_path 临时 SQLite;monkeypatch settings.STORE_BACKEND/DB_URL + repo._RAW_DIR;
reset_engine 避免引擎跨用例泄漏。set_active_date 固定运行日期。
"""
import pytest

from tools.config import settings
from tools.store import backend_db, repo

_D = "2026-08-06"


def _rec(code: str) -> dict:
    return {"schema_version": "1.0", "meta": {"code": code, "name": f"票{code}"},
            "signals": None, "events": []}


@pytest.fixture
def db(tmp_path, monkeypatch):
    """DB 后端 + 临时 SQLite;raw 目录也隔离到 tmp(delete_stock 会碰 raw 文件)。"""
    monkeypatch.setattr(settings, "STORE_BACKEND", "db")
    monkeypatch.setattr(settings, "DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(repo, "_RAW_DIR", raw)
    backend_db.reset_engine()
    repo.set_active_date(_D)
    yield repo
    repo.set_active_date(None)
    backend_db.reset_engine()


# —— 记录 / 视图 / 按票视图 往返 ——
def test_record_roundtrip(db):
    db.put_record(_rec("000021"))
    assert db.get_record("000021") == _rec("000021")


def test_record_latest_of_multiple_dates(db):
    db.put_record(_rec("000021"), date="2026-08-01")
    db.put_record({**_rec("000021"), "mark": "new"}, date="2026-08-06")
    assert db.get_record("000021").get("mark") == "new"     # 取最新日期


def test_put_record_missing_code_raises(db):
    with pytest.raises(ValueError):
        db.put_record({"meta": {}})


def test_get_record_missing_raises(db):
    with pytest.raises(FileNotFoundError):
        db.get_record("999999")


def test_view_roundtrip(db):
    db.put_view("panel", {"rows": [1, 2, 3]})
    assert db.get_view("panel") == {"rows": [1, 2, 3]}


def test_code_view_roundtrip(db):
    db.put_code_view("chart", "000021", {"close": [1, 2]})
    assert db.get_code_view("chart", "000021") == {"close": [1, 2]}


def test_upsert_overwrites_same_key(db):
    db.put_record(_rec("000021"))
    db.put_record({**_rec("000021"), "mark": "v2"})          # 同 (date,code) 覆盖
    assert db.get_record("000021").get("mark") == "v2"
    assert len(list(db.iter_records())) == 1                 # 不产生重复行


# —— 遍历 / 日期列表 / 删除 ——
def test_iter_records_excludes_views(db):
    db.put_record(_rec("000021"))
    db.put_record(_rec("600519"))
    db.put_view("panel", {"x": 1})                            # 视图不混入个股遍历
    assert sorted(r["meta"]["code"] for r in db.iter_records()) == ["000021", "600519"]


def test_list_dates(db):
    db.put_record(_rec("000021"), date="2026-08-01")
    db.put_record(_rec("000021"), date="2026-08-06")
    assert db.list_dates() == ["2026-08-01", "2026-08-06"]
    assert db.list_dates("analysis") == ["2026-08-01", "2026-08-06"]


def test_delete_stock(db):
    db.put_record(_rec("000021"))
    db.put_code_view("chart", "000021", {"a": 1})
    db.put_record(_rec("600519"))
    removed = db.delete_stock("000021")
    assert removed                                            # 确有删除
    with pytest.raises(FileNotFoundError):
        db.get_record("000021")
    assert db.get_record("600519")["meta"]["code"] == "600519"  # 别的票不受影响


# —— 核心:同一批数据,DB 后端 与 文件后端 get_* 结果逐字段等价 ——
def test_equivalence_file_vs_db(tmp_path, monkeypatch):
    rec, view, cv = _rec("000021"), {"rows": [1, 2]}, {"close": [3, 4]}

    # 文件后端写读
    fraw, fan = tmp_path / "fr", tmp_path / "fa"
    fraw.mkdir()
    fan.mkdir()
    monkeypatch.setattr(settings, "STORE_BACKEND", "file")
    monkeypatch.setattr(repo, "_RAW_DIR", fraw)
    monkeypatch.setattr(repo, "_ANALYSIS_DIR", fan)
    repo.set_active_date(_D)
    repo.put_record(rec)
    repo.put_view("panel", view)
    repo.put_code_view("chart", "000021", cv)
    f = (repo.get_record("000021"), repo.get_view("panel"),
         repo.get_code_view("chart", "000021"), repo.list_dates("analysis"))

    # DB 后端写读(同一批数据)
    monkeypatch.setattr(settings, "STORE_BACKEND", "db")
    monkeypatch.setattr(settings, "DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    backend_db.reset_engine()
    repo.put_record(rec)
    repo.put_view("panel", view)
    repo.put_code_view("chart", "000021", cv)
    d = (repo.get_record("000021"), repo.get_view("panel"),
         repo.get_code_view("chart", "000021"), repo.list_dates("analysis"))
    repo.set_active_date(None)
    backend_db.reset_engine()

    assert d == f      # 记录/视图/按票视图/日期列表 全部逐字段等价
