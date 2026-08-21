"""数据存取层(文件后端仓储):收敛 raw 原始数据 / 中心记录 / 视图 的读写口径。

上层(采集/分析/组装/聚合/展示)不再直接碰路径与格式,统一走本层。
现状物理是文件;规模化后只换本层实现(DuckDB/SQLite),上层零改动
(见 docs/信息流转与层职责.md §2.2 / §3)。

按日期分区(2026-08 起):每次跑的数据落到 `<日期>/` 子目录,旧数据不被覆盖、
天然留历史快照(未来回测 BT.2 直接可用)。
  - 写:date 缺省取 `active_date()`(编排开始时 set_active_date 设一次)→ 再缺省今天。
  - 读:date 缺省 "latest" → 解析**含该目标数据(kind+code / record / view)的最新日期**,
    即从最新日期倒序回退到第一个该文件确实存在的日期(某日只抓了部分 kind 时不会误判缺失)。
  - 例外:`llm_cache` 不按日期(内容 hash,跨天复用免重复烧钱)。

物理格式映射(kind → parquet/json):
  - parquet(列式,时序数值):kline、fundflow、index_kline、board_kline
  - json(半结构化):fundamental、announcement、news、ugc、policy、llm_cache、board_membership

职责边界(基座层):只依赖 config;不 import 采集/分析/展示。
缺失约定:所有 get_* 缺数据抛 FileNotFoundError(与现有 load_X 一致)。
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
_MASTER_DIR = settings.DATA_MASTER                            # data/master(滚动主档)
_MASTER_DAILY_BASIC_DIR = _MASTER_DIR / "daily_basic"         # 全市场日度换手率/流通股本
_ANALYSIS_DIR = settings.PROJECT_ROOT / "data" / "analysis"   # data/analysis

# —— kind → 物理格式 ——
_PARQUET_KINDS = ("kline", "fundflow", "index_kline", "board_kline",
                  "event_yjyg", "event_yjkb", "event_ggcg")   # 事件驱动采集缓存(业绩预告/快报/增减持)
_JSON_KINDS = ("fundamental", "announcement", "news", "ugc", "policy", "llm_cache",
               "board_membership")
_RAW_KINDS = _PARQUET_KINDS + _JSON_KINDS
_FLAT_KINDS = ("llm_cache",)   # 不按日期分区的 raw kind

_CODE_RE = re.compile(r"^\d{6}$")             # 个股记录文件名
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # 日期目录名 YYYY-MM-DD

# —— 编排期"当前运行日期":set 一次,本次所有写入落同一日期目录 ——
_ACTIVE_DATE: str | None = None


def set_active_date(date: str | None) -> None:
    """编排入口调用:设定本次运行的日期(所有 put_* 默认落此日期目录)。传 None 复位。"""
    global _ACTIVE_DATE
    _ACTIVE_DATE = date


def active_date() -> str | None:
    return _ACTIVE_DATE


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _write_date(date: str | None) -> str:
    """写入日期:显式 date > active_date() > 今天。"""
    return date or _ACTIVE_DATE or _today()


# —— 存储后端分发:分析侧(record/view/code_view)可切 DB;raw 恒走文件 ——
def _use_db() -> bool:
    return settings.STORE_BACKEND == "db"


def _db():
    from tools.store import backend_db
    return backend_db


def _latest_date(root: Path) -> str | None:
    """root 下最新的日期子目录名;无则 None。"""
    if not root.exists():
        return None
    dates = [p.name for p in root.iterdir() if p.is_dir() and _DATE_RE.match(p.name)]
    return max(dates) if dates else None


def _read_date(root: Path, date: str | None) -> str | None:
    """读取日期:显式具体日期直接用;None/"latest" → root 下最新日期(无则 None)。"""
    if date and date != "latest":
        return date
    return _latest_date(root)


def _date_dirs_desc(root: Path) -> list[str]:
    """root 下所有日期子目录名,降序(最新在前)。"""
    if not root.exists():
        return []
    return sorted((p.name for p in root.iterdir() if p.is_dir() and _DATE_RE.match(p.name)),
                  reverse=True)


def _resolve_read_date(root: Path, date: str | None, exists_fn) -> str | None:
    """按 (目标数据存在性) 解析读取日期。

    - 显式具体日期(非 None/"latest"):直接返回,不判断存在性(与原行为一致,
      存在性交由调用方后续 p.exists() 判定,保留原 FileNotFoundError 路径与报错信息)。
    - None/"latest":从最新日期倒序回退,返回**第一个 exists_fn(d) 为真**的日期
      (即"含该目标数据的最新日期");所有日期都无则返回 None(调用方抛 FileNotFoundError)。

    向后兼容:当最新日期就有该数据时,首个命中即最新日期,行为与旧的全局 latest 完全一致。
    性能:倒序遍历,命中即停,不全扫。
    """
    if date and date != "latest":
        return date
    for d in _date_dirs_desc(root):
        if exists_fn(d):
            return d
    return None


def list_dates(root: str = "analysis") -> list[str]:
    """列出某根(analysis/raw)下所有日期,升序。analysis 侧受 STORE_BACKEND 影响(raw 恒文件)。"""
    if root == "analysis" and _use_db():
        return _db().list_dates()
    return sorted(p.name for p in (_ANALYSIS_DIR if root == "analysis" else _RAW_DIR).iterdir()
                  if p.is_dir() and _DATE_RE.match(p.name)) if (
        _ANALYSIS_DIR if root == "analysis" else _RAW_DIR).exists() else []


# ————————————————————————————————————————————————
# 内部:路径 + 读写原语
# ————————————————————————————————————————————————
def _raw_path(kind: str, code: str, date: str | None) -> Path:
    if kind not in _RAW_KINDS:
        raise ValueError(f"未知 raw kind: {kind!r}(支持 {_RAW_KINDS})")
    ext = "parquet" if kind in _PARQUET_KINDS else "json"
    if kind in _FLAT_KINDS:                       # llm_cache 扁平,不分日期
        return _RAW_DIR / kind / f"{code}.{ext}"
    return _RAW_DIR / str(date) / kind / f"{code}.{ext}"


def _meta_path(kind: str, code: str, date: str | None) -> Path:
    """raw 采集元数据 sidecar(fetched_at/source/rows),与数据文件同目录。"""
    return _raw_path(kind, code, date).parent / f"{code}.meta.json"


def _record_path(code: str, date: str) -> Path:
    return _ANALYSIS_DIR / date / f"{code}.json"


def _view_path(name: str, date: str) -> Path:
    return _ANALYSIS_DIR / date / f"{name}.json"


def _code_view_path(name: str, code: str, date: str) -> Path:
    return _ANALYSIS_DIR / date / name / f"{code}.json"


def _read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _write_json(p: Path, obj) -> str:
    """原子写:先写同目录 .tmp,再 os.replace 覆盖目标(崩溃/并发不留半截文件)。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / (p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return str(p)


def _write_parquet(p: Path, df) -> str:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / (p.name + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, p)
    return str(p)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ————————————————————————————————————————————————
# raw 原始数据
# ————————————————————————————————————————————————
def get_raw(kind: str, code: str, date: str | None = "latest"):
    """读单票某类 raw。parquet→DataFrame,json→dict/list。缺数据抛 FileNotFoundError。"""
    if kind not in _RAW_KINDS:                    # 先校验 kind(未知 kind 抛 ValueError)
        raise ValueError(f"未知 raw kind: {kind!r}(支持 {_RAW_KINDS})")
    d = None if kind in _FLAT_KINDS else _resolve_read_date(
        _RAW_DIR, date, lambda dd: _raw_path(kind, code, dd).exists())
    if kind not in _FLAT_KINDS and d is None:
        raise FileNotFoundError(f"{code} 无 {kind} 原始数据(任何日期目录均无),请先采集")
    p = _raw_path(kind, code, d)
    if not p.exists():
        raise FileNotFoundError(f"{code} 无 {kind} 原始数据,请先采集: {p}")
    if kind in _PARQUET_KINDS:
        import pandas as pd
        return pd.read_parquet(p)
    return _read_json(p)


def put_raw(kind: str, code: str, payload, meta: dict | None = None,
            date: str | None = None) -> str:
    """写单票某类 raw(原子写)+ 旁写采集元数据 sidecar。返回数据文件路径。

    date 缺省取 active_date()/今天(llm_cache 恒扁平不分日期)。
    meta 可含 source;fetched_at 自动补、rows 自动测(可被 meta 覆盖)。
    """
    d = None if kind in _FLAT_KINDS else _write_date(date)
    p = _raw_path(kind, code, d)
    if kind in _PARQUET_KINDS:
        data_path = _write_parquet(p, payload)
    else:
        data_path = _write_json(p, payload)
    _write_meta(kind, code, d, payload, meta)
    return data_path


def _write_meta(kind: str, code: str, date: str | None, payload, meta: dict | None) -> None:
    m = {"fetched_at": _now_iso(), "kind": kind, "code": code}
    try:
        m["rows"] = len(payload)
    except TypeError:
        pass
    if meta:
        m.update(meta)
    _write_json(_meta_path(kind, code, date), m)


def get_raw_meta(kind: str, code: str, date: str | None = "latest") -> dict | None:
    """读某票某类 raw 的采集元数据;无 sidecar 返回 None(advisory)。

    "latest" 同 get_raw:回退到含该 (kind, code) meta 的最新日期(与数据同目录、
    随 put_raw 一起写),使新鲜度判断锚定实际读到的那一日数据。
    """
    d = None if kind in _FLAT_KINDS else _resolve_read_date(
        _RAW_DIR, date, lambda dd: _meta_path(kind, code, dd).exists())
    if kind not in _FLAT_KINDS and d is None:
        return None
    p = _meta_path(kind, code, d)
    return _read_json(p) if p.exists() else None


def raw_age_days(kind: str, code: str, date: str | None = "latest") -> float | None:
    """raw 数据距上次采集的天数;无元数据/不可解析返回 None。"""
    m = get_raw_meta(kind, code, date)
    if not m or not m.get("fetched_at"):
        return None
    try:
        t = datetime.fromisoformat(m["fetched_at"])
    except ValueError:
        return None
    now = datetime.now(t.tzinfo) if t.tzinfo else datetime.now()
    return (now - t).total_seconds() / 86400.0


def is_stale(kind: str, code: str, max_days: float, date: str | None = "latest") -> bool:
    """数据是否超 max_days 未更新;无数据/无元数据一律视为陈旧(促重采)。"""
    age = raw_age_days(kind, code, date)
    return age is None or age > max_days


# ————————————————————————————————————————————————
# 滚动主档 data/master/kline/<code>.parquet
# 每股一份长历史(date 升序、前复权),不按日期分区 → 跨交易日增量 append,
# 根治按日期分区的跨日返工。列 schema 由采集层决定(store 只透传)。
# ————————————————————————————————————————————————
def _master_path(code: str) -> Path:
    if not _CODE_RE.match(code):
        raise ValueError(f"主档 code 需为 6 位数字: {code!r}")
    return _MASTER_DIR / "kline" / f"{code}.parquet"


def _master_meta_path(code: str) -> Path:
    return _master_path(code).parent / f"{code}.meta.json"


def has_master_kline(code: str) -> bool:
    """主档是否存在该股。"""
    return _master_path(code).exists()


def get_master_kline(code: str):
    """读单票主档 K线(全历史,date 升序)。缺失抛 FileNotFoundError。"""
    p = _master_path(code)
    if not p.exists():
        raise FileNotFoundError(f"{code} 无主档 K线,请先落地主档: {p}")
    import pandas as pd
    return pd.read_parquet(p)


def put_master_kline(code: str, df, meta: dict | None = None) -> str:
    """全量覆盖写单票主档(原子写)+ meta sidecar。df 需含 date 列。

    写入前按 date 去重(保留最后一条)+ 升序,保证主档规整。返回数据文件路径。
    """
    import pandas as pd
    df = _dedup_sort_by_date(df)
    p = _master_path(code)
    data_path = _write_parquet(p, df)
    _write_master_meta(code, df, meta)
    return data_path


def append_master_kline(code: str, df_new, meta: dict | None = None) -> str:
    """增量 append 到主档:与现有合并 → 按 date 去重(同日以新数据覆盖,幂等)→
    升序 → 原子写。主档不存在时等价于首次落地。返回数据文件路径。

    幂等性:盘中/盘后多次跑同一天,同 date 只保留最后写入的一条,不产生重复行。
    """
    import pandas as pd
    p = _master_path(code)
    if p.exists():
        old = pd.read_parquet(p)
        merged = pd.concat([old, df_new], ignore_index=True)
    else:
        merged = df_new
    return put_master_kline(code, merged, meta)


def put_master_daily_basic(date: str, df) -> str:
    """写全市场某交易日 daily_basic 快照（换手率、流通股本）。"""
    day = str(date)[:10]
    return _write_parquet(_MASTER_DAILY_BASIC_DIR / f"{day}.parquet", df)


def get_master_daily_basic(date: str):
    """读全市场某交易日 daily_basic 快照；缺失抛 FileNotFoundError。"""
    p = _MASTER_DAILY_BASIC_DIR / f"{str(date)[:10]}.parquet"
    if not p.exists():
        raise FileNotFoundError(f"无 daily_basic 快照: {p}")
    import pandas as pd
    return pd.read_parquet(p)


def _dedup_sort_by_date(df):
    """按 date 去重(保留最后一条,即同日新覆盖旧)+ 升序 + 重置索引。"""
    import pandas as pd
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"])
    out = (out.drop_duplicates(subset=["date"], keep="last")
              .sort_values("date").reset_index(drop=True))
    return out


def _write_master_meta(code: str, df, meta: dict | None) -> None:
    m = {"fetched_at": _now_iso(), "code": code}
    try:
        m["rows"] = int(len(df))
        if len(df):
            import pandas as pd
            d = pd.to_datetime(df["date"])
            m["first_date"] = d.min().strftime("%Y-%m-%d")
            m["last_date"] = d.max().strftime("%Y-%m-%d")
    except (TypeError, KeyError):
        pass
    if meta:
        m.update(meta)
    _write_json(_master_meta_path(code), m)


def get_master_kline_meta(code: str) -> dict | None:
    """读主档 meta(fetched_at/rows/first_date/last_date/source);无则 None。"""
    p = _master_meta_path(code)
    return _read_json(p) if p.exists() else None


def list_master_codes() -> list[str]:
    """列出主档已落地的所有股票代码(升序)。"""
    root = _MASTER_DIR / "kline"
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.parquet") if _CODE_RE.match(p.stem))


# ————————————————————————————————————————————————
# 中心记录 data/analysis/<日期>/{code}.json
# ————————————————————————————————————————————————
def get_record(code: str, date: str | None = "latest") -> dict:
    """读单票中心记录(缺省最新日期)。缺文件抛 FileNotFoundError。"""
    if _use_db():
        return _db().get_record(code, date)
    d = _resolve_read_date(_ANALYSIS_DIR, date, lambda dd: _record_path(code, dd).exists())
    if d is None:
        raise FileNotFoundError(f"{code} 无结构化记录(任何日期目录均无),请先 serialize")
    p = _record_path(code, d)
    if not p.exists():
        raise FileNotFoundError(f"{code} 无结构化记录,请先 serialize: {p}")
    return _read_json(p)


def put_record(rec: dict, date: str | None = None) -> str:
    """写中心记录(用 rec['meta']['code'] 定文件名)。缺 code 抛 ValueError。返回路径。"""
    if _use_db():
        return _db().put_record(rec, _write_date(date))
    code = (rec.get("meta") or {}).get("code")
    if not code:
        raise ValueError("记录缺 meta.code,无法确定文件名")
    return _write_json(_record_path(str(code), _write_date(date)), rec)


def iter_records(date: str | None = "latest"):
    """遍历某日期(缺省最新)下所有个股中心记录,yield 记录 dict。

    仅 yield 文件名为 6 位代码的 json,自动排除 panel/screen 等视图文件。
    无任何日期目录时直接返回(空)。
    """
    if _use_db():
        yield from _db().iter_records(date)
        return
    d = _read_date(_ANALYSIS_DIR, date)
    if d is None:
        return
    for p in sorted((_ANALYSIS_DIR / d).glob("*.json")):
        if _CODE_RE.match(p.stem):
            yield _read_json(p)


def delete_stock(code: str) -> list[str]:
    """删除某票的全部落盘(遍历所有日期分区):raw 各 kind 数据+采集元数据、
    中心记录、按票视图(chart/sentiment 等)。

    供票池「删除」操作清理该票缓存(store 拥有文件布局,删除归此层)。
    视图(panel/screen)是全池聚合,由上层重建覆盖,不在此删。
    返回实际删除的文件路径列表(用于日志/回执)。
    """
    removed: list[str] = []

    def _rm(p: Path) -> None:
        if p.exists():
            p.unlink()
            removed.append(str(p))

    # 滚动主档(每股一份长历史)
    _rm(_master_path(code))
    _rm(_master_meta_path(code))
    # raw:扁平 kind(llm_cache)无日期;其余遍历所有日期分区
    for kind in _RAW_KINDS:
        if kind in _FLAT_KINDS:
            _rm(_raw_path(kind, code, None))
            _rm(_meta_path(kind, code, None))
        else:
            for d in list_dates("raw"):
                _rm(_raw_path(kind, code, d))
                _rm(_meta_path(kind, code, d))
    # 中心记录 + 按票视图(chart/sentiment…):DB 后端删库行,文件后端删各日期分区文件
    if _use_db():
        removed += _db().delete_stock(code)
    else:
        for d in list_dates("analysis"):
            _rm(_record_path(code, d))
            daydir = _ANALYSIS_DIR / d
            if daydir.exists():
                for sub in daydir.iterdir():
                    if sub.is_dir():
                        _rm(sub / f"{code}.json")
    return removed


# ————————————————————————————————————————————————
# 视图 data/analysis/<日期>/{name}.json(panel / screen 等)
# ————————————————————————————————————————————————
def get_view(name: str, date: str | None = "latest"):
    """读视图对象(如 panel/screen,缺省最新日期)。缺文件抛 FileNotFoundError。"""
    if _use_db():
        return _db().get_view(name, date)
    d = _resolve_read_date(_ANALYSIS_DIR, date, lambda dd: _view_path(name, dd).exists())
    if d is None:
        raise FileNotFoundError(f"无视图 {name}(任何日期目录均无),请先生成")
    p = _view_path(name, d)
    if not p.exists():
        raise FileNotFoundError(f"无视图 {name},请先生成: {p}")
    return _read_json(p)


def put_view(name: str, obj, date: str | None = None) -> str:
    """写视图对象(缺省当前运行日期)。返回路径。"""
    if _use_db():
        return _db().put_view(name, obj, _write_date(date))
    return _write_json(_view_path(name, _write_date(date)), obj)


# ————————————————————————————————————————————————
# 按票视图 data/analysis/<日期>/<name>/{code}.json(chart / sentiment 等)
# ————————————————————————————————————————————————
def put_code_view(name: str, code: str, obj, date: str | None = None) -> str:
    """写按票视图(如 chart/sentiment,缺省当前运行日期)。返回路径。"""
    if _use_db():
        return _db().put_code_view(name, code, obj, _write_date(date))
    return _write_json(_code_view_path(name, code, _write_date(date)), obj)


def get_code_view(name: str, code: str, date: str | None = "latest") -> dict:
    """读按票视图(缺省最新日期)。缺文件抛 FileNotFoundError。"""
    if _use_db():
        return _db().get_code_view(name, code, date)
    d = _resolve_read_date(_ANALYSIS_DIR, date,
                           lambda dd: _code_view_path(name, code, dd).exists())
    if d is None:
        raise FileNotFoundError(f"{code} 无 {name} 视图(任何日期目录均无)")
    p = _code_view_path(name, code, d)
    if not p.exists():
        raise FileNotFoundError(f"{code} 无 {name} 视图: {p}")
    return _read_json(p)
