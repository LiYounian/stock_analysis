"""午盘 Q · 三策略共用信号函数(纯计算,可独测)。

方案见 docs/每日分析_午盘Q/Q1_强势接力.md / Q2_弱转强反抽.md / Q3_资金流前瞻.md。

设计:
    · 全部函数纯计算,不触网、不读文件(输入由 screener 层组装好传入)
    · 缺失数据 → 返回 None / False(不填 0 伪装)
    · 无 as_of 参数——被调用方保证输入快照本身已按 as_of 截断(未来函数由数据源侧保证)

排除项(基本卫生):
    · 涨停/跌停附近(NotLimitUp / NotDownLimit,按 board 用不同规则)
    · ST/*ST/退(NotSTNotNew)
    · 次新股(上市 < 60 交易日)
    · 低流动性(成交额 < 5000 万)
"""
from __future__ import annotations

from typing import Mapping

import pandas as pd

from tools.analysis.market_forecast.breadth import board_of

# 板块 → 常规涨跌幅上限(百分数,ST 例外由 quote 里的 name 前缀识别)
_BOARD_LIMIT_PCT: dict[str, float] = {
    "主板": 10.0,
    "创业板": 20.0,
    "科创板": 20.0,
    "北交所": 30.0,
}
_ST_LIMIT_PCT: float = 5.0

# 排除阈值
NEAR_LIMIT_UP_BUFFER = 0.985      # price < 涨停价 × 0.985(Q1 排除)
NEAR_LIMIT_DOWN_BUFFER = 1.015    # price > 跌停价 × 1.015(Q2 排除)
MIN_AMOUNT_WAN = 5000.0           # 成交额下限(万元)
MIN_LISTING_DAYS = 60

# 各策略默认 rank 权重(与方案 Q1/Q2/Q3 §rank 一致;M3.a 后可调)
Q1_RANK_WEIGHTS = {"intraday_return": 0.4, "am_pm_ratio": 0.3,
                    "distance_to_high": 0.2, "sector_rank": 0.1}
Q2_RANK_WEIGHTS = {"rebound": 0.4, "afternoon_vol_expand": 0.3,
                    "intraday_low_abs": 0.2, "current_return_inverse": 0.1}
Q3_RANK_WEIGHTS = {"main_net_pm": 0.4, "large_order_pct": 0.3,
                    "northbound_net": 0.2, "lhb_proxy": 0.1}


# ────────────────────────────── 共用 helpers ──────────────────────────────

def _f(x) -> float | None:
    """任意 → float。None/NaN/非数 → None。"""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:              # NaN
        return None
    return v


def is_st_by_name(name: str | None) -> bool:
    """从股票名字判 ST。gtimg 返回 'ST*XXX' / '*ST XXX' / 'S*ST XXX' / '退市 XXX'。

    ST 判据:名字**开头**是 ST/*ST/S*ST(避免其他子串误伤,如英文名 test 里含 ST)。
    退市判据:名字含中文"退"。
    """
    if not name:
        return False
    s = name.strip().replace(" ", "")
    # A 股 ST 标识固定在开头,且组合有限
    for prefix in ("*ST", "*st", "ST", "st", "S*ST", "S*st"):
        if s.startswith(prefix):
            return True
    return "退" in name


def limit_up_price(quote: Mapping, code: str) -> float | None:
    """本票涨停价 = prev_close × (1 + 涨幅上限/100)。ST 走 5%,其余按板块。

    - prev_close 缺失 → None
    - 结果四舍五入到 0.01 元(A 股最小价位)
    """
    pc = _f(quote.get("prev_close"))
    if pc is None:
        return None
    if is_st_by_name(quote.get("name")):
        pct = _ST_LIMIT_PCT
    else:
        pct = _BOARD_LIMIT_PCT.get(board_of(code), 10.0)
    return round(pc * (1 + pct / 100.0), 2)


def limit_down_price(quote: Mapping, code: str) -> float | None:
    """本票跌停价 = prev_close × (1 - 涨幅上限/100)。"""
    pc = _f(quote.get("prev_close"))
    if pc is None:
        return None
    if is_st_by_name(quote.get("name")):
        pct = _ST_LIMIT_PCT
    else:
        pct = _BOARD_LIMIT_PCT.get(board_of(code), 10.0)
    return round(pc * (1 - pct / 100.0), 2)


# ────────────────────────────── 基础卫生过滤 ──────────────────────────────

def not_limit_up(quote: Mapping, code: str, buffer: float = NEAR_LIMIT_UP_BUFFER) -> bool:
    """价格未接近涨停(默认 < 涨停价 × 0.985)。价格/涨停价缺失 → False(保守,不算通过)。"""
    p = _f(quote.get("price"))
    lu = limit_up_price(quote, code)
    if p is None or lu is None:
        return False
    return p < lu * buffer


def not_down_limit(quote: Mapping, code: str, buffer: float = NEAR_LIMIT_DOWN_BUFFER) -> bool:
    """价格未接近跌停(默认 > 跌停价 × 1.015)。价格/跌停价缺失 → False。"""
    p = _f(quote.get("price"))
    ld = limit_down_price(quote, code)
    if p is None or ld is None:
        return False
    return p > ld * buffer


def liquidity_ok(quote: Mapping, min_amount_wan: float = MIN_AMOUNT_WAN) -> bool:
    """当日累计成交额 ≥ 下限(单位:万元)。缺失 → False。"""
    a = _f(quote.get("amount_wan"))
    if a is None:
        return False
    return a >= min_amount_wan


def not_st_not_new(quote: Mapping, listing_days: int | None = None,
                    min_days: int = MIN_LISTING_DAYS) -> bool:
    """非 ST + 上市 ≥ min_days。listing_days=None → 只判 ST(降级路径)。"""
    if is_st_by_name(quote.get("name")):
        return False
    if listing_days is None:
        return True                    # 数据缺失只降级判 ST,不误杀正常票
    return listing_days >= min_days


