"""数据存取层 store 单测。

锁定语义:
  - put/get 往返一致(记录 json / raw json / 视图);
  - iter_records 遍历个股记录且排除 panel/screen 等非个股文件;
  - 缺失文件抛 FileNotFoundError;未知 kind 抛 ValueError;put_record 缺 meta.code 抛 ValueError。
路径隔离:用 tmp_path + monkeypatch 把 store 的路径根指到临时目录,绝不污染真实 data/。
"""
import json

import pytest

from tools.store import repo


@pytest.fixture
def store(tmp_path, monkeypatch):
    """把 raw / analysis 路径根 monkeypatch 到临时目录,返回 repo 模块。"""
    raw = tmp_path / "raw"
    analysis = tmp_path / "analysis"
    raw.mkdir()
    analysis.mkdir()
    monkeypatch.setattr(repo, "_RAW_DIR", raw)
    monkeypatch.setattr(repo, "_ANALYSIS_DIR", analysis)
    return repo


def _rec(code: str) -> dict:
    return {"schema_version": "1.0", "meta": {"code": code, "name": f"票{code}"},
            "signals": None, "events": []}


# —— 中心记录往返 ——
def test_record_roundtrip(store):
    rec = _rec("000021")
    path = store.put_record(rec)
    assert path.endswith("000021.json")
    got = store.get_record("000021")
    assert got == rec


def test_put_record_uses_meta_code(store):
    """文件名取自 rec['meta']['code'],而非传参。"""
    store.put_record(_rec("600519"))
    assert store.get_record("600519")["meta"]["name"] == "票600519"


def test_put_record_missing_code_raises(store):
    with pytest.raises(ValueError):
        store.put_record({"meta": {}})


# —— raw json 往返(如 news)——
def test_raw_json_roundtrip(store):
    payload = [{"title": "利好A", "time": "2026-08-06"}]
    store.put_raw("news", "000021", payload)
    assert store.get_raw("news", "000021") == payload


def test_raw_json_llm_cache_roundtrip(store):
    payload = {"extract": {"净情绪分": 0.3}}
    store.put_raw("llm_cache", "000021", payload)
    assert store.get_raw("llm_cache", "000021") == payload


def test_raw_unknown_kind_raises(store):
    with pytest.raises(ValueError):
        store.put_raw("nope", "000021", {})
    with pytest.raises(ValueError):
        store.get_raw("nope", "000021")


# —— raw parquet 往返(kline/fundflow)——
def test_raw_parquet_roundtrip(store):
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame({"date": ["2026-08-05", "2026-08-06"], "close": [1.0, 2.0]})
    store.put_raw("kline", "000021", df)
    got = store.get_raw("kline", "000021")
    assert list(got["close"]) == [1.0, 2.0]


# —— 视图往返 ——
def test_view_roundtrip(store):
    obj = {"rows": [{"代码": "000021", "涨跌%": 1.2}]}
    store.put_view("panel", obj)
    assert store.get_view("panel") == obj


# —— iter_records 遍历且排除视图 ——
def test_iter_records_excludes_views(store):
    store.put_record(_rec("000021"))
    store.put_record(_rec("600519"))
    store.put_view("panel", {"rows": []})
    store.put_view("screen", {"candidates": []})
    # 再塞一个非个股 json(应被排除)
    (store._ANALYSIS_DIR / "notes.json").write_text(
        json.dumps({"x": 1}), encoding="utf-8")

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
