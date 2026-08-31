# ⚠️ 已下线(存档保留):2026-09-01 起,策略D_自选池小市值组合(web 策略5)从生产/展示摘除。
#    原因:从未前瞻回测 / 未证明有效 / 无明确用途。本算子代码不删,仍在 registry 注册、单测照常绿;
#    仅不再被 web/pipeline 调用。如需恢复见 docs/日志/开发日志.md。
"""小市值组合选股(移植自聚宽社区「价值选股与RSRS择时」)。

原脚本核心 = 小市值选股 + 每周三调仓 + 复合止损 + 空仓月切黄金 ETF。持仓管理/止损/
调仓 = 回测器/主观决策的职责(见 8650081 剥离规矩),这里只提炼可分析层复用的选股。

主流程:限池 → 板块头过滤 → 按 valuation.mktcap_yi 升序 → 月度池(top_k×20)→
候选池(top_k×3) → top_k;涨跌停/停牌/次新过滤 + 空仓月 embargo 标记(不代买 ETF)。

    策略C_小市值组合(全 A + 中小板指历史成分股票池):
      限 config/small_cap_universe.json 池 + 剥 30/68/8/4/9 + 次新过滤开启。

    策略D_自选池小市值组合(自选池):
      records 已由自选池过滤;不限池、不剥板块头、次新过滤关闭(自选长期持有,
      不可能是次新);涨跌停/停牌/embargo 与 C 一致;默认 top_k=3。
"""
from __future__ import annotations

import json
from datetime import date, datetime

from tools.config import settings
from tools.strategy.registry import strategy

_UNIVERSE_PATH = settings.PROJECT_ROOT / "config" / "small_cap_universe.json"
_LIMIT_PCT_THRESHOLD = 9.7           # |pct_chg|≥9.7 视为触板(002/003 深主板中小 10%)
_MIN_KLINE_DAYS = 120                # 次新:上市 <120 交易日(与原脚本一致)
_MONTHLY_POOL_MULT = 20              # 月度池 = top_k × 20
_CANDIDATE_POOL_MULT = 3             # 候选池 = top_k × 3


def _load_universe() -> set[str]:
    """中小板指历史成分股票池;缺文件 → 空 set(降级为不限池,策略D 同款语义)。"""
    try:
        codes = json.loads(_UNIVERSE_PATH.read_text("utf-8"))
        return set(codes) if isinstance(codes, list) else set()
    except FileNotFoundError:
        return set()


def _code_head_excluded(code: str) -> bool:
    """原脚本 filter_stocks 剥离:创业(30)/科创(68)/北交(8/4)/B或退(9)。"""
    return code.startswith(("30", "68", "8", "4", "9"))


def _default_kline_loader(code: str):
    try:
        from tools.collectors import market
        return market.load_kline(code)
    except Exception:
        return None


def is_embargo_month(as_of: date | datetime | None = None) -> bool:
    """空仓月判定,与原脚本 today_is_tradable 反义严格对齐(12-22~1-28 与 3-20~4-28)。"""
    dt = as_of or datetime.today()
    mon, day = dt.month, dt.day
    return (
        (mon == 12 and day >= 22) or (mon == 1 and day <= 28) or
        (mon == 3 and day >= 20) or (mon == 4 and day <= 28)
    )


def _pass_filters(code: str, rec: dict, *, kline_loader,
                  check_new_listing: bool) -> bool:
    """业务过滤:未停牌(snapshot 存在)+ 未触涨跌停 + (可选)非次新。"""
    snap = (rec or {}).get("snapshot")
    if not snap:
        return False
    pct = snap.get("pct_chg")
    if isinstance(pct, (int, float)) and abs(pct) >= _LIMIT_PCT_THRESHOLD:
        return False
    if check_new_listing:
        kline = kline_loader(code)
        if kline is None or len(kline) < _MIN_KLINE_DAYS:
            return False
    return True


def _run_small_cap(records, *, top_k, as_of, kline_loader, universe,
                   exclude_board_head, check_new_listing) -> dict:
    loader = kline_loader if kline_loader is not None else _default_kline_loader

    scoped: list[tuple[str, float]] = []
    for code, rec in (records or {}).items():
        if universe and code not in universe:
            continue
        if exclude_board_head and _code_head_excluded(code):
            continue
        mktcap = ((rec or {}).get("valuation") or {}).get("mktcap_yi")
        if not isinstance(mktcap, (int, float)) or mktcap <= 0:
            continue
        scoped.append((code, float(mktcap)))

    embargo = is_embargo_month(as_of)
    if not scoped:
        return {"codes": [], "candidates": [], "embargo": embargo, "top_k": top_k,
                "monthly_pool_size": 0}

    scoped.sort(key=lambda kv: kv[1])
    monthly_pool = [c for c, _ in scoped[: top_k * _MONTHLY_POOL_MULT]]
    filtered = [c for c in monthly_pool
                if _pass_filters(c, records[c], kline_loader=loader,
                                 check_new_listing=check_new_listing)]
    candidates = filtered[: top_k * _CANDIDATE_POOL_MULT]
    return {
        "codes": candidates[:top_k],
        "candidates": candidates,
        "embargo": embargo,
        "top_k": top_k,
        "monthly_pool_size": len(monthly_pool),
    }


_SCHEMA_COMMON = {
    "records": "dict[code, 中心记录]",
    "top_k": "目标持仓数",
    "as_of": "评估日期(date/datetime),用于判空仓月;缺省=今天",
    "kline_loader": "可选 code→DataFrame,默认走 market.load_kline",
}


@strategy("策略C_小市值组合", "选股", params_schema=_SCHEMA_COMMON)
def combo_small_cap_screen(records, top_k=5, as_of=None, kline_loader=None) -> dict:
    """限中小板指历史成分池 + 剥 30/68/8/4/9 + 次新过滤开启。"""
    return _run_small_cap(records, top_k=top_k, as_of=as_of,
                          kline_loader=kline_loader,
                          universe=_load_universe(),
                          exclude_board_head=True,
                          check_new_listing=True)


@strategy("策略D_自选池小市值组合", "选股", params_schema=_SCHEMA_COMMON)
def combo_watchlist_small_cap_screen(records, top_k=3, as_of=None, kline_loader=None) -> dict:
    """自选池小市值:不限池、不剥板块头、关次新过滤;涨跌停/停牌/embargo 同 C。"""
    return _run_small_cap(records, top_k=top_k, as_of=as_of,
                          kline_loader=kline_loader,
                          universe=None,
                          exclude_board_head=False,
                          check_new_listing=False)
