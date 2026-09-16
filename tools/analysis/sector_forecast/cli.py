"""板块预测系统 CLI(P1)。

用法:
    python -m tools.analysis.sector_forecast --date 2026-09-15            # 面板+角色表
    python -m tools.analysis.sector_forecast --date 2026-09-15 --regime-only
    python -m tools.analysis.sector_forecast                             # 默认今天

产出:
    data/analysis/<date>/sector_regime.json   板块环境面板
    data/sector_roster/<板块>.json            四角色主表(--regime-only 时跳过)

**防未来函数**:--date 非今天 = 历史补跑,只读本地 master kline 的该日行。
⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime


def _setup_logging() -> None:
    from tools.config import settings
    logdir = settings.PROJECT_ROOT / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(logdir / "sector_forecast.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.handlers = [fh, sh]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="板块预测系统 P1:板块环境面板 + 四角色主表")
    ap.add_argument("--date", default=None, help="口径日 YYYY-MM-DD(默认今天)")
    ap.add_argument("--regime-only", action="store_true", help="只出板块面板,跳过角色表/两步/日报")
    ap.add_argument("--no-report", action="store_true", help="出面板+角色+focus,但不渲染每日文档")
    ap.add_argument("--sectors", default=None,
                    help="逗号分隔的目标板块(申万一级);缺省 = 种子清单")
    args = ap.parse_args(argv)
    _setup_logging()
    log = logging.getLogger("sector_forecast.cli")

    date = args.date or datetime.now().strftime("%Y-%m-%d")
    try:
        from tools.collectors import calendar as cal
        if not cal.is_trading_day(date):
            log.info("跳过:%s 非 A 股交易日", date)
            return 0
    except Exception:
        pass

    from tools.analysis.sector_forecast import regime_panel as RP
    from tools.analysis.sector_forecast import universe as U

    # 一次全A加载,面板与角色共用
    frame = U.load_sector_frame(date)
    if frame.empty:
        log.error("全A截面为空(该日无K线?),不落盘、非0退出")
        return 1

    # 面板只构一次(温度计较重),面板/角色/focus/日报全复用
    panel = RP.build_sector_regime(date, frame=frame)
    out = RP.write_sector_regime(date, panel=panel)
    log.info("板块面板 → %s", out)
    if args.regime_only:
        return 0

    from tools.analysis.sector_forecast import roster as RO
    sectors = [s.strip() for s in args.sectors.split(",")] if args.sectors else None
    paths = RO.build_all_rosters(date, frame=frame, sectors=sectors)
    log.info("角色主表 → %d 个板块落盘 data/sector_roster/", len(paths))

    from tools.analysis.sector_forecast import focus as F
    fpath = F.write_focus(date, panel=panel)
    log.info("重点板块池 → %s", fpath)

    if not args.no_report:
        from tools.analysis.sector_forecast import daily_report as DR
        mp, jp = DR.write_report(date, panel=panel)
        log.info("每日板块文档 → %s", mp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
