"""日内实时观测循环(盯候选+持仓,阈值触发买/卖倾向信号)。

## 为什么有这个模块(2026-09-07)

日内超短线循环下午要持续盯"午盘初选的 ~5 只候选 + 当前持仓",价量越过阈值时记一条
**倾向信号**(买入倾向/卖出倾向/放量关注),供 ~14:30 定稿"买/持/卖"参考。
调研结论(见设计 §1/§4):免费 A 股 L1 无 WebSocket,盯几只用 **HTTP 轮询每 3–5s** 即足够且最简
(5 只一次请求 ~0.1s),复用 `tools.collectors.gtimg_quote.fetch_quotes`。

## 契约

  · **信号是"倾向/关注",不自动成交**——成交在尾盘定稿后由 position_ledger 记账(研究模拟)。
  · 规则判定(`evaluate_quote`)是**纯函数**,循环(`run_watch`)只做取数+判定+落事件,便于离线单测。
  · **防未来函数**:只用当前报价;`quote_time` **未推进**(同一时刻,如非交易时段静态值)→ 不触发,
    避免拿收盘静态值反复误触发(实测:非交易时段源方返回冻结值、quote_time 不变)。
  · **去重(迟滞)**:同一 (code, 规则, 方向) 一个观测段内只触发一次,须先回落到阈值内才允许再触发,
    不让贴着阈值的抖动刷屏。
  · 产出:`data/intraday/<date>/watch_events.jsonl`,每行一条触发事件(追加写)。

⚠️ 测试环境研究用,非投资建议;只读行情、不下单。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from tools.collectors import gtimg_quote
from tools.config import settings

logger = logging.getLogger("pipeline.intraday_watch")

OUT_ROOT = settings.PROJECT_ROOT / "data" / "intraday"

# 方向枚举(供下游定稿映射到 买入/持有/卖出;倾向≠成交)
BUY = "买入倾向"
SELL = "卖出倾向"
WATCH = "放量关注"


@dataclass
class WatchConfig:
    """阈值口径(保守默认版;前向迭代)。ref_price = 午盘参考价(相对它算急拉/急跌)。"""
    up_pct: float = 7.0          # 当日涨幅 ≥ 此 → 买入倾向(强势确认,接近涨停)
    down_pct: float = -5.0       # 当日涨幅 ≤ 此 → 卖出倾向(止损预警)
    surge_pct: float = 3.0       # 相对午盘参考价急拉 ≥ 此 → 买入倾向
    plunge_pct: float = -3.0     # 相对午盘参考价急跌 ≤ 此 → 卖出倾向
    vol_ratio_min: float = 2.0   # 量比 ≥ 此 → 放量关注
    interval_s: float = 4.0      # 轮询间隔秒(3–5s;贴源方刷新节奏)


@dataclass
class WatchState:
    """跨轮状态:每 code 上次 quote_time(判静态)+ 已触发键集合(迟滞去重)。"""
    last_quote_time: dict[str, str] = field(default_factory=dict)
    fired: set = field(default_factory=set)          # {(code, rule, direction)}


# ────────────────────────────── 纯函数:规则判定 ──────────────────────────────

def evaluate_quote(code: str, q: dict, ref_price: float | None,
                   cfg: WatchConfig) -> list[dict]:
    """对单只当前报价套阈值,返回命中的规则事件(不含时间戳/去重,循环层补)。

    每条事件:{code, rule, direction, price, pct_chg, vol_ratio, quote_time}。
    价缺 → 空(缺失不猜)。ref_price 缺 → 跳过急拉/急跌两条(不假造参考)。
    """
    price = q.get("price")
    if price is None:
        return []
    pct = q.get("pct_chg")
    vr = q.get("vol_ratio")
    qt = q.get("quote_time")
    hits: list[dict] = []

    def _add(rule: str, direction: str):
        hits.append({"code": code, "rule": rule, "direction": direction,
                     "price": price, "pct_chg": pct, "vol_ratio": vr, "quote_time": qt})

    if pct is not None and pct >= cfg.up_pct:
        _add("涨幅突破", BUY)
    if pct is not None and pct <= cfg.down_pct:
        _add("跌幅预警", SELL)
    if ref_price not in (None, 0) and price is not None:
        move = (price / ref_price - 1.0) * 100.0
        if move >= cfg.surge_pct:
            _add("急拉", BUY)
        if move <= cfg.plunge_pct:
            _add("急跌", SELL)
    if vr is not None and vr >= cfg.vol_ratio_min:
        _add("放量", WATCH)
    return hits


def filter_new_events(state: WatchState, code: str, quote_time: str | None,
                      events: list[dict], all_rules: list[str] | None = None) -> list[dict]:
    """迟滞去重 + 防未来:quote_time 未推进 → 全部丢弃;已触发过的 (code,rule,dir) 丢弃;
    本轮**未命中**的规则从 fired 里清除(回落后允许再次触发)。返回本轮应落盘的新事件。
    """
    # 防未来/静态值:源方时刻没走 → 这一轮不算新数据,不触发
    if quote_time is not None and state.last_quote_time.get(code) == quote_time:
        return []
    if quote_time is not None:
        state.last_quote_time[code] = quote_time

    hit_keys = {(code, e["rule"], e["direction"]) for e in events}
    # 回落:本轮没命中的旧键清除,允许下次再触发(迟滞)
    for key in [k for k in state.fired if k[0] == code and k not in hit_keys]:
        state.fired.discard(key)

    new: list[dict] = []
    for e in events:
        key = (code, e["rule"], e["direction"])
        if key in state.fired:
            continue                      # 已触发、未回落 → 不重复
        state.fired.add(key)
        new.append(e)
    return new


# ────────────────────────────── 落盘 ──────────────────────────────

def events_path(date: str) -> Path:
    return OUT_ROOT / date / "watch_events.jsonl"


def append_events(date: str, events: list[dict]) -> None:
    """追加触发事件到 jsonl(每行一条,带 emitted_at 真实时刻)。"""
    if not events:
        return
    p = events_path(date)
    p.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with open(p, "a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps({"emitted_at": now, **e}, ensure_ascii=False) + "\n")


# ────────────────────────────── 循环 ──────────────────────────────

def watch_once(codes: list[str], ref_prices: dict[str, float], cfg: WatchConfig,
               state: WatchState, *,
               quote_fn: Callable[[list[str]], dict[str, dict]] = gtimg_quote.fetch_quotes
               ) -> list[dict]:
    """拉一轮报价 → 判定 → 去重,返回本轮新触发事件(不落盘,由 run_watch 落)。"""
    quotes = quote_fn(codes)
    new: list[dict] = []
    for code in codes:
        q = quotes.get(code)
        if not q:
            continue
        events = evaluate_quote(code, q, ref_prices.get(code), cfg)
        new.extend(filter_new_events(state, code, q.get("quote_time"), events))
    return new


def run_watch(codes: list[str], ref_prices: dict[str, float] | None = None, *,
              date: str | None = None, cfg: WatchConfig | None = None,
              max_iters: int | None = None, stop_at: datetime | None = None,
              now_fn: Callable[[], datetime] = lambda: datetime.now().astimezone(),
              quote_fn: Callable[[list[str]], dict[str, dict]] = gtimg_quote.fetch_quotes,
              sleep_fn: Callable[[float], None] = time.sleep) -> int:
    """观测循环:每 interval 轮询、判定、落新事件。返回累计触发条数。

    停止条件(任一):`max_iters` 轮数到 / `stop_at` 墙钟到(如 14:57 收盘前)。两者皆 None=外部停。
    `now_fn`/`sleep_fn`/`quote_fn` 可注入(hermetic)。交易时段判定由 launchd 排期在外层保证。
    """
    date = date or datetime.now().strftime("%Y-%m-%d")
    cfg = cfg or WatchConfig()
    ref_prices = ref_prices or {}
    state = WatchState()
    total = 0
    i = 0
    while max_iters is None or i < max_iters:
        if stop_at is not None and now_fn() >= stop_at:
            logger.info("到达收盘前停止点 %s,观测结束", stop_at.isoformat(timespec="minutes"))
            break
        try:
            new = watch_once(codes, ref_prices, cfg, state, quote_fn=quote_fn)
        except Exception as e:                    # 单轮网络异常不终止循环,记日志、下轮再试
            logger.warning("观测轮 %d 拉取失败,跳过:%s: %s", i, type(e).__name__, e)
            new = []
        if new:
            append_events(date, new)
            total += len(new)
            for e in new:
                logger.info("触发 %s %s %s 价=%s 涨幅=%s%%", e["code"], e["rule"],
                            e["direction"], e["price"], e["pct_chg"])
        i += 1
        if max_iters is None or i < max_iters:
            sleep_fn(cfg.interval_s)
    return total


# ────────────────────────────── 午休参考价 ──────────────────────────────

def load_ref_prices(date: str, slot: str = "1145") -> dict[str, float]:
    """从午休快照 `data/intraday/<date>/T<slot>.json` 读 {code: 价} 作急拉/急跌参考。

    快照缺失 → 空 dict(急拉/急跌两条规则本轮不判,不假造参考,见 evaluate_quote)。
    """
    p = OUT_ROOT / date / f"T{slot}.json"
    if not p.exists():
        logger.warning("午休快照不存在 %s,急拉/急跌无参考价", p)
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("午休快照解析失败 %s:%s", p, e)
        return {}
    out: dict[str, float] = {}
    for code, q in (data.get("codes") or {}).items():
        price = q.get("price")
        if price is not None:
            out[code] = float(price)
    return out


# ────────────────────────────── CLI ──────────────────────────────

def _setup_logging() -> None:
    log_path = settings.PROJECT_ROOT / "logs" / "intraday_watch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()):
        h.setFormatter(fmt)
        root.addHandler(h)


def main(argv: list[str] | None = None) -> int:
    """CLI:盯 = --codes(午盘候选)∪ 当前持仓(读账本);急拉/急跌参考价读午休快照。

        python -m tools.pipeline.intraday_watch --codes 300308,002234 --max-iters 900
    """
    import argparse

    from tools.pipeline import intraday_snapshot as snap
    from tools.pipeline import position_ledger as pl

    ap = argparse.ArgumentParser(description="日内实时观测循环(盯候选+持仓,阈值触发倾向信号)")
    ap.add_argument("--codes", default="", help="午盘候选代码,逗号分隔(缺省时从当日 日内_<date>.md 解析)")
    ap.add_argument("--date", default=None, help="日期 YYYY-MM-DD(默认今天)")
    ap.add_argument("--slot", default="1145", help="午休参考快照 slot(默认 1145)")
    ap.add_argument("--interval", type=float, default=4.0, help="轮询间隔秒(默认 4)")
    ap.add_argument("--max-iters", type=int, default=None, help="最大轮数(默认到外部停)")
    ap.add_argument("--until", default="14:57", help="收盘前停止点 HH:MM(默认 14:57;空串=不设)")
    args = ap.parse_args(argv)
    _setup_logging()

    date = args.date or datetime.now().strftime("%Y-%m-%d")
    cand = [c.strip() for c in args.codes.split(",") if c.strip()]
    if not cand:                       # 缺省:从当日午盘选股 md 解析候选(launchd 无参起动用)
        pick_md = snap.PICK_DIR / f"日内_{date}.md"
        cand = snap.parse_pick_codes(pick_md)
        if cand:
            logger.info("从 %s 解析出候选 %d 只", pick_md, len(cand))
    held = pl.list_open_codes(pl.load())
    codes = list(dict.fromkeys([*cand, *held]))
    if not codes:
        logger.warning("无候选也无持仓,观测循环无事可盯,退出")
        return 0
    ref = load_ref_prices(date, args.slot)
    stop_at = None
    if args.until.strip():
        hh, mm = args.until.split(":")
        stop_at = datetime.strptime(f"{date} {hh}:{mm}", "%Y-%m-%d %H:%M").astimezone()
    logger.info("观测启动:%d 只(候选 %d ∪ 持仓 %d),参考价 %d 只,间隔 %ss,停止点 %s",
                len(codes), len(cand), len(held), len(ref), args.interval, args.until or "无")
    cfg = WatchConfig(interval_s=args.interval)
    total = run_watch(codes, ref, date=date, cfg=cfg, max_iters=args.max_iters, stop_at=stop_at)
    logger.info("观测结束:累计触发 %d 条", total)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
