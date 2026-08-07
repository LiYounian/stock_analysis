"""把 data/analysis/<日期>/ 的分析产物导入 store 的 DB 后端(只搬运,不重算)。

用途:展示端「只读、不算、不触网」,但页面要**从数据库取数展示**。本地端算好的
文件产物随 git 带到展示端后,本工具把它一次性灌进 DB(幂等 upsert),之后 web 以
`STORE_BACKEND=db` 直接读库。数据更新 = 展示端 `git pull` 后重跑本工具即可。

只读 data/analysis 下的 json 文件 + 调 tools.store.backend_db 公开 API 落库;
不采集、不调 LLM、不改 store。目标库由 settings.DB_URL 决定(缺省单文件 SQLite)。

产物 → 表 的映射(与文件后端布局一一对应):
  <日期>/<6位代码>.json      → 中心记录 record
  <日期>/<其它名>.json       → 池级视图 view(panel / screen / sentiment_policy…)
  <日期>/<name>/<代码>.json  → 按票视图 code_view(chart / sentiment / news_ai…)
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

from tools.config import settings
from tools.store import backend_db

logger = logging.getLogger("sync.import")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")   # 日期目录
_CODE_RE = re.compile(r"^\d{6}$")                # 6 位股票代码


def _analysis_dir() -> Path:
    return settings.PROJECT_ROOT / "data" / "analysis"


def _redact_db_url(url: str) -> str:
    """日志脱敏:只留连接方案,隐去可能含账号密码的部分(如 PG/MySQL 连接串)。"""
    return url.split("://", 1)[0] if "://" in url else "unknown"


def list_dates(analysis_dir: Path) -> list[str]:
    """analysis 根下所有形如 YYYY-MM-DD 的日期目录,升序。"""
    if not analysis_dir.exists():
        return []
    return sorted(p.name for p in analysis_dir.iterdir()
                  if p.is_dir() and _DATE_RE.match(p.name))


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def import_date(analysis_dir: Path, date: str) -> dict:
    """导入某一天的全部产物到 DB(幂等)。返回 records/views/code_views 计数。"""
    payload = collect_date(analysis_dir, date)
    for code, rec in payload["records"].items():
        backend_db.put_record(rec, date)
    for name, obj in payload["views"].items():
        backend_db.put_view(name, obj, date)
    for name, per_code in payload["code_views"].items():
        for code, obj in per_code.items():
            backend_db.put_code_view(name, code, obj, date)
    counts = {"records": len(payload["records"]), "views": len(payload["views"]),
              "code_views": sum(len(v) for v in payload["code_views"].values())}
    logger.info("导入 %s:记录 %d / 视图 %d / 按票视图 %d",
                date, counts["records"], counts["views"], counts["code_views"])
    return counts


def collect_date(analysis_dir: Path, date: str) -> dict:
    """把某日产物读成内存结构(不落库),供导入器与上传工具共用枚举口径:
      {"records": {code: rec}, "views": {name: obj}, "code_views": {name: {code: obj}}}
    顶层 6 位 json=record,其余=view;子目录 <name>/<code>.json=code_view。
    """
    day = analysis_dir / date
    out: dict = {"records": {}, "views": {}, "code_views": {}}
    if not day.is_dir():
        return out
    for p in sorted(day.glob("*.json")):
        obj = _load(p)
        if _CODE_RE.match(p.stem):
            obj.setdefault("meta", {}).setdefault("code", p.stem)   # 兜底补 code
            out["records"][p.stem] = obj
        else:
            out["views"][p.stem] = obj
    for sub in sorted(day.iterdir()):
        if not sub.is_dir():
            continue
        per_code = {q.stem: _load(q) for q in sorted(sub.glob("*.json")) if _CODE_RE.match(q.stem)}
        if per_code:
            out["code_views"][sub.name] = per_code
    return out


def import_all(analysis_dir: Path | None = None, only_date: str | None = None) -> dict:
    """导入全部(或指定)日期到 DB。返回汇总计数。"""
    analysis_dir = analysis_dir or _analysis_dir()
    dates = [only_date] if only_date else list_dates(analysis_dir)
    total = {"dates": 0, "records": 0, "views": 0, "code_views": 0}
    for d in dates:
        if not (analysis_dir / d).is_dir():
            logger.warning("跳过:日期目录不存在 %s", d)
            continue
        c = import_date(analysis_dir, d)
        total["dates"] += 1
        for k in ("records", "views", "code_views"):
            total[k] += c[k]
    logger.info("导入完成:%s", total)
    return total


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="把 data/analysis 文件产物导入 store DB 后端(只搬运,不重算)")
    ap.add_argument("--date", help="只导入某一天 YYYY-MM-DD;缺省导入全部日期")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logger.info("目标库后端=%s", _redact_db_url(settings.DB_URL))
    total = import_all(only_date=args.date)
    print(f"导入完成:{total}")


if __name__ == "__main__":
    main()
