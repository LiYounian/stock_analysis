"""数据存取层(文件后端仓储):收敛 raw 原始数据 / 中心记录 / 视图 的读写口径。

上层(采集/分析/组装/聚合/展示)不再直接碰路径与格式,统一走本层。
现状物理是文件;规模化后只换本层实现(DuckDB/SQLite),上层零改动
(见 docs/信息流转与层职责.md §2.2 / §3)。

物理格式映射(kind → parquet/json):
  - parquet(列式,存时序数值,pandas 读写):kline、fundflow
  - json(半结构化文本/嵌套):fundamental、announcement、news、llm_cache
  依据:与现有 collectors 落盘口径一致——market/fundflow 落 parquet(量价时序),
  news/fundamental/announcement 落 json(文本/嵌套结构)。

职责边界(基座层):只依赖 config;不 import 采集/分析/展示。
缺失约定:所有 get_* 缺文件抛 FileNotFoundError(与现有 load_X 一致)。
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from tools.config import settings

logger = logging.getLogger("store.repo")

# —— 路径根(可被测试 monkeypatch 指到临时目录)——
_RAW_DIR = settings.DATA_RAW                                   # data/raw
_ANALYSIS_DIR = settings.PROJECT_ROOT / "data" / "analysis"   # data/analysis

# —— kind → 物理格式 ——
_PARQUET_KINDS = ("kline", "fundflow")
_JSON_KINDS = ("fundamental", "announcement", "news", "llm_cache")
_RAW_KINDS = _PARQUET_KINDS + _JSON_KINDS

# 个股中心记录文件名 = 6 位代码;iter_records 靠此排除 panel/screen 等非个股文件。
# (不 import contracts:store 只依赖 config,故此处本地固化同一约定。)
_CODE_RE = re.compile(r"^\d{6}$")


# ————————————————————————————————————————————————
# 内部:路径解析(运行期读模块全局,便于测试 monkeypatch 路径根)
# ————————————————————————————————————————————————
def _raw_path(kind: str, code: str) -> Path:
    if kind not in _RAW_KINDS:
        raise ValueError(f"未知 raw kind: {kind!r}(支持 {_RAW_KINDS})")
    ext = "parquet" if kind in _PARQUET_KINDS else "json"
    return _RAW_DIR / kind / f"{code}.{ext}"


def _meta_path(kind: str, code: str) -> Path:
    """raw 采集元数据 sidecar(记 fetched_at/source/rows,供新鲜度判断)。
    与数据文件同目录、独立后缀 `.meta.json`,不与 `{code}.json` 冲突。"""
    return _raw_path(kind, code).parent / f"{code}.meta.json"


def _record_path(code: str) -> Path:
    return _ANALYSIS_DIR / f"{code}.json"


def _view_path(name: str) -> Path:
    return _ANALYSIS_DIR / f"{name}.json"


def _read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _write_json(p: Path, obj) -> str:
    """原子写:先写同目录 .tmp,再 os.replace 覆盖目标(崩溃/并发不留半截文件)。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / (p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return str(p)


def _now_iso() -> str:
    """本地时区当前时间(秒级 ISO),作 raw 采集时间戳。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ————————————————————————————————————————————————
# raw 原始数据
# ————————————————————————————————————————————————
def get_raw(kind: str, code: str):
    """读单票某类 raw 数据。parquet kind 返回 DataFrame,json kind 返回 dict/list。

    缺文件抛 FileNotFoundError;未知 kind 抛 ValueError。
    """
    p = _raw_path(kind, code)
    if not p.exists():
        raise FileNotFoundError(f"{code} 无 {kind} 原始数据,请先采集: {p}")
    if kind in _PARQUET_KINDS:
        import pandas as pd
        return pd.read_parquet(p)
    return _read_json(p)


def put_raw(kind: str, code: str, payload, meta: dict | None = None) -> str:
    """写单票某类 raw 数据(原子写)+ 旁写采集元数据 sidecar。返回数据文件路径。

    parquet kind 需传 DataFrame,json kind 传 dict/list。
    meta:可含 source 等;`fetched_at` 自动补当前时间、`rows` 自动测量(均可被 meta 覆盖)。
    """
    p = _raw_path(kind, code)
    p.parent.mkdir(parents=True, exist_ok=True)
    if kind in _PARQUET_KINDS:
        tmp = p.parent / (p.name + ".tmp")
        payload.to_parquet(tmp, index=False)
        os.replace(tmp, p)               # 原子覆盖
        data_path = str(p)
    else:
        data_path = _write_json(p, payload)
    _write_meta(kind, code, payload, meta)
    return data_path


def _write_meta(kind: str, code: str, payload, meta: dict | None) -> None:
    """旁写 {code}.meta.json:fetched_at(自动)+ rows(可测则测)+ 调用方 meta。"""
    m = {"fetched_at": _now_iso(), "kind": kind, "code": code}
    try:
        m["rows"] = len(payload)
    except TypeError:
        pass
    if meta:
        m.update(meta)
    _write_json(_meta_path(kind, code), m)


def get_raw_meta(kind: str, code: str) -> dict | None:
    """读某票某类 raw 的采集元数据;无 sidecar 返回 None(元数据是可选/advisory)。"""
    p = _meta_path(kind, code)
    return _read_json(p) if p.exists() else None


def raw_age_days(kind: str, code: str) -> float | None:
    """raw 数据距上次采集的天数;无元数据/时间戳不可解析时返回 None。"""
    m = get_raw_meta(kind, code)
    if not m or not m.get("fetched_at"):
        return None
    try:
        t = datetime.fromisoformat(m["fetched_at"])
    except ValueError:
        return None
    now = datetime.now(t.tzinfo) if t.tzinfo else datetime.now()
    return (now - t).total_seconds() / 86400.0


def is_stale(kind: str, code: str, max_days: float) -> bool:
    """数据是否超 max_days 未更新。无数据/无元数据一律视为陈旧(促使重采)。"""
    age = raw_age_days(kind, code)
    return age is None or age > max_days


# ————————————————————————————————————————————————
# 中心记录 data/analysis/{code}.json
# ————————————————————————————————————————————————
def get_record(code: str) -> dict:
    """读单票中心记录。缺文件抛 FileNotFoundError。"""
    p = _record_path(code)
    if not p.exists():
        raise FileNotFoundError(f"{code} 无结构化记录,请先 serialize: {p}")
    return _read_json(p)


def put_record(rec: dict) -> str:
    """写中心记录(用 rec['meta']['code'] 定文件名)。缺 code 抛 ValueError。返回路径。"""
    code = (rec.get("meta") or {}).get("code")
    if not code:
        raise ValueError("记录缺 meta.code,无法确定文件名")
    return _write_json(_record_path(str(code)), rec)


def iter_records():
    """遍历 data/analysis/ 下所有个股中心记录,yield 记录 dict。

    仅 yield 文件名为 6 位代码的 json,自动排除 panel.json / screen.json 等非个股文件。
    """
    for p in sorted(_ANALYSIS_DIR.glob("*.json")):
        if _CODE_RE.match(p.stem):
            yield _read_json(p)


# ————————————————————————————————————————————————
# 视图 data/analysis/{name}.json(panel / screen 等)
# ————————————————————————————————————————————————
def get_view(name: str):
    """读视图对象(如 panel/screen)。缺文件抛 FileNotFoundError。"""
    p = _view_path(name)
    if not p.exists():
        raise FileNotFoundError(f"无视图 {name},请先生成: {p}")
    return _read_json(p)


def put_view(name: str, obj) -> str:
    """写视图对象。返回路径。"""
    return _write_json(_view_path(name), obj)
