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


# —— 采集元数据 + 原子写 + 新鲜度 ——
def test_put_raw_writes_meta(store):
    """put_raw 旁写 meta:source 透传、rows 自测、fetched_at 自动补。"""
    store.put_raw("news", "000021", [{"t": 1}, {"t": 2}], meta={"source": "eastmoney"})
    m = store.get_raw_meta("news", "000021")
    assert m["source"] == "eastmoney"
    assert m["rows"] == 2
    assert m["fetched_at"]                       # 自动补,非空
    # meta sidecar 不污染数据读取
    assert store.get_raw("news", "000021") == [{"t": 1}, {"t": 2}]


def test_put_raw_meta_missing_returns_none(store):
    """从未写过 → get_raw_meta 返回 None(元数据 advisory)。"""
    assert store.get_raw_meta("news", "999999") is None


def test_atomic_write_leaves_no_tmp(store):
    """原子写落地后目录里不残留 .tmp。"""
    store.put_raw("news", "000021", [{"t": 1}])
    store.put_record(_rec("000021"))
    leftovers = list((store._RAW_DIR / "news").glob("*.tmp")) + \
        list(store._ANALYSIS_DIR.glob("*.tmp"))
    assert leftovers == []


def test_is_stale_fresh_vs_missing(store):
    """刚写的不算陈旧;无任何数据/元数据视为陈旧。"""
    store.put_raw("kline", "000021",
                  __import__("pandas").DataFrame({"close": [1.0]}),
                  meta={"source": "tencent"})
    assert store.is_stale("kline", "000021", max_days=3) is False
    assert store.is_stale("kline", "999999", max_days=3) is True


def test_is_stale_old_meta(store):
    """篡改 fetched_at 到很久以前 → 判定陈旧。"""
    store.put_raw("news", "000021", [{"t": 1}])
    mp = store._meta_path("news", "000021")
    m = json.loads(mp.read_text(encoding="utf-8"))
    m["fetched_at"] = "2000-01-01T00:00:00"
    mp.write_text(json.dumps(m), encoding="utf-8")
    assert store.is_stale("news", "000021", max_days=3) is True


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
