"""把 `data/reports/选股分析/*.md` 定向分析报告发布进 store 容器视图「选股分析报告」,
随"按日期上传"同步到远端(ingest→DB),使远端 web 的【选股分析】页也能展示。

设计:单容器视图 `选股分析报告` = {"reports": {name: {name,title,date,md}}}。
  - 一个 `__view__:选股分析报告` 分片,体积小(单份 md ~20K),复用现成上传/签名/断点续传。
  - web 层 `data_access` 本地优先读文件、否则读该容器视图 → 远端(DB 后端、无本地 md 文件)也能展示。
  - 容器累积:发布时先读最新容器,合并本次报告,再写到指定日期(默认取报告名里的日期)。

用法:
  python -m tools.sync.publish_report                 # 发布该目录下全部 .md
  python -m tools.sync.publish_report 选股分析_20260813  # 只发布指定报告(名或路径)
  之后 `python -m tools.sync.upload --date <报告日期>` 把该日 views(含本容器)上传远端。
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from tools.config import settings
from tools.store import repo as store

REPORT_DIR = settings.PROJECT_ROOT / "data" / "reports" / "选股分析"
VIEW_NAME = "选股分析报告"


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip().lstrip("⚠️ ").strip()
    return fallback


def _date_of(stem: str) -> str:
    m = re.search(r"(20\d{6})", stem)
    return f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}" if m else ""


def _resolve(files: list[str] | None) -> list[Path]:
    if not files:
        return sorted(REPORT_DIR.glob("*.md"))
    out = []
    for f in files:
        p = Path(f)
        if not p.exists():                       # 传的是报告名(stem)而非路径
            p = REPORT_DIR / (p.name if p.suffix == ".md" else p.name + ".md")
        out.append(p)
    return out


def publish(files: list[str] | None = None, date: str | None = None) -> str:
    """把报告并入容器视图并落库(供后续 upload 同步)。返回写入的日期。"""
    try:
        container = store.get_view(VIEW_NAME) or {}
    except FileNotFoundError:
        container = {}
    reports = dict(container.get("reports", {}))
    published = []
    for p in _resolve(files):
        if not p.exists():
            print(f"!! 跳过(不存在):{p}")
            continue
        text = p.read_text(encoding="utf-8")
        name = p.stem
        reports[name] = {"name": name, "title": _title(text, name),
                         "date": _date_of(name), "md": text}
        published.append(name)
    if not published:
        print("!! 无可发布报告")
        return ""
    # 写入日期:优先入参;否则取本批报告里最新日期(让容器随该日 analysis 一起被 upload 收集)
    d = date or max((reports[n]["date"] for n in published if reports[n]["date"]), default=None)
    store.put_view(VIEW_NAME, {"reports": reports}, date=d)
    print(f"已发布 {len(published)} 份到视图「{VIEW_NAME}」(date={d}):{published}")
    print(f"下一步同步远端:python -m tools.sync.upload --date {d}")
    return d or ""


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="发布选股分析报告到 store 容器视图(供上传同步远端)")
    ap.add_argument("reports", nargs="*", help="报告名或 .md 路径(默认全部)")
    ap.add_argument("--date", help="写入的分析日期 YYYY-MM-DD(默认取报告名里的日期)")
    a = ap.parse_args(argv)
    publish(a.reports or None, a.date)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
