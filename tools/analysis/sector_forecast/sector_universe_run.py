"""S1 · 板块 universe 周度 runner——纯量价、无 LLM。

设计:docs/计划/2026-09-16_消息板块选股_程序化流水线_设计.md §1 S1。
每周一次维护"当前该有哪些板块 + 哪些热门",产 data/analysis/sector_universe.json
(全申万一级 + 热度标记 + 与上周 diff)+ history 归档留痕。

用法:python -m tools.analysis.sector_forecast.sector_universe_run [--date YYYY-MM-DD]
非交易日自动回退到 ≤date 最近交易日(周度任务可挂周一/周五 EOD)。
⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta


def _setup_logging() -> None:
    from tools.config import settings
    logdir = settings.PROJECT_ROOT / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(logdir / "sector_universe.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers = [fh, logging.StreamHandler()]


def _resolve_trading_day(date: str) -> str:
    """≤date 最近交易日;日历取不到 → 原样返回(不阻断)。"""
    try:
        from tools.collectors import calendar as cal
        d = datetime.strptime(date, "%Y-%m-%d")
        for _ in range(10):
            s = d.strftime("%Y-%m-%d")
            if cal.is_trading_day(s):
                return s
            d -= timedelta(days=1)
    except Exception:
        pass
    return date


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="S1 板块 universe 周度维护 runner")
    ap.add_argument("--date", default=None, help="口径日 YYYY-MM-DD(默认今天;非交易日回退最近交易日)")
    args = ap.parse_args(argv)
    _setup_logging()
    log = logging.getLogger("sector_forecast.sector_universe_run")

    date = _resolve_trading_day(args.date or datetime.now().strftime("%Y-%m-%d"))
    from tools.analysis.sector_forecast import sector_universe as SU
    payload = SU.build_sector_universe(date)
    if payload.get("n_板块", 0) == 0:
        log.error("全A截面为空(该日无K线?),不落盘、非0退出:%s", date)
        return 1
    out = SU.write_sector_universe(date, payload=payload)
    d = payload.get("与上周diff", {})
    log.info("S1 完成 → %s(%d板块/%d热门;基线%s 转热%s 转冷%s)",
             out, payload["n_板块"], payload["n_热门"],
             d.get("基线"), d.get("转热"), d.get("转冷"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