# ────────────────────────────── 通用形态信号 ──────────────────────────────

def intraday_return(quote: Mapping) -> float | None:
    """(price / open - 1),小数。任一缺失 → None。"""
    p = _f(quote.get("price"))
    o = _f(quote.get("open"))
    if p is None or o is None or o == 0:
        return None
    return p / o - 1.0


def distance_to_day_high(quote: Mapping) -> float | None:
    """1 - price / high(0=贴日高,越大越远)。缺失 → None。"""
    p = _f(quote.get("price"))
    h = _f(quote.get("high"))
    if p is None or h is None or h == 0:
        return None
    return 1.0 - p / h


def intraday_low_return(quote: Mapping) -> float | None:
    """(low / open - 1)(负值 = 早盘最低跌破开盘的比例)。"""
    l = _f(quote.get("low"))
    o = _f(quote.get("open"))
    if l is None or o is None or o == 0:
        return None
    return l / o - 1.0


def rebound_from_low(quote: Mapping) -> float | None:
    """(price / low - 1)(从日内最低点反弹的比例)。"""
    p = _f(quote.get("price"))
    l = _f(quote.get("low"))
    if p is None or l is None or l == 0:
        return None
    return p / l - 1.0


def am_pm_vol_ratio(pm_quote: Mapping, am_quote: Mapping) -> float | None:
    """下午累计量 / 上午累计量(以 volume 字段为准)。

    实用近似:pm_quote 的 volume 是 asof 时刻的**当日累计成交量**,
    am_quote 的 volume 是上午某个 slot 的**当日累计成交量**(如 T1030 只到 10:30)。
    → 二者比值 > 1 表明下午来量继续放大,即 Q1 AmPmRatio。

    简单场景(am_quote is None) → 用 pm_quote.vol_ratio(源方给的量比,值>=1 → 温和放量)兜底。
    """
    if am_quote is None:
        vr = _f(pm_quote.get("vol_ratio"))
        return vr
    pm_v = _f(pm_quote.get("volume"))
    am_v = _f(am_quote.get("volume"))
    if pm_v is None or am_v is None or am_v == 0:
        return None
    return pm_v / am_v


# ────────────────────────────── T-1 上下文(kline) ──────────────────────────────

def ma_stacked_bullish(t1_kline: pd.DataFrame | None,
                        current_open: float | None) -> bool:
    """MA5 > MA10 > MA20(短均线多头) 且 current_open > MA5。

    t1_kline 需含 'close' 列,末行 = T-1 收盘;current_open = T 日 open。
    数据不足(≤20 行) → False。
    """
    if t1_kline is None or current_open is None or len(t1_kline) < 20 or "close" not in t1_kline.columns:
        return False
    c = t1_kline["close"].astype(float)
    ma5 = float(c.tail(5).mean())
    ma10 = float(c.tail(10).mean())
    ma20 = float(c.tail(20).mean())
    return (ma5 > ma10 > ma20) and (current_open > ma5)


def not_long_term_downtrend(t1_kline: pd.DataFrame | None,
                              current_open: float | None,
                              ratio: float = 0.9) -> bool:
    """current_open > MA60 × ratio(排除长期熊股)。样本不足 60 行 → True(不判)。"""
    if t1_kline is None or current_open is None or "close" not in t1_kline.columns:
        return True
    if len(t1_kline) < 60:
        return True                    # 次新股由 not_st_not_new 上游拦
    ma60 = float(t1_kline["close"].astype(float).tail(60).mean())
    return current_open > ma60 * ratio


# ────────────────────────────── 板块排名 ──────────────────────────────

def sector_rank_in_top(sector: str | None,
                        sector_ranks: dict[str, int] | None,
                        top_n: int = 5) -> bool:
    """所属板块日内涨幅排名 ≤ top_n。sector_ranks=None 或缺 sector → False。"""
    if not sector or not sector_ranks:
        return False
    rank = sector_ranks.get(sector)
    if rank is None:
        return False
    return rank <= top_n


# ────────────────────────────── 资金流(Q3) ──────────────────────────────

def main_net_since(ffdf: pd.DataFrame | None, since_hhmm: str = "13:00") -> float | None:
    """自 since_hhmm 起的所有 5min 主力净流入求和(元)。ffdf 无/空 → None。"""
    if ffdf is None or ffdf.empty or "主力净流入" not in ffdf.columns:
        return None
    hh, mm = since_hhmm.split(":")
    threshold_minutes = int(hh) * 60 + int(mm)
    row_minutes = ffdf["time"].dt.hour * 60 + ffdf["time"].dt.minute
    sub = ffdf[row_minutes >= threshold_minutes]
    if sub.empty:
        return None
    return float(sub["主力净流入"].sum())


def large_order_pct(ffdf: pd.DataFrame | None) -> float | None:
    """(大单+超大单)累计净流入的**绝对值** / 五单总额的**绝对值** ∈ [0,1]。

    fundflow 只给净流入不给总量;用比例形式 |大+超大| / (|大|+|超大|+|中|+|小|+|主力|) 近似
    "大单参与度"。ffdf 无/空 → None。
    """
    if ffdf is None or ffdf.empty:
        return None
    cols_all = ["主力净流入", "小单净流入", "中单净流入", "大单净流入", "超大单净流入"]
    if not all(c in ffdf.columns for c in cols_all):
        return None
    total_abs = float(sum(ffdf[c].abs().sum() for c in cols_all))
    if total_abs == 0:
        return None
    large = float(ffdf["大单净流入"].abs().sum() + ffdf["超大单净流入"].abs().sum())
    return large / total_abs
