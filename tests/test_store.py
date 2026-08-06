"""数据存取层 store 单测(按日期分区)。

锁定语义:
  - put/get 往返一致(记录 / raw / 视图 / 按票视图);写落 <日期>/,读取最新日期;
  - iter_records 遍历个股记录且排除 panel/screen 视图;
  - 采集元数据(fetched_at/source)+ 原子写(无 .tmp 残留)+ 新鲜度 is_stale;
  - llm_cache 扁平不分日期(跨天复用);
  - 缺失抛 FileNotFoundError;未知 kind 抛 ValueError;put_record 缺 meta.code 抛 ValueError。
路径隔离:tmp_path + monkeypatch 路径根;set_active_date 固定运行日期,绝不污染真实 data/。
"""
import json

import pytest

from tools.store import repo

_D = "2026-08-06"     # 固定运行日期


@pytest.fixture
def store(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    analysis = tmp_path / "analysis"
    raw.mkdir()
    analysis.mkdir()
    monkeypatch.setattr(repo, "_RAW_DIR", raw)
    monkeypatch.setattr(repo, "_ANALYSIS_DIR", analysis)
    repo.set_active_date(_D)          # 本次所有写入落 2026-08-06/
    yield repo
    repo.set_active_date(None)        # 复位,避免泄漏到其它测试


def _rec(code: str) -> dict:
    return {"schema_version": "1.0", "meta": {"code": code, "name": f"票{code}"},
            "signals": None, "events": []}


# —— 中心记录往返 + 按日期落盘 ——
def test_record_roundtrip(store):
    rec = _rec("000021")
    path = store.put_record(rec)
    assert path.endswith(f"{_D}/000021.json")     # 落到日期目录
    assert store.get_record("000021") == rec       # latest 读回


def test_put_record_uses_meta_code(store):
    store.put_record(_rec("600519"))
    assert store.get_record("600519")["meta"]["name"] == "票600519"


def test_put_record_missing_code_raises(store):
    with pytest.raises(ValueError):
        store.put_record({"meta": {}})


def test_get_record_latest_of_multiple_dates(store):
    """有多个日期目录时,get_record 取最新那天。"""
    store.put_record(_rec("000021"), date="2026-08-01")
    store.put_record({**_rec("000021"), "mark": "new"}, date="2026-08-06")
    assert store.get_record("000021").get("mark") == "new"


# —— raw json 往返 ——
def test_raw_json_roundtrip(store):
    payload = [{"title": "利好A", "time": "2026-08-06"}]
    store.put_raw("news", "000021", payload)
    assert store.get_raw("news", "000021") == payload


def test_raw_llm_cache_is_flat(store):
    """llm_cache 不分日期(扁平),换运行日期仍能读到。"""
    store.put_raw("llm_cache", "000021", {"x": 1})
    store.set_active_date("2026-09-09")            # 换一天
    assert store.get_raw("llm_cache", "000021") == {"x": 1}   # 仍命中


def test_raw_unknown_kind_raises(store):
    with pytest.raises(ValueError):
        store.put_raw("nope", "000021", {})
    with pytest.raises(ValueError):
        store.get_raw("nope", "000021")


def test_raw_parquet_roundtrip(store):
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame({"date": ["2026-08-05", "2026-08-06"], "close": [1.0, 2.0]})
    store.put_raw("kline", "000021", df)
    got = store.get_raw("kline", "000021")
    assert list(got["close"]) == [1.0, 2.0]


# —— 采集元数据 + 原子写 + 新鲜度 ——
def test_put_raw_writes_meta(store):
    store.put_raw("news", "000021", [{"t": 1}, {"t": 2}], meta={"source": "eastmoney"})
    m = store.get_raw_meta("news", "000021")
    assert m["source"] == "eastmoney"
    assert m["rows"] == 2
    assert m["fetched_at"]
    assert store.get_raw("news", "000021") == [{"t": 1}, {"t": 2}]


def test_put_raw_meta_missing_returns_none(store):
    assert store.get_raw_meta("news", "999999") is None


def test_atomic_write_leaves_no_tmp(store):
    store.put_raw("news", "000021", [{"t": 1}])
    store.put_record(_rec("000021"))
    assert list((store._RAW_DIR).rglob("*.tmp")) == []
    assert list((store._ANALYSIS_DIR).rglob("*.tmp")) == []


def test_is_stale_fresh_vs_missing(store):
    store.put_raw("kline", "000021",
                  __import__("pandas").DataFrame({"close": [1.0]}),
                  meta={"source": "tencent"})
    assert store.is_stale("kline", "000021", max_days=3) is False
    assert store.is_stale("kline", "999999", max_days=3) is True


def test_is_stale_old_meta(store):
    store.put_raw("news", "000021", [{"t": 1}])
    mp = store._meta_path("news", "000021", _D)
    m = json.loads(mp.read_text(encoding="utf-8"))
    m["fetched_at"] = "2000-01-01T00:00:00"
    mp.write_text(json.dumps(m), encoding="utf-8")
    assert store.is_stale("news", "000021", max_days=3) is True


# —— 视图 / 按票视图往返 ——
def test_view_roundtrip(store):
    obj = {"rows": [{"代码": "000021", "涨跌%": 1.2}]}
    store.put_view("panel", obj)
    assert store.get_view("panel") == obj


def test_code_view_roundtrip(store):
    obj = {"dates": ["2026-08-06"], "close": [12.3]}
    store.put_code_view("chart", "000021", obj)
    assert store.get_code_view("chart", "000021") == obj


# —— iter_records 遍历且排除视图 ——
def test_iter_records_excludes_views(store):
    store.put_record(_rec("000021"))
    store.put_record(_rec("600519"))
    store.put_view("panel", {"rows": []})
    store.put_view("screen", {"candidates": []})
    codes = sorted(r["meta"]["code"] for r in store.iter_records())
    assert codes == ["000021", "600519"]


def test_iter_records_empty(store):
    assert list(store.iter_records()) == []


# —— 缺失文件抛 FileNotFoundError ——
def test_get_record_missing_raises(store):
    with pytest.raises(FileNotFoundError):
        store.get_record("999999")


def test_get_raw_missing_raises(store):
    with pytest.raises(FileNotFoundError):
        store.get_raw("news", "999999")


def test_get_view_missing_raises(store):
    with pytest.raises(FileNotFoundError):
        store.get_view("panel")
