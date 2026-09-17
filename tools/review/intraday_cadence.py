"""午盘/日内选股复盘的**隔夜 cadence 变体**（当天收盘买 → 次日午盘前卖）。

与尾盘大复盘（model_a.py：限价=D收盘·D+1回踩成交·D+2条件持有）**彻底解耦**：本模块是干净的
**隔夜单段**——D 收盘**市价买入必成交** → 次日 11:30 全A午休冻结快照价卖出，绝对收益 + α。
不复用 model_a 的 r_exit/hold_to_d2/stop_flag 逻辑；`Pick`/归因/基准仍复用尾盘基建。

权威口径：docs/计划/2026-09-17_午盘日内选股复盘_隔夜口径_设计.md（统筹审批：卖=11:30全A冻结快照代理
"次日午盘前~11:00"；买=D收盘价，与尾盘 Model-A / Agent 实验窗买入口径同源、跨版可比）。

## 价源（as-of 防未来红线：买只取 D 当日行、卖只取 D+1 已落盘快照/行，>卖点的 bar 绝不进计算）
- **买入价** = master 日线 `close`@D（全覆盖、天然 ≤D）。
- **卖出价**（分级，绝不用 D+1 收盘顶替——那会破坏"早盘卖"语义）：
  1. `data/intraday/<D+1>/noon_screen_snapshot.json` → `quotes[code].price`（全A 11:30 冻结，全覆盖）
  2. `data/intraday/<D+1>/T1145.json` → `codes[code].price`（11:45 定向 watchlist，覆盖窄）
  3. master 日线 `open`@D+1（次日开盘近似，降级标 `open_degraded`，口径漂移解读降权）
  4. 都缺 → r_overnight=None、status=pending（有声缺失，可回填幂等覆盖）
- **α 基准** = 全A等权同期隔夜。D+1 noon_screen 快照里每只 `pct_chg` 恰是「gtimg D收盘→D+1 11:30」
  的隔夜收益 → `breadth.equal_weight_mean_pct([pct_chg...])`（唯一真源、全A覆盖、零额外 IO）。
  快照缺 → 基准 None → α=null 有声缺失（绝不顶替/编）。

⚠️ 测试环境研究模拟，**非投资建议**。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from tools.analysis.market_forecast import breadth as B
from tools.review.loaders import _resolve_root
from tools.review.types import Pick

logger = logging.getLogger("review.intraday_cadence")

NOON_SCREEN_NAME = "noon_screen_snapshot.json"   # 全A 11:30 冻结（首选卖出价源）
T1145_NAME = "T1145.json"                         # 11:45 定向快照（次选）

# 卖出价源标签（冻结：下游/测试逐字引用）
SRC_NOON = "noon_screen"        # 全A 11:30 冻结快照价（标准口径）
SRC_T1145 = "t1145"             # 11:45 定向快照价
SRC_OPEN_DEG = "open_degraded"  # 次日开盘价降级（口径漂移，解读降权）


@dataclass
class OvernightLabels:
    """隔夜 cadence 事后收益标签（本线唯一收益口径）。未知/未到期/算不到 → None（有声缺失，不猜）。"""

    buy_price: Optional[float] = None      # 买入价 = D 收盘价
    sell_price: Optional[float] = None     # 卖出价 = D+1 早盘快照价（分级）
    sell_src: Optional[str] = None         # SRC_NOON | SRC_T1145 | SRC_OPEN_DEG | None
    r_overnight: Optional[float] = None    # 隔夜绝对收益% = (卖−买)/买×100
    market_overnight: Optional[float] = None  # 全A等权同期隔夜% (基准)
    alpha_overnight: Optional[float] = None   # r_overnight − 全A等权隔夜
    win: Optional[bool] = None             # r_overnight>0；None=pending/缺失（剔出胜率分母）
    degraded: bool = False                 # 卖价走 open 降级口径（解读降权）
    status: str = "pending"                # settled | degraded | pending | no_data
    note: Optional[str] = None


# ───────────────────── K 线 / 快照 读取（缓存复用）─────────────────────
def _kline(code: str, cache: dict) -> Optional[pd.DataFrame]:
    """master 日线（含 date/open/close）。缺档/异常 → None（有声缺失）。跨 pick 复用缓存。"""
    if code in cache:
        return cache[code]
    from tools.collectors import market
    try:
        df = market.load_kline(code)
    except Exception as e:  # noqa: BLE001 - 缺档/异常保守降级
        logger.debug("load_kline 失败 %s：%s", code, e)
        df = None
    if df is not None and ("date" not in getattr(df, "columns", []) or df.empty):
        df = None
    cache[code] = df
    return df


def _col_on(df: Optional[pd.DataFrame], date: str, col: str) -> Optional[float]:
    """日线里 date==当日行的某列（防未来：只取该日行，不碰其后行）。NaN/缺 → None。"""
    if df is None or col not in df.columns:
        return None
    row = df[pd.to_datetime(df["date"]) == pd.Timestamp(date)]
    if row.empty:
        return None
    v = row.iloc[-1][col]
    return None if pd.isna(v) else float(v)


def _snapshot_dir(date: str, data_root: str | None) -> Path:
    """<data_root>/intraday/<date>/（worktree 下 _resolve_root 指主仓 data/）。"""
    return _resolve_root(data_root) / "intraday" / date


def load_noon_screen(date: str, data_root: str | None, cache: dict) -> dict:
    """D 日全A 11:30 冻结快照 → {code: {price, pct_chg, open}}。缺文件 → {}。跨调用缓存。"""
    key = ("noon", date)
    if key in cache:
        return cache[key]
    path = _snapshot_dir(date, data_root) / NOON_SCREEN_NAME
    out: dict = {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        out = {c: q for c, q in (payload.get("quotes") or {}).items()
               if q.get("price") is not None}
    except FileNotFoundError:
        logger.debug("noon_screen 快照缺：%s", path)
    except (ValueError, OSError) as e:
        logger.warning("读 noon_screen 快照失败 %s（跳过）：%s", path, e)
    cache[key] = out
    return out


def load_t1145(date: str, data_root: str | None, cache: dict) -> dict:
    """D 日 11:45 定向快照 → {code: {price, ...}}。缺文件 → {}。跨调用缓存。"""
    key = ("t1145", date)
    if key in cache:
        return cache[key]
    path = _snapshot_dir(date, data_root) / T1145_NAME
    out: dict = {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        out = {c: q for c, q in (payload.get("codes") or {}).items()
               if q.get("price") is not None}
    except FileNotFoundError:
        logger.debug("T1145 快照缺：%s", path)
    except (ValueError, OSError) as e:
        logger.warning("读 T1145 快照失败 %s（跳过）：%s", path, e)
    cache[key] = out
    return out


# ───────────────────── 买 / 卖 / 收益 / 基准 ─────────────────────
def buy_price(code: str, d: str, kline_cache: dict) -> Optional[float]:
    """买入价 = D 收盘价（master 日线 close@D）。缺 → None。"""
    return _col_on(_kline(code, kline_cache), d, "close")


def sell_price(code: str, d1: str, data_root: str | None,
               snap_cache: dict, kline_cache: dict) -> tuple[Optional[float], Optional[str]]:
    """次日卖出价（分级）。返回 (price, src)。全缺 → (None, None)。

    绝不用 D+1 收盘（那会破坏"早盘卖"语义）。open 降级会标 SRC_OPEN_DEG。
    """
    noon = load_noon_screen(d1, data_root, snap_cache)
    q = noon.get(code)
    if q and q.get("price") is not None:
        return float(q["price"]), SRC_NOON

    t1145 = load_t1145(d1, data_root, snap_cache)
    q = t1145.get(code)
    if q and q.get("price") is not None:
        return float(q["price"]), SRC_T1145

    o = _col_on(_kline(code, kline_cache), d1, "open")   # 降级：次日开盘近似早盘
    if o is not None:
        return o, SRC_OPEN_DEG
    return None, None


def overnight_return(buy: Optional[float], sell: Optional[float]) -> Optional[float]:
    """隔夜绝对收益% = (卖−买)/买×100。任一缺/买≤0 → None。"""
    if buy is None or sell is None or buy <= 0:
        return None
    return (sell / buy - 1.0) * 100.0


def market_overnight_ew(d1: str, data_root: str | None, snap_cache: dict) -> Optional[float]:
    """全A等权同期隔夜基准%：D+1 noon_screen 每只 pct_chg（=gtimg D收盘→D+1 11:30 隔夜收益）等权均值。

    复用 breadth.equal_weight_mean_pct（唯一真源）。快照缺/无有效 pct_chg → None（α 将 null 有声缺失）。
    """
    noon = load_noon_screen(d1, data_root, snap_cache)
    if not noon:
        return None
    pcts = [q.get("pct_chg") for q in noon.values() if isinstance(q.get("pct_chg"), (int, float))]
    if not pcts:
        return None
    val = B.equal_weight_mean_pct(pcts, total=len(pcts))
    return None if pd.isna(val) else float(val)


# ───────────────────── 单票 / 批量标签 ─────────────────────
def next_trading_day(d: str) -> str:
    """D → D+1 交易日（复用项目日历唯一真源）。取不到日历 → 抛（不臆造隔夜卖点）。"""
    from tools.collectors import calendar as cal
    return cal.next_trading_day(d)


def compute_labels(pick: Pick, kline_cache: dict, snap_cache: dict,
                   data_root: str | None, market_ew_cache: dict) -> OvernightLabels:
    """单 Pick → 隔夜收益标签。D=pick.date，D+1=下一交易日。缓存跨 pick 复用。"""
    d = pick.date
    lab = OvernightLabels()
    lab.buy_price = buy_price(pick.code, d, kline_cache)

    try:
        d1 = next_trading_day(d)
    except Exception as e:  # noqa: BLE001 - 无日历 → 卖点未知，pending
        logger.warning("取 %s 的下一交易日失败（pending）：%s", d, e)
        lab.status = "pending"
        lab.note = "下一交易日未知（日历缺）"
        return lab

    sell, src = sell_price(pick.code, d1, data_root, snap_cache, kline_cache)
    lab.sell_price, lab.sell_src = sell, src
    lab.degraded = (src == SRC_OPEN_DEG)

    lab.r_overnight = overnight_return(lab.buy_price, lab.sell_price)

    if d1 not in market_ew_cache:
        market_ew_cache[d1] = market_overnight_ew(d1, data_root, snap_cache)
    lab.market_overnight = market_ew_cache[d1]

    if lab.r_overnight is not None and lab.market_overnight is not None:
        lab.alpha_overnight = lab.r_overnight - lab.market_overnight

    # 状态机：卖价缺→pending（次日快照未出，可回填）；买价也缺→no_data；成交→settled/degraded
    if lab.r_overnight is None:
        lab.status = "no_data" if lab.buy_price is None else "pending"
        lab.win = None
        lab.note = lab.note or ("次日快照未出/未到期" if lab.buy_price is not None else "买价缺")
    else:
        lab.win = lab.r_overnight > 0
        lab.status = "degraded" if lab.degraded else "settled"
    return lab


@dataclass
class _AttrShim:
    """喂给尾盘 attribution.classify 的最小适配（隔夜 cadence 无回踩/未触发概念）。

    classify 只读 lab.untriggered / lab.runaway_up / lab.r_exit 三字段——隔夜市价买必成交
    → untriggered=False、runaway_up=False；r_exit=r_overnight（归因据其正负分选对/选错桶）。
    """

    untriggered: bool = False
    runaway_up: bool = False
    r_exit: Optional[float] = None


def to_attr_shim(lab: OvernightLabels) -> _AttrShim:
    """OvernightLabels → classify 可读的 shim（r_exit=隔夜收益）。复用尾盘规则归因，口径无关。"""
    return _AttrShim(untriggered=False, runaway_up=False, r_exit=lab.r_overnight)


def compute_all(picks: list[Pick], data_root: str | None = None) -> list[tuple[Pick, OvernightLabels]]:
    """批量：共享 kline / snapshot / 基准 缓存。返回 [(pick, labels)]。"""
    try:                                   # worktree data/ 为空 → 指主仓
        from tools.analysis.market_forecast.dataroot import ensure_data_root
        ensure_data_root(data_root)
    except Exception as e:  # noqa: BLE001
        logger.warning("ensure_data_root 失败（kline 可能取不到、收益列 pending）：%s", e)
    kline_cache: dict = {}
    snap_cache: dict = {}
    market_ew_cache: dict = {}
    return [(p, compute_labels(p, kline_cache, snap_cache, data_root, market_ew_cache))
            for p in picks]
