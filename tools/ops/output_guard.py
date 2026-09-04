"""产出缺口护栏:断言关键策略视图当日已落盘,缺口显式告警(不静默)。

为什么有这个护栏(实测教训):趋势跟随口径(SEPA+VCP)是本项目**唯一**趋势跟随策略,其余偏
均值回归/位置/量价。它曾在 2026-08-28、2026-09-03 两次**静默零产出**——根因不是崩溃、不是调度
没触发,而是**日期漂移**:盘后 pull_refresh 是长跑任务,机器睡眠唤醒 / 跨午夜续跑时,SEPA 步用
wall-clock `today()` 定 as_of,把当日产物写进了 D+1 目录,而 $D 当天目录里 SEPA 三视图为空。
其余尾部步(forecast/upload)都吃钉死的 $D,唯独 SEPA 没钉,于是缺口被 40MB 长日志淹没、无人察觉。

治本已在生产侧钉死 $D(见 ops/launchd/pull_refresh.sh ②.6);本护栏是**第二道防线**——即便未来
再有任一环节漂移/漏跑,当日缺口也必须**留痕、可 grep、可监控**,而不是又一次静默失效。

契约(与确定性节点同纪律「文件存在 ⇒ 内容可用」呼应):
  · 非交易日 → ok=True,跳过(不告警);
  · 每个必达视图须 `data/analysis/<date>/<view>.json` 存在、可解析、且内部 `as_of == date`
    (`as_of` 不符 = 正是漂移那类 bug,视为缺失);
  · 缺任一 → ok=False;CLI 退出码 2(非零=告警),打印 `!!! 护栏告警` 明细;
  · `--marker` 时把缺口明细写 `data/analysis/<date>/_GAP_ALARM.json`(marker 自证,便于监控扫描)。

CLI:
  python -m tools.ops.output_guard --date 2026-09-03            # 核验;缺失退 2
  python -m tools.ops.output_guard --date 2026-09-03 --marker   # 缺失额外写 _GAP_ALARM.json
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

from tools.collectors import calendar as trade_cal
from tools.config import settings

logger = logging.getLogger("ops.output_guard")

# 必达视图:趋势跟随口径三视图(合格池 / 观察池 / 雷达)。唯一趋势策略,缺一即缺口。
DEFAULT_REQUIRED = ("SEPA合格池", "SEPA观察池", "SEPA雷达")

MARKER_NAME = "_GAP_ALARM.json"


def _analysis_root() -> Path:
    return settings.PROJECT_ROOT / "data" / "analysis"


def _view_status(view: str, date: str, analysis_dir: Path) -> dict:
    """单个视图核验结果:{view, ok, reason, path}。reason 仅在缺失时有意义。"""
    path = analysis_dir / date / f"{view}.json"
    if not path.exists():
        return {"view": view, "ok": False, "reason": "文件不存在", "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"view": view, "ok": False, "reason": f"JSON 解析失败:{e}", "path": str(path)}
    as_of = payload.get("as_of")
    if as_of != date:
        # 文件在但 as_of 不符 = 日期漂移的指纹:别把它当"已产出"放过。
        return {"view": view, "ok": False,
                "reason": f"as_of 不符(期望 {date},实际 {as_of})——疑似日期漂移",
                "path": str(path)}
    return {"view": view, "ok": True, "reason": "", "path": str(path)}


def check_outputs(date: str, *, analysis_dir: Path | None = None,
                  required=DEFAULT_REQUIRED, trading_day: bool | None = None) -> dict:
    """核验 date 当日必达视图是否齐全。

    trading_day=None → 自查交易日历(不可用时回退周一~周五并 WARNING)。非交易日直接 ok=True 跳过。
    返回 {date, ok, skipped, trading_day, checked:[...], missing:[...]}。best-effort:不抛异常。
    """
    analysis_dir = analysis_dir or _analysis_root()
    if trading_day is None:
        try:
            trading_day = trade_cal.is_trading_day(date)
        except Exception as e:  # noqa: BLE001
            logger.warning("交易日历不可用,回退周一~周五近似判定:%s", e)
            wd = datetime.strptime(date, "%Y-%m-%d").weekday()
            trading_day = wd < 5

    if not trading_day:
        return {"date": date, "ok": True, "skipped": True, "trading_day": False,
                "checked": [], "missing": []}

    checked = [_view_status(v, date, analysis_dir) for v in required]
    missing = [c for c in checked if not c["ok"]]
    return {"date": date, "ok": not missing, "skipped": False, "trading_day": True,
            "checked": checked, "missing": missing}


def write_marker(report: dict, *, analysis_dir: Path | None = None) -> Path | None:
    """缺口时把明细写 data/analysis/<date>/_GAP_ALARM.json(marker 自证)。齐全则不写、返回 None。"""
    if report.get("ok"):
        return None
    analysis_dir = analysis_dir or _analysis_root()
    day_dir = analysis_dir / report["date"]
    day_dir.mkdir(parents=True, exist_ok=True)
    marker = day_dir / MARKER_NAME
    payload = {
        "alarm": "产出缺口:当日趋势跟随口径(SEPA)视图缺失",
        "date": report["date"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "missing": [{"view": m["view"], "reason": m["reason"]} for m in report["missing"]],
        "note": "根因通常是日期漂移(定时任务跨午夜/机器睡眠唤醒后用 wall-clock 写到 D+1);"
                "见 tools/ops/output_guard.py 顶部与 ops/launchd/pull_refresh.sh ②.6。",
    }
    marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return marker


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="产出缺口护栏:核验当日必达策略视图,缺口告警")
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                    help="核验日期 YYYY-MM-DD(默认今天)")
    ap.add_argument("--views", help="逗号分隔的必达视图名(默认 SEPA 三视图)")
    ap.add_argument("--marker", action="store_true", help="缺口时写 _GAP_ALARM.json marker")
    a = ap.parse_args(argv)

    required = tuple(v.strip() for v in a.views.split(",") if v.strip()) if a.views else DEFAULT_REQUIRED
    report = check_outputs(a.date, required=required)

    if report["skipped"]:
        print(f"产出缺口护栏:{a.date} 非交易日,跳过核验")
        return 0
    if report["ok"]:
        print(f"产出缺口护栏:{a.date} 趋势跟随口径视图齐全 ✅（{', '.join(required)}）")
        return 0

    # 缺口:显式告警(不静默)。
    print(f"!!! 护栏告警:{a.date} 趋势跟随口径(SEPA)产出缺口——以下视图缺失/异常:")
    for m in report["missing"]:
        print(f"    · {m['view']}:{m['reason']}  ({m['path']})")
    if a.marker:
        marker = write_marker(report)
        if marker:
            print(f"    → 已写缺口 marker:{marker}")
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
