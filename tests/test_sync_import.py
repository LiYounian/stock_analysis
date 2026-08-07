"""tools.sync.import_to_db 单测:文件产物 → DB 后端的搬运正确 + 幂等。

锁住语义:
  - 顶层 6 位 json → record;顶层其它 json → view;子目录 <name>/<code>.json → code_view;
  - 导入后经 backend_db 能逐字段读回;list_dates 反映导入的日期;
  - 二次导入幂等(不产生重复记录、日期不翻倍)。
路径/库隔离:tmp_path 造 analysis 目录 + monkeypatch settings.DB_URL 指到临时 SQLite。
"""
import json

import pytest

from tools.config import settings
from tools.store import backend_db
from tools.sync import import_to_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    """把 DB 指到临时 SQLite,前后 reset_engine 隔离,绝不碰真实库。"""
    monkeypatch.setattr(settings, "DB_URL", f"sqlite:///{tmp_path / 'test.db'}")
    backend_db.reset_engine()
    yield
    backend_db.reset_engine()


def _seed(analysis, date):
    d = analysis / date
    (d / "chart").mkdir(parents=True)
    (d / "news_ai").mkdir(parents=True)
    (d / "000021.json").write_text(
        json.dumps({"meta": {"code": "000021", "name": "深科技"}, "signals": None}),
        encoding="utf-8")
    (d / "panel.json").write_text(
        json.dumps({"rows": [{"代码": "000021", "涨跌%": 1.2}]}), encoding="utf-8")
    (d / "screen.json").write_text(json.dumps({"presets": {}}), encoding="utf-8")
    (d / "chart" / "000021.json").write_text(
        json.dumps({"dates": ["2026-08-06"], "close": [12.3]}), encoding="utf-8")
    (d / "news_ai" / "000021.json").write_text(
        json.dumps([{"title": "t", "ai": {"方向": "多"}}]), encoding="utf-8")


def test_import_populates_db(tmp_path, db):
    analysis = tmp_path / "analysis"
    _seed(analysis, "2026-08-06")

    total = import_to_db.import_all(analysis_dir=analysis)
    assert total == {"dates": 1, "records": 1, "views": 2, "code_views": 2}

    # 逐类读回验证(经 DB 后端)
    assert backend_db.get_record("000021", "2026-08-06")["meta"]["name"] == "深科技"
    assert backend_db.get_view("panel", "2026-08-06")["rows"][0]["代码"] == "000021"
    assert backend_db.get_view("screen", "2026-08-06") == {"presets": {}}
    assert backend_db.get_code_view("chart", "000021", "2026-08-06")["close"] == [12.3]
    assert backend_db.get_code_view("news_ai", "000021", "2026-08-06")[0]["ai"]["方向"] == "多"
    assert backend_db.list_dates() == ["2026-08-06"]


def test_import_only_date(tmp_path, db):
    analysis = tmp_path / "analysis"
    _seed(analysis, "2026-08-06")
    _seed(analysis, "2026-08-07")

    import_to_db.import_all(analysis_dir=analysis, only_date="2026-08-07")
    assert backend_db.list_dates() == ["2026-08-07"]   # 只导指定那天


def test_import_idempotent(tmp_path, db):
    analysis = tmp_path / "analysis"
    _seed(analysis, "2026-08-06")

    import_to_db.import_all(analysis_dir=analysis)
    import_to_db.import_all(analysis_dir=analysis)      # 二次导入不应产生重复

    assert backend_db.list_dates() == ["2026-08-06"]
    assert len(list(backend_db.iter_records("2026-08-06"))) == 1


def test_import_missing_dir_is_noop(tmp_path, db):
    total = import_to_db.import_all(analysis_dir=tmp_path / "nope")
    assert total == {"dates": 0, "records": 0, "views": 0, "code_views": 0}
