"""模拟持仓账本(日内超短线循环的**持仓状态 + 买/持/卖记分地基**)。

## 为什么有这个模块(2026-09-07)

日内超短线循环每天对每个标的给 `买入/持有/卖出`,而"持有/卖出"判断必须有锚——
"我现在持有哪些、成本多少、持有第几个交易日、浮盈多少、相对全A等权的超额多少"。
项目此前**只选股不记仓**,缺这一层。本模块补上:一份可移交的模拟持仓账本
(研究记账,非真实交易),由尾盘执行写入、次日午盘复盘读取。
设计见 docs/计划/2026-09-07_日内超短线循环_设计.md §5。

## 契约

账本文件 `data/analysis/positions.json`:
    version/updated_at
    open   —— 持有中 [{code,name,open_date,open_price,open_bench,
                        last_date,last_price,days_held,pnl_pct,alpha_pct,status,note}]
    closed —— 已平仓 [{... , close_date,close_price,close_bench,
                        realized_pnl_pct,realized_alpha_pct,close_reason}]

纪律:
  · **α 基准 = 全A等权净值指数点位**(`open_bench`/当前 `bench`),由**调用方注入**
    (来自 `cross_section_stats` 的 `mean_pct` 链成的等权净值)。账本只做算术、不取数,
    保持纯函数、可离线单测。**基准缺失 → alpha_pct = None(不假造 0/"中性")**(经验#11/#17)。
  · **α 口径**:基准涨幅% = (bench_now/open_bench − 1)×100;alpha = 个股涨幅% − 基准涨幅%;
    个股与基准必须取**同一时点**的值(调用方保证配对,见设计 §6)。
  · **防未来函数**:mark/close 传入的 price、bench 一律为 ≤ 决策时点的值,不得回填更晚数据。
  · **幂等/原子**:save 先写 .tmp 再 rename;重复开同一 code 的仓 → 拒绝(ValueError),
    不静默叠加(超短线一码一仓)。
  · **持有天数按交易日计**(`count_trading_days`,走交易日历;可 monkeypatch 测)。

⚠️ 测试环境研究记账,非投资建议;不下单、不接券商。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.collectors import calendar as cal
from tools.config import settings

logger = logging.getLogger("pipeline.position_ledger")

LEDGER_VERSION = "1.0.0"
LEDGER_PATH = settings.PROJECT_ROOT / "data" / "analysis" / "positions.json"


# ────────────────────────────── 读写 ──────────────────────────────

def _empty_ledger() -> dict:
    return {"version": LEDGER_VERSION, "updated_at": None, "open": [], "closed": []}


def load(path: str | Path = LEDGER_PATH) -> dict:
    """读账本;文件不存在/损坏 → 返回空账本(不阻断,首次运行即空)。"""
    p = Path(path)
    if not p.exists():
        return _empty_ledger()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("账本损坏(%s),按空账本处理:%s", p, e)
        return _empty_ledger()
    data.setdefault("open", [])
    data.setdefault("closed", [])
    data.setdefault("version", LEDGER_VERSION)
    return data


def save(ledger: dict, path: str | Path = LEDGER_PATH) -> None:
    """原子落盘(先 .tmp 再 rename,避免下游读到写一半的 JSON)。"""
    p = Path(path)
    ledger["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# ────────────────────────────── 查询 ──────────────────────────────

def find_open(ledger: dict, code: str) -> dict | None:
    """返回持仓中该 code 的记录(引用,可原地改);无 → None。"""
    for pos in ledger.get("open", []):
        if pos.get("code") == code:
            return pos
    return None


def list_open_codes(ledger: dict) -> list[str]:
    """当前持仓的代码列表(供实时观测/复盘拿到"要盯哪些")。"""
    return [p["code"] for p in ledger.get("open", [])]


# ────────────────────────────── 交易日计数 ──────────────────────────────

def count_trading_days(open_date: str, date: str) -> int:
    """[open_date, date] 之间的交易日数(含两端;买入当日 = 第 1 个交易日)。

    日历不可用 → 回退按自然日 +1 近似(记 warning)。可 monkeypatch 做 hermetic 测试。
    """
    if date < open_date:
        return 0
    try:
        dates = sorted(d for d in cal.trading_dates() if open_date <= d <= date)
    except Exception as e:
        logger.warning("交易日历异常(%s),持有天数按自然日近似", e)
        dates = []
    if dates:
        return len(dates)
    d0 = datetime.strptime(open_date, "%Y-%m-%d")
    d1 = datetime.strptime(date, "%Y-%m-%d")
    return (d1 - d0).days + 1


# ────────────────────────────── α 算术 ──────────────────────────────

def _pct(now: float, base: float) -> float | None:
    """(now/base − 1)×100;base 无效 → None。"""
    if base in (None, 0) or now is None:
        return None
    return (now / base - 1.0) * 100.0


def _alpha(pnl_pct: float | None, open_bench: float | None, bench: float | None) -> float | None:
    """alpha = 个股涨幅% − 基准涨幅%;基准任一端缺 → None(不假造)。"""
    bench_ret = _pct(bench, open_bench) if (open_bench is not None and bench is not None) else None
    if pnl_pct is None or bench_ret is None:
        return None
    return pnl_pct - bench_ret


# ────────────────────────────── 开/标记/平 ──────────────────────────────

def open_position(ledger: dict, *, code: str, name: str, date: str, price: float,
                  bench: float | None = None, note: str = "") -> dict:
    """建仓(尾盘模拟成交)。已持有同 code → ValueError(一码一仓,不叠加)。

    `bench` = 建仓时点全A等权净值指数点位(调用方注入);缺则 alpha 后续为 None。
    """
    if find_open(ledger, code) is not None:
        raise ValueError(f"已持有 {code},不重复建仓(先平再开)")
    pos = {
        "code": code, "name": name,
        "open_date": date, "open_price": float(price),
        "open_bench": (float(bench) if bench is not None else None),
        "last_date": date, "last_price": float(price),
        "days_held": count_trading_days(date, date),   # 建仓当日 = 第 1 个交易日
        "pnl_pct": 0.0,
        "alpha_pct": 0.0 if bench is not None else None,
        "status": "持有中",
        "note": note,
    }
    ledger.setdefault("open", []).append(pos)
    return pos


def mark_to_market(ledger: dict, *, date: str, quotes: dict[str, dict],
                   bench: float | None = None) -> list[str]:
    """按当前报价更新所有持仓的浮盈/超额/持有天数。返回本次更新到的 code 列表。

    `quotes` = {code: {price, ...}}(如 gtimg_quote.fetch_quotes 的返回);缺报价的持仓
    只更新 days_held、保留上次价(记 stale,不假造)。`bench` = 当前时点全A等权净值点位。
    """
    updated: list[str] = []
    for pos in ledger.get("open", []):
        pos["days_held"] = count_trading_days(pos["open_date"], date)
        q = quotes.get(pos["code"])
        price = q.get("price") if q else None
        if price is None:
            pos["stale"] = True
            continue
        pos.pop("stale", None)
        pos["last_date"] = date
        pos["last_price"] = float(price)
        pos["pnl_pct"] = _pct(float(price), pos["open_price"])
        pos["alpha_pct"] = _alpha(pos["pnl_pct"], pos.get("open_bench"), bench)
        updated.append(pos["code"])
    return updated


def close_position(ledger: dict, *, code: str, date: str, price: float,
                   bench: float | None = None, reason: str = "") -> dict:
    """平仓(尾盘模拟成交)。把持仓移入 closed 并结算实现收益/超额。未持有 → ValueError。"""
    pos = find_open(ledger, code)
    if pos is None:
        raise ValueError(f"未持有 {code},无法平仓")
    ledger["open"].remove(pos)
    realized_pnl = _pct(float(price), pos["open_price"])
    realized_alpha = _alpha(realized_pnl, pos.get("open_bench"), bench)
    pos.update({
        "close_date": date, "close_price": float(price),
        "close_bench": (float(bench) if bench is not None else None),
        "days_held": count_trading_days(pos["open_date"], date),
        "realized_pnl_pct": realized_pnl,
        "realized_alpha_pct": realized_alpha,
        "close_reason": reason,
        "status": "已平仓",
    })
    ledger.setdefault("closed", []).append(pos)
    return pos


# ────────────────────────────── 日内记分卡 ──────────────────────────────

def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def summarize(ledger: dict, *, min_n: int = 1) -> dict:
    """已平仓 → 记分卡:笔数/胜率/中位收益/中位超额/盈亏比/平均持有天数。

    · 超额统计只计 `realized_alpha_pct` 非 None 的笔(基准缺失的不掺进来,不假造);
    · 样本 < min_n → `insufficient=True`(前向积累未够,别过早下结论)。
    """
    closed = ledger.get("closed", [])
    n = len(closed)
    if n < min_n:
        return {"n": n, "insufficient": True, "min_n": min_n}
    pnls = [c["realized_pnl_pct"] for c in closed if c.get("realized_pnl_pct") is not None]
    alphas = [c["realized_alpha_pct"] for c in closed if c.get("realized_alpha_pct") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    pl_ratio = (avg_win / abs(avg_loss)) if (avg_win is not None and avg_loss) else None
    days = [c["days_held"] for c in closed if c.get("days_held") is not None]
    return {
        "n": n,
        "insufficient": False,
        "win_rate": (len(wins) / len(pnls)) if pnls else None,
        "median_pnl_pct": _median(pnls),
        "median_alpha_pct": _median(alphas),      # 非 None 超额的中位;无则 None
        "alpha_n": len(alphas),
        "profit_loss_ratio": pl_ratio,
        "avg_days_held": (sum(days) / len(days)) if days else None,
    }


# ────────────────────────────── CLI ──────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """CLI:查看/记账(尾盘执行写入,复盘读数)。

        python -m tools.pipeline.position_ledger show
        python -m tools.pipeline.position_ledger summary
        python -m tools.pipeline.position_ledger open  --code 300308 --name 中际旭创 \
            --date 2026-09-07 --price 12.34 [--bench 1000.0] [--note 尾盘建仓]
        python -m tools.pipeline.position_ledger close --code 300308 \
            --date 2026-09-08 --price 13.00 [--bench 1005.0] [--reason 止盈]
    """
    import argparse

    ap = argparse.ArgumentParser(description="模拟持仓账本(日内超短线,研究记账)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="打印当前持仓 + 记分卡")
    sub.add_parser("summary", help="只打印记分卡 JSON")
    for name in ("open", "close"):
        sp = sub.add_parser(name, help=f"{name} 一笔(尾盘模拟成交)")
        sp.add_argument("--code", required=True)
        sp.add_argument("--date", required=True)
        sp.add_argument("--price", type=float, required=True)
        sp.add_argument("--bench", type=float, default=None, help="该时点全A等权净值点位(缺则alpha=null)")
        if name == "open":
            sp.add_argument("--name", default="")
            sp.add_argument("--note", default="")
        else:
            sp.add_argument("--reason", default="")
    args = ap.parse_args(argv)

    led = load()
    if args.cmd == "summary":
        print(json.dumps(summarize(led), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "show":
        print(json.dumps({"open": led["open"], "scorecard": summarize(led)},
                         ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "open":
        pos = open_position(led, code=args.code, name=args.name, date=args.date,
                            price=args.price, bench=args.bench, note=args.note)
    else:
        pos = close_position(led, code=args.code, date=args.date, price=args.price,
                             bench=args.bench, reason=args.reason)
    save(led)
    print(json.dumps(pos, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
