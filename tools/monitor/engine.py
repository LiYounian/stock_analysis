"""② 轮询引擎:拉 gtimg → 求值触发 → emit Alert → notifier 分发。

- 定向清单:只轮询 watchlist 里的 code(十几只=1个URL),**永不全A**。
- 求值:evaluate() 是纯函数(item, quote, prev)→[Alert],单测友好、与轮询解耦。
- 去抖:once(每 code+trigger 日内一次) + cross_*(需上轮值判穿越)。
- 交易时段闸门:非交易日/午休静默;时刻类闸门(收盘了结)到点触发。
⚠️ 只拉行情、只弹提示,不下单。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

from tools.collectors import calendar as cal
from tools.collectors import gtimg_quote
from tools.monitor.notify import Notifier, get_notifier
from tools.monitor.schema import CST, WATCH_DIR, Alert, Trigger, WatchItem, Watchlist

logger = logging.getLogger("monitor.engine")

# A股连续竞价时段(集合竞价简化并入)。
_AM = (dtime(9, 25), dtime(11, 30))
_PM = (dtime(13, 0), dtime(15, 0))


def in_trading_session(now: Optional[datetime] = None) -> bool:
    """当前是否处于交易时段(交易日 且 在 9:25-11:30 / 13:00-15:00)。"""
    now = now or datetime.now(CST)
    if not cal.is_trading_day(now.strftime("%Y-%m-%d")):
        return False
    t = now.timetz().replace(tzinfo=None)
    return (_AM[0] <= t <= _AM[1]) or (_PM[0] <= t <= _PM[1])


def _cmp(op: str, cur: float, thr: float, prev: Optional[float]) -> bool:
    if op == "<=":
        return cur <= thr
    if op == ">=":
        return cur >= thr
    if op == "<":
        return cur < thr
    if op == ">":
        return cur > thr
    if op == "cross_down":                 # 上轮在阈上、本轮到/破阈下
        return prev is not None and prev > thr >= cur
    if op == "cross_up":
        return prev is not None and prev < thr <= cur
    return False


def _time_hit(at: str, now: datetime) -> bool:
    try:
        hh, mm = (int(x) for x in at.split(":"))
    except Exception:
        return False
    t = now.timetz().replace(tzinfo=None)
    return t >= dtime(hh, mm)


def evaluate(item: WatchItem, quote: dict, prev: Optional[dict] = None,
             *, now: Optional[datetime] = None) -> list[Alert]:
    """对单票求值所有触发,返回命中的 Alert(不含去抖;去抖在 run 里按 fired 集判)。纯函数。"""
    now = now or datetime.now(CST)
    alerts: list[Alert] = []
    price = quote.get("price") if quote else None
    for cond in item.all_conditions():
        hit = False
        if cond.kind == "time":
            hit = bool(cond.at) and _time_hit(cond.at, now)
        else:
            cur = quote.get(cond.field) if quote else None
            if cur is None or cond.value is None:
                continue
            pv = prev.get(cond.field) if prev else None
            hit = _cmp(cond.op, cur, cond.value, pv)
        if hit:
            alerts.append(Alert(
                code=item.code, name=item.name, trigger_id=cond.id, kind=cond.kind,
                action=cond.action, price=price, value=cond.value, fired_at=now.isoformat(timespec="seconds"),
            ))
    return alerts


def _alerts_path(date: str, root: Optional[Path] = None) -> Path:
    return (root or WATCH_DIR) / f"{date}_alerts.jsonl"


def _load_fired(date: str, root: Optional[Path] = None) -> set[tuple[str, str]]:
    """回放当日 alerts.jsonl 恢复已 fire 的 (code,trigger_id),防进程重启后重弹。"""
    p = _alerts_path(date, root)
    fired: set[tuple[str, str]] = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                fired.add((d["code"], d["trigger_id"]))
            except Exception:
                continue
    return fired


def _append_alert(date: str, alert: Alert, root: Optional[Path] = None) -> None:
    p = _alerts_path(date, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(alert.to_dict(), ensure_ascii=False) + "\n")


def run(watchlist: Watchlist, notifier: Optional[Notifier] = None, *,
        interval: float = 4.0, root: Optional[Path] = None,
        max_rounds: Optional[int] = None, respect_session: bool = True) -> None:
    """盯当日 watchlist。max_rounds/respect_session 供测试与联调(生产缺省常驻+守时段)。"""
    notifier = notifier or get_notifier()
    codes = watchlist.codes()
    if not codes:
        logger.warning("watchlist 为空,无可盯标的")
        return
    fired = _load_fired(watchlist.date, root)          # 去抖:已 fire 集(once)
    prev: dict[str, dict] = {}
    rounds = 0
    logger.info("盯盘启动:%d 只 | interval=%ss | notifier=%s", len(codes), interval, notifier.name)
    while True:
        if respect_session and not in_trading_session():
            logger.debug("非交易时段,静默")
            time.sleep(min(interval * 5, 30))
            if max_rounds is not None:
                rounds += 1
                if rounds >= max_rounds:
                    break
            continue
        try:
            quotes = gtimg_quote.fetch_quotes(codes)
        except Exception as e:
            logger.warning("行情拉取失败(退避重试):%s", e)
            time.sleep(min(interval * 3, 15))
            continue
        for it in watchlist.items:
            q = quotes.get(it.code)
            if not q:
                continue                                # 停牌/缺失:不误触发
            for alert in evaluate(it, q, prev.get(it.code)):
                key = (alert.code, alert.trigger_id)
                cond = next((c for c in it.all_conditions() if c.id == alert.trigger_id), None)
                if cond and cond.once and key in fired:
                    continue                            # 去抖:本日已弹过
                notifier.notify(alert)
                _append_alert(watchlist.date, alert, root)
                if cond and cond.once:
                    fired.add(key)
            prev[it.code] = q
        rounds += 1
        if max_rounds is not None and rounds >= max_rounds:
            break
        time.sleep(interval)
