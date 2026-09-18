"""午盘 Q · M3.b 分时回测(真 14:30/14:50 价,非收盘代理)。

## 与 M3.a 的区别(为什么重写而不是改 M3.a)

M3.a(`backtest_midday_q.py`)是**降级回测**:拿日线 OHLC 假装分时——
用 `close/open-1` 代替"14:30 现价/open",给了策略"看到收盘再决定"的先知优势。
M3.b 用 baostock 5min 真实 bar,消除这个代理。

09-17 实证(400 个票×日样本)M3.a 的代理偏差其实很小(信号侧均值 +0.033pp、
|偏差|>1pp 仅 6%)—— 所以 M3.a 那个"三条全红灯"的结论**不是降级造成的假象**。
但 M3.a 另有两个**真 bug**,本模块一并修:

### 修 1:一字板/跌停按收盘价成交(M3.a 系统性高估)

M3.a 的 Q3 前三大盈利全是一字涨停(次日 O=H=L=C,全天封板不开),
实盘**买不进也卖不掉**,回测却按收盘价计 +19.8% 收益,Q3 全部正收益靠这 3 笔撑着
(去掉后均值 -0.13%)。本模块按 `_回测与评估口径.md` §八.2 的既定口径处理:
    · 买入日触及涨停 → 剔除(不可买)
    · T+1 卖出日一字涨停/跌停 → **强制持有到下一日再卖,累计双边成本**(不是剔除)

### 修 2:回撤口径是逐笔累加(不是组合回撤)

M3.a 的 `max_dd` 把每笔收益当时间序列累加,得出 -49%/-62% 这种数,
与"最大单日回撤"不是一回事(评估口径 §二 要的是**组合口径**)。
本模块按日聚合成组合日收益(同日多笔等权),再算组合回撤。

## 口径(对齐 `_回测与评估口径.md`)

    信号:   只用 ≤14:30(首判)/ ≤14:50(复核)的 bar + T-1 及以前日线 —— 防未来
    买入:   T 日 14:50 bar 收盘价(方案:14:50-15:00 VWAP 的近似)
    卖出:   T+1 日 14:50 bar 收盘价(方案 v0.3 尾盘卖;14:30-14:50 区间取 14:50)
    成本:   15bp/次(买 5bp + 卖 10bp 含印花税)
    基准:   同日焦点池等权同口径持有(14:50 → 次日 14:50)

## 用法

    python -m tools.backtest.fetch_midday_q_bars --start 2026-01-02 --end 2026-09-10
    python -m tools.backtest.backtest_midday_q_m3b

⚠️ 非投资建议;历史回测≠未来保证。
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
from datetime import datetime
from pathlib import Path

import pandas as pd

from tools.config import midday_q_universe as UNIV
from tools.config import settings
from tools.store import repo as store
from tools.strategy import midday_q_signals as S

logger = logging.getLogger("backtest.midday_q_m3b")

BARS_DIR = settings.PROJECT_ROOT / "data" / "analysis" / "midday_q" / "m3b_bars"
TOTAL_COST_BPS = 15.0
TOP_N_PER_STRATEGY = 5

# 各 stage 的判定时刻
AS_OF_1430 = "1430"
AS_OF_1450 = "1450"


# ────────────────────────────── 数据加载 ──────────────────────────────

def load_bars(min_coverage: float = 0.8,
               require_end_within: int = 10) -> dict[str, pd.DataFrame]:
    """读 m3b_bars/*.parquet → {code: DataFrame(date,time,ohlcv)}。

    ## 为什么要覆盖率门槛(2026-09-17 加)

    全A 采集实测:约 **27%** 的票数据末日聚集在 03-11 / 05-14 / 07-13 三个日期
    (交易日数 42 / 84 / 125,而完整票是 168)。抽查那几只都是正常在市公司
    (冰山冷热/北京科锐/法尔胜…),**不是退市** —— 判断是 baostock 对长区间的
    分段限流截断,且 `error_code` 仍返 0,采集侧的幂等校验对"整段尾部缺失"无感。

    这种票混进回测会造成两个隐蔽错误:
      1. **"同日截面"名不副实** —— 某日实际参与打分的票数远少于名义票池,
         策略等于在一个悄悄缩小的池子里选股;
      2. **等权基准被扭曲** —— 基准按当日有数据的票等权,若后半段只剩
         73% 的票,基准代表的已不是原池子。

    故默认剔除覆盖不足的票,并把剔除量 log 出来(不静默丢弃)。

    参数:
        min_coverage:       该票交易日数 / 全样本最大交易日数,低于此剔除
        require_end_within: 该票末日距全样本最晚日不得超过 N 个交易日
                            (拦"前面齐、尾部整段缺"的限流截断)
    """
    out: dict[str, pd.DataFrame] = {}
    if not BARS_DIR.exists():
        raise FileNotFoundError(
            f"分时 bar 目录不存在:{BARS_DIR}\n"
            "先跑:python -m tools.backtest.fetch_midday_q_bars")
    raw: dict[str, pd.DataFrame] = {}
    for p in sorted(BARS_DIR.glob("*.parquet")):
        try:
            df = pd.read_parquet(p)
        except Exception as e:
            logger.warning("读 %s 失败:%s", p.name, e)
            continue
        if df.empty:
            continue
        # 零价行防御:停牌日 baostock 返 open=close=0,采集侧(新版)已拦,
        # 但已落盘的旧文件里仍有 → 这里兜底。任何 price/buy-1 遇 0 会变 inf,
        # 污染均值/夏普(实证 7/937386 行就足以把整列均值变成 inf)。
        df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]
        if df.empty:
            continue
        raw[p.stem] = df
    if not raw:
        return out

    # 全样本口径:最大交易日数 + 最晚日期
    all_dates = sorted({d for df in raw.values()
                         for d in df["date"].astype(str).tolist()})
    max_days = max(df["date"].nunique() for df in raw.values())
    latest = all_dates[-1]
    cutoff_idx = max(0, len(all_dates) - 1 - require_end_within)
    end_floor = all_dates[cutoff_idx]

    dropped_short = dropped_stale = 0
    for code, df in raw.items():
        n = df["date"].nunique()
        if n / max_days < min_coverage:
            dropped_short += 1
            continue
        if str(df["date"].max()) < end_floor:
            dropped_stale += 1
            continue
        out[code] = df

    logger.info("加载分时 bar %d 只(原 %d);剔除:覆盖率<%.0f%% %d 只、"
                "末日早于 %s %d 只",
                len(out), len(raw), min_coverage * 100, dropped_short,
                end_floor, dropped_stale)
    if (dropped_short + dropped_stale) > len(raw) * 0.3:
        logger.warning("⚠️ 剔除了 %.0f%% 的票 —— 采集数据大面积不完整,"
                       "回测结论的代表性受限,建议先补采",
                       (dropped_short + dropped_stale) / len(raw) * 100)
    return out


def daily_from_bars(bars: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """从分时 bar 自行聚合出日线(算 MA / 昨收 / 历史均量用)。

    ## 为什么不读主档(2026-09-17 改)

    主档 `data/master/kline/` 只有 **126 只**(焦点池),而全A 分时有 5206 只。
    若沿用 `load_daily()` 读主档,5000+ 只票会因拿不到 `_daily_ctx`
    (MA/昨收/均量,Q1/Q2 的必要条件)而被**全部静默跳过** —— 全A 采集就白跑了。

    分时 bar 自身含 date/ohlc/volume/amount,足以自足聚合:
        open  = 当日最早采样时刻的 open(KEEP_TIMES 里是 0935)
        close = 当日最晚采样时刻的 close(1500)
        high/low/volume/amount = 当日各采样点的 max/min/sum

    ⚠️ 诚实边界:volume/amount 是**采样点之和**(7 个时刻),不是全日总量;
    high/low 是采样点极值,不是真实日内极值。这两项只用于
    "相对自身历史的比值"(vol_ma20 比值、MA 排列),**同口径相除时偏差大部分抵消**;
    但绝对量级不可与主档日线混用。
    """
    out: dict[str, pd.DataFrame] = {}
    for code, bdf in bars.items():
        df = bdf.sort_values(["date", "time"])
        g = df.groupby("date", sort=True)
        agg = pd.DataFrame({
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
            "amount": g["amount"].sum(),
        }).reset_index()
        if not agg.empty:
            out[code] = agg
    logger.info("从分时 bar 聚合日线 %d 只", len(out))
    return out


def load_daily() -> dict[str, pd.DataFrame]:
    """主档日线(仅焦点池 126 只可用)。

    ⚠️ 全A 回测请用 `daily_from_bars()` —— 主档只有焦点池,
    读主档会让 5000+ 只全A票拿不到 MA/昨收上下文而被静默跳过。
    保留本函数供"只跑焦点池且想用真实全日量"的场景对照。
    """
    out: dict[str, pd.DataFrame] = {}
    for c in UNIV.get_focus_codes():
        try:
            df = store.get_master_kline(c)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        out[c] = df.sort_values("date").reset_index(drop=True)
    logger.info("加载主档日线 %d 只", len(out))
    return out


def _bar_at(bars: pd.DataFrame, date: str, hhmm: str) -> pd.Series | None:
    """取某日某时刻的 bar。缺 → None。"""
    sub = bars[(bars["date"] == date) & (bars["time"] == hhmm)]
    return sub.iloc[0] if not sub.empty else None


def _cum_to(bars: pd.DataFrame, date: str, hhmm: str) -> tuple[float, float, float, float]:
    """当日 ≤hhmm 的累计:(成交量, 成交额, 区间最高, 区间最低)。

    ⚠️ 防未来核心:只取 time <= hhmm 的 bar。KEEP_TIMES 是抽样时刻(非全量 bar),
    故 high/low 是"这几个采样点的极值",不是真实日内极值 —— 对
    DistanceToDayHigh 这类信号是**低估分母**,会让条件偏松。已在报告里标注。
    """
    sub = bars[(bars["date"] == date) & (bars["time"] <= hhmm)]
    if sub.empty:
        return 0.0, 0.0, 0.0, 0.0
    return (float(sub["volume"].sum()), float(sub["amount"].sum()),
            float(sub["high"].max()), float(sub["low"].min()))


def trading_dates(bars: dict[str, pd.DataFrame]) -> list[str]:
    dates: set[str] = set()
    for df in bars.values():
        dates.update(df["date"].astype(str).tolist())
    return sorted(dates)


# ────────────────────────────── 日线上下文(T-1 及以前) ──────────────────────────────

def _daily_ctx(daily: pd.DataFrame, date: str) -> dict | None:
    """T-1 及以前的 MA5/10/20、昨收、前20日均量。不足 → None。"""
    hist = daily[daily["date"] < date]
    if len(hist) < 20:
        return None
    cl = hist["close"].astype(float)
    return {
        "prev_close": float(cl.iloc[-1]),
        "ma5": float(cl.tail(5).mean()),
        "ma10": float(cl.tail(10).mean()),
        "ma20": float(cl.tail(20).mean()),
        "ma60": float(cl.tail(60).mean()) if len(hist) >= 60 else None,
        "vol_ma20": float(hist["volume"].astype(float).tail(20).mean()),
    }


def _limit_pct(code: str) -> float:
    """该票涨跌幅上限(小数)。委托生产侧单一真源 `midday_q_signals.board_limit_pct()`。

    不自己判 `startswith("30"/"68")` —— 两个理由:
      1. 宪法:同一条"代码→板块"规则不得散落多处各自演化
         (见 tests/test_exchange_single_source.py 防复发闸门);
      2. **回测必须与生产同口径** —— 生产 screener 判涨停用的就是那套表,
         回测另写一份的话,"哪些票算涨停不可买"两边会悄悄不一致,
         回测结论就不能用来推断生产表现。
    """
    return S.board_limit_pct(code)


# ────────────────────────────── 大盘闸门(焦点池内广度代理) ──────────────────────────────

def compute_gate(bars: dict[str, pd.DataFrame], daily: dict[str, pd.DataFrame],
                  date: str, as_of: str) -> dict:
    """按 ≤as_of 的分时算焦点池广度 → 闸门状态(代理全A,同 M3.a 口径)。"""
    ups = downs = flats = lim_up = lim_dn = 0
    pcts: list[float] = []
    for c, bdf in bars.items():
        ctx = _daily_ctx(daily.get(c, pd.DataFrame()), date) if c in daily else None
        if not ctx:
            continue
        bar = _bar_at(bdf, date, as_of)
        if bar is None:
            continue
        pc = ctx["prev_close"]
        if pc <= 0:
            continue
        pct = (float(bar["close"]) / pc - 1) * 100
        pcts.append(pct)
        if pct > 0.1: ups += 1
        elif pct < -0.1: downs += 1
        else: flats += 1
        lim = _limit_pct(c) * 100
        if pct >= lim - 0.5: lim_up += 1
        elif pct <= -(lim - 0.5): lim_dn += 1

    total = ups + downs + flats
    if total == 0:
        return {"state": "未知", "allowed_strategies": [], "position_pct": 0.0}

    mean_pct = sum(pcts) / total
    g1 = g2 = mean_pct / 100.0
    g3 = ups / max(downs, 1)
    g4 = lim_up / max(lim_dn, 1)
    g5 = (ups - downs) / total

    strong = sum([g1 >= 0.005, g2 >= 0.005, g3 >= 1.5, g4 >= 5.0, g5 >= 0.2])
    weak = sum([g1 <= -0.005, g2 <= -0.005, g3 <= 0.67, g4 <= 1.0, g5 <= -0.2])
    if g4 <= 0.5 and g5 <= -0.5: state = "崩盘"
    elif strong >= 4: state = "强势"
    elif weak >= 4: state = "弱势"
    else: state = "震荡"

    # 与 M3.a 同一张 STATE_ALLOW(v3 口径),便于两版结果直接对比。
    # Q3b 占用原 Q3 的闸门位置 —— 它是 Q3 的日线口径替代(定位同为"资金面确认"),
    # 不放进来的话 Q3b 永不触发。
    STATE_ALLOW = {
        "强势": (["Q3", "Q3b"], 1.0),
        "弱势": (["Q2", "Q3", "Q3b"], 0.7),
        "震荡": (["Q1", "Q2", "Q3", "Q3b"], 0.5),
        "崩盘": ([], 0.0),
        "未知": ([], 0.0),
    }
    allowed, pos = STATE_ALLOW[state]
    return {"state": state, "allowed_strategies": list(allowed), "position_pct": pos,
            "indicators": {"g1": g1, "g3": g3, "g4": g4, "g5": g5,
                            "up": ups, "down": downs,
                            "lim_up": lim_up, "lim_dn": lim_dn}}


# ────────────────────────────── 三策略(真分时口径) ──────────────────────────────

def q1_hits(bars, daily, date: str, as_of: str) -> list[dict]:
    """Q1 强势接力(真分时):日内涨幅 3-6% + 量能不衰 + 贴日高 + 均线多头 + 非涨停。"""
    hits = []
    for c, bdf in bars.items():
        ctx = _daily_ctx(daily.get(c, pd.DataFrame()), date) if c in daily else None
        if not ctx:
            continue
        b_open = _bar_at(bdf, date, "0935")
        b_now = _bar_at(bdf, date, as_of)
        if b_open is None or b_now is None:
            continue
        opn = float(b_open["open"])
        now = float(b_now["close"])
        if opn <= 0:
            continue

        ir = now / opn - 1
        if not (0.03 <= ir <= 0.06):
            continue

        vol_cum, amt_cum, hi, _lo = _cum_to(bdf, date, as_of)
        if hi <= 0:
            continue
        # 距"截至 as_of 的最高"(非全日高 → 防未来)
        dh = 1 - now / hi
        if dh > 0.015:
            continue
        # 量能:截至 as_of 的累计量 vs 前20日全日均量(as_of=14:30 时约当日 5/6 时长)
        if ctx["vol_ma20"] <= 0:
            continue
        vol_exp = vol_cum / ctx["vol_ma20"]
        if vol_exp < 0.8:
            continue
        if not (ctx["ma5"] > ctx["ma10"] > ctx["ma20"] and opn > ctx["ma5"]):
            continue
        # 非涨停(不可买)
        lu = ctx["prev_close"] * (1 + _limit_pct(c))
        if now >= lu * 0.985:
            continue
        if amt_cum < 5000 * 10000:
            continue

        hits.append({"code": c,
                      "rank": 0.4 * ir + 0.3 * (vol_exp - 0.8) + 0.2 * (1 - dh),
                      "signals": {"ir": round(ir, 4), "vol_exp": round(vol_exp, 2),
                                   "dh": round(dh, 4)}})
    hits.sort(key=lambda h: h["rank"], reverse=True)
    return hits[:TOP_N_PER_STRATEGY]


def q2_hits(bars, daily, date: str, as_of: str) -> list[dict]:
    """Q2 弱转强反抽(真分时):早盘急跌 → 午后回血到平盘附近 + 放量 + 非长期熊 + 非跌停。"""
    hits = []
    for c, bdf in bars.items():
        ctx = _daily_ctx(daily.get(c, pd.DataFrame()), date) if c in daily else None
        if not ctx:
            continue
        b_open = _bar_at(bdf, date, "0935")
        b_now = _bar_at(bdf, date, as_of)
        if b_open is None or b_now is None:
            continue
        opn = float(b_open["open"]); now = float(b_now["close"])
        if opn <= 0:
            continue

        vol_cum, amt_cum, _hi, lo = _cum_to(bdf, date, as_of)
        if lo <= 0:
            continue
        il = lo / opn - 1                      # 截至 as_of 的最低相对开盘
        if il > -0.03:
            continue
        rb = now / lo - 1                      # 从最低点反弹
        if rb < 0.02:
            continue
        cr = now / opn - 1
        if not (-0.01 <= cr <= 0.02):
            continue
        if ctx["vol_ma20"] <= 0:
            continue
        vol_exp = vol_cum / ctx["vol_ma20"]
        if vol_exp < 1.2:
            continue
        if ctx["ma60"] is not None and opn <= ctx["ma60"] * 0.9:
            continue
        ld = ctx["prev_close"] * (1 - _limit_pct(c))
        if now <= ld * 1.015:
            continue
        if amt_cum < 5000 * 10000:
            continue

        hits.append({"code": c,
                      "rank": 0.4 * rb + 0.3 * vol_exp + 0.2 * abs(il) + 0.1 * (-cr),
                      "signals": {"il": round(il, 4), "rb": round(rb, 4),
                                   "cr": round(cr, 4), "vol_exp": round(vol_exp, 2)}})
    hits.sort(key=lambda h: h["rank"], reverse=True)
    return hits[:TOP_N_PER_STRATEGY]


# ────────────────────────────── Q3b 资金面确认(日线资金流) ──────────────────────────────
#
# ⚠️ **Q3b 不是 Q3**,是另一条策略。
#
# 原 Q3 的核心信号是"当日 13:00→14:50 的分时主力净流入"(方案 §核心假设:
# 「分时主力净流入在午后**仍持续为正**,反映主力**当日已在建仓**」),
# 那个数据拿不到历史(东财 fflow/kline 只返当天)。
#
# Q3b 改用**日线**资金流,信号变成"主力**过去几天**的累积流向":
#     原 Q3 :盘中捕捉"主力今天正在动手"的即时信号 → 前瞻次日
#     Q3b   :看主力前几日的流向趋势 → 慢速的资金面确认
# 交易逻辑不同,故独立命名/回测/判定。**不得把 Q3b 的结论当作 Q3 的结论。**
#
# ## 两个数据限制(决定了 Q3b 只能做单信号)
#
# 数据源是新浪 MoneyFlow(东财日线资金流 2026-09-18 实测被墙):
#   1. **只有"主力净流入"一列**,五档(小单/中单/大单/超大单)全是 NaN
#      → 原 Q3 的第二个信号 `LargeOrderPct` **算不出来**;
#   2. 新浪"主力"口径与东财**不可比**(fundflow.py 标 tier=main_only)
#      → 阈值**不能照搬** Q3 的 `MainNetPM ≥ 0`。
#
# ## 阈值为什么用分位数而不是"≥0"
#
# 实测全样本(25 万个票×日):主力净流入 >0 的比例只有 **43.9%**,中位数 -0.023 亿。
# 照搬"≥0"会选中 44% 的票 —— 几乎不起筛选作用。
# 故 Q3b 用**相对该票自身历史的分位**:近 5 日净流入之和 > 过去 60 日同窗口的
# 70 分位,即"主力最近的流入强度处于该票自身的历史高位"。
#
# ## 防未来红线
#
# 只用 **T-1 及以前**的日线资金流。当日(T)的日线值含 14:50→15:00 那段,
# 14:50 决策时尚未发生,用它就是未来函数。

Q3B_DIR = settings.PROJECT_ROOT / "data" / "analysis" / "midday_q" / "q3b_fundflow"
Q3B_SUM_WIN = 5          # 近 N 日净流入求和
Q3B_HIST_WIN = 60        # 对比的历史窗口
Q3B_PCTILE = 0.70        # 分位门槛


def load_q3b_flow() -> dict[str, pd.DataFrame]:
    """读 q3b_fundflow/*.parquet → {code: DataFrame(date, 主力净流入)}。"""
    out: dict[str, pd.DataFrame] = {}
    if not Q3B_DIR.exists():
        return out
    for p in sorted(Q3B_DIR.glob("*.parquet")):
        try:
            df = pd.read_parquet(p)
        except Exception:
            continue
        if df.empty or "主力净流入" not in df.columns:
            continue
        out[p.stem] = df.sort_values("date").reset_index(drop=True)
    logger.info("加载 Q3b 日线资金流 %d 只", len(out))
    return out


def _q3b_signal(flow: pd.DataFrame, date: str) -> tuple[bool, float] | None:
    """近 5 日净流入之和是否处于自身历史 70 分位以上。

    只取 `date` **之前**的行(防未来:当日日线资金流含 14:50 后的部分)。
    返回 (是否命中, 当前 5 日和/亿)。样本不足 → None。
    """
    hist = flow[flow["date"] < date]
    if len(hist) < Q3B_HIST_WIN + Q3B_SUM_WIN:
        return None
    v = hist["主力净流入"].astype(float)
    cur = float(v.tail(Q3B_SUM_WIN).sum())
    # 历史上同样"连续5日和"的分布(滚动窗口)
    roll = v.rolling(Q3B_SUM_WIN).sum().dropna().tail(Q3B_HIST_WIN)
    if roll.empty:
        return None
    thr = float(roll.quantile(Q3B_PCTILE))
    return (cur > thr), cur / 1e8


def q3b_hits(bars, daily, date: str, as_of: str,
              flows: dict[str, pd.DataFrame] | None = None) -> list[dict]:
    """Q3b 资金面确认:主力近期流入处自身历史高位 + 价格温和 + 流动性。

    价格侧条件刻意**比 Q1/Q2 宽松**(不要求强势或超跌),因为 Q3b 的主张是
    "资金面先行" —— 若还叠加严格价格形态,就分不清 alpha 来自资金还是价量。
    """
    flows = flows or {}
    hits = []
    for c, bdf in bars.items():
        flow = flows.get(c)
        if flow is None:
            continue
        sig = _q3b_signal(flow, date)
        if sig is None or not sig[0]:
            continue
        ctx = _daily_ctx(daily.get(c, pd.DataFrame()), date) if c in daily else None
        if not ctx:
            continue
        b_open = _bar_at(bdf, date, "0935")
        b_now = _bar_at(bdf, date, as_of)
        if b_open is None or b_now is None:
            continue
        opn = float(b_open["open"]); now = float(b_now["close"])
        if opn <= 0:
            continue
        cr = now / opn - 1
        if not (-0.02 <= cr <= 0.05):          # 温和区间(同 Q3 原设计)
            continue
        _v, amt_cum, _hi, _lo = _cum_to(bdf, date, as_of)
        if amt_cum < 8000 * 10000:             # Q3 加强的流动性门(8000万)
            continue
        lu = ctx["prev_close"] * (1 + _limit_pct(c))
        if now >= lu * 0.985:                  # 涨停不可买
            continue
        hits.append({"code": c, "rank": sig[1],
                      "signals": {"flow5d_yi": round(sig[1], 3),
                                   "cr": round(cr, 4)}})
    hits.sort(key=lambda h: h["rank"], reverse=True)
    return hits[:TOP_N_PER_STRATEGY]


# Q3 需要分时资金流历史 —— 东财 fflow/kline 只返当天(lmt 给多大都拿不到历史),
# 故 M3.b 无法回测 Q3。Q3 仍须靠定时任务未来采样攒够 60 交易日。
# Q3b(日线口径)是独立策略,由 --with-q3b 开启。
STRATEGIES = {"Q1": q1_hits, "Q2": q2_hits}



# ────────────────────────────── 成交可行性 + 收益 ──────────────────────────────

def _is_one_word_limit(bdf: pd.DataFrame, daily_df: pd.DataFrame,
                        code: str, date: str) -> str | None:
    """判某日是否不可成交。返回 "涨停一字"/"跌停"/None。

    一字板判据:当日各采样时刻 high==low(全天无波动)且涨幅≥涨停线-0.5pp。
    跌停判据:收盘跌幅 ≤ -(涨停线-0.5pp)。
    ⚠️ KEEP_TIMES 是抽样点,high==low 只能证明"这些时刻无波动",
       比真全量 bar 略宽松 —— 宁可多判几个不可成交(保守),不高估收益。
    """
    ctx_hist = daily_df[daily_df["date"] < date]
    if ctx_hist.empty:
        return None
    pc = float(ctx_hist["close"].astype(float).iloc[-1])
    sub = bdf[bdf["date"] == date]
    if sub.empty or pc <= 0:
        return None
    b1500 = sub[sub["time"] == "1500"]
    close = float(b1500["close"].iloc[0]) if not b1500.empty else float(sub["close"].iloc[-1])
    pct = close / pc - 1
    lim = _limit_pct(code)
    flat = bool((sub["high"].astype(float) == sub["low"].astype(float)).all())
    if flat and pct >= lim - 0.005:
        return "涨停一字"
    if pct <= -(lim - 0.005):
        return "跌停"
    return None


# 止损检查时点(次日,按时间顺序)。用真分时采样点判触发,不用日线 low
# —— 日线 low 会高估止损效果(那是全天最低,实盘未必在该价位成交得掉)。
_STOP_CHECK_TIMES = ("0935", "1030", "1425", "1430", "1445", "1450")


def _sell_with_stop(bars, daily, code: str, buy_date: str, buy_price: float,
                     dates: list[str], stop_pct: float | None
                     ) -> tuple[float, int, str] | None:
    """带止损的卖出。`stop_pct=None` → 退化为原逻辑(无止损)。

    ## 口径(诚实标注)

    · **触发判据用采样点收盘价**,不是日线 low。日线 low 是全天最低点,
      按它成交等于假设"总能在最低价止损掉",会**系统性高估止损效果**。
      用采样点收盘 = "在这几个时刻检查一次,跌破就出" —— 更接近实盘的
      定时检查行为,且偏保守(采样点之间的瞬时击穿会漏掉,少触发几笔)。
    · 触发后按**该采样点收盘价**成交,不按 stop 价 —— 实盘跌破时挂单
      通常成交在更差的价位,按采样价成交比按 stop 价更贴近现实。
    · 止损优先于"一字/跌停顺延":跌停日本来就卖不掉,止损同样卖不掉,
      故先判可成交性,不可成交就顺延到下一日继续检查。
    """
    bdf = bars[code]
    ddf = daily[code]
    try:
        i = dates.index(buy_date)
    except ValueError:
        return None
    if buy_price <= 0:
        return None

    for k in range(1, 4):
        if i + k >= len(dates):
            return None
        d = dates[i + k]
        if _is_one_word_limit(bdf, ddf, code, d):
            continue                      # 一字/跌停:卖不掉 → 顺延

        if stop_pct is not None:
            # 按时间顺序扫采样点,首个跌破 stop 的时刻即出场
            for t in _STOP_CHECK_TIMES:
                bar = _bar_at(bdf, d, t)
                if bar is None:
                    continue
                px = float(bar["close"])
                if px <= 0:
                    continue
                if px / buy_price - 1 <= stop_pct:
                    return px, k, f"止损@D{k} {t[:2]}:{t[2:]}"

        bar = _bar_at(bdf, d, AS_OF_1450)
        if bar is None:
            continue
        return float(bar["close"]), k, ("顺延%d日" % k if k > 1 else "")
    return None


def _sell_with_feasibility(bars, daily, code: str, buy_date: str,
                            dates: list[str]) -> tuple[float, int, str] | None:
    """从 buy_date 的次日起找**可成交**的卖出价(真 14:50 bar)。无止损。

    按 `_回测与评估口径.md` §八.2:T+1 卖出日触及跌停/一字涨停 → 强制持有到下一日,
    **累计双边成本**。最多顺延 3 个交易日(再不行视为无法退出,丢弃该笔)。
    返回 (卖价, 持有天数, 备注)。
    """
    bdf = bars[code]; ddf = daily[code]
    try:
        i = dates.index(buy_date)
    except ValueError:
        return None
    for k in range(1, 4):
        if i + k >= len(dates):
            return None
        d = dates[i + k]
        blocked = _is_one_word_limit(bdf, ddf, code, d)
        bar = _bar_at(bdf, d, AS_OF_1450)
        if bar is None:
            continue
        if blocked:
            continue                      # 不可成交 → 顺延
        return float(bar["close"]), k, ("顺延%d日" % k if k > 1 else "")
    return None


def backtest(bars: dict[str, pd.DataFrame], daily: dict[str, pd.DataFrame],
              start: str | None = None, end: str | None = None,
              stop_pcts: tuple[float | None, ...] = (None,),
              flows: dict[str, pd.DataFrame] | None = None) -> dict:
    """逐日:14:30 首判 → 14:50 复核 → 两次都命中才买 → T+1 14:50 卖。

    `stop_pcts`:要对比的止损档位(小数,如 -0.03)。`None` = 无止损。
    **信号只算一次,各档位复用同一批入选** —— 信号计算是耗时大头
    (5169 只 × 167 日约 12 分钟),每档重跑一遍要一小时。
    止损只影响"怎么卖",不影响"选哪些票",故可安全复用。

    `flows`:Q3b 的日线资金流。传入即把 Q3b 加进策略集(Q1/Q2 不需要它)。
    """
    flows = flows or {}
    strategies = dict(STRATEGIES)
    if flows:
        strategies["Q3b"] = q3b_hits

    dates = trading_dates(bars)
    if start: dates = [d for d in dates if d >= start]
    if end: dates = [d for d in dates if d <= end]
    trade_dates = dates[:-1] if len(dates) > 1 else []

    # log[stop_pct][strat] = [...]
    log = {sp: {s: [] for s in strategies} for sp in stop_pcts}


    gate_log = []
    skipped = {"买入涨停": 0, "无法退出": 0}

    for date in trade_dates:
        g1 = compute_gate(bars, daily, date, AS_OF_1430)
        g2 = compute_gate(bars, daily, date, AS_OF_1450)
        # 闸门翻转到弱势/崩盘 → 全撤(方案:复核翻转则空仓)
        flipped = (g1["state"] != g2["state"]) and g2["state"] in ("弱势", "崩盘")
        allowed = [] if flipped else [s for s in g2["allowed_strategies"]
                                        if s in g1["allowed_strategies"]]
        gate_log.append({"date": date, "s1": g1["state"], "s2": g2["state"],
                          "flipped": flipped, "allowed": allowed})

        for strat, fn in strategies.items():
            if strat not in allowed:
                continue
            # Q3b 需要额外的日线资金流(Q1/Q2 不需要)
            kw = {"flows": flows} if strat == "Q3b" else {}
            # 双点确认:首判命中 ∩ 复核命中
            h1 = {h["code"] for h in fn(bars, daily, date, AS_OF_1430, **kw)}
            if not h1:
                continue
            for h in fn(bars, daily, date, AS_OF_1450, **kw):
                if h["code"] not in h1:
                    continue
                code = h["code"]
                # 买入价:T 日 14:50 真实 bar 收盘
                b_buy = _bar_at(bars[code], date, AS_OF_1450)
                if b_buy is None:
                    continue
                buy = float(b_buy["close"])
                if buy <= 0:
                    continue
                # 买入日不可买(涨停一字)→ 剔除
                if _is_one_word_limit(bars[code], daily[code], code, date) == "涨停一字":
                    skipped["买入涨停"] += 1
                    continue
                # 各止损档位共用同一笔入选,只是卖法不同
                any_ok = False
                for sp in stop_pcts:
                    sold = _sell_with_stop(bars, daily, code, date, buy, dates, sp)
                    if sold is None:
                        continue
                    sell, hold_days, note = sold
                    # 顺延持有 → 累计双边成本(每多持一日多一次双边)
                    cost = TOTAL_COST_BPS / 10000 * hold_days
                    log[sp][strat].append({
                        "date": date, "code": code, "rank": h["rank"],
                        "ret": sell / buy - 1 - cost,
                        "hold_days": hold_days, "note": note,
                        "gate": g2["state"], "signals": h["signals"]})
                    any_ok = True
                if not any_ok:
                    skipped["无法退出"] += 1

    # 向后兼容:`trades` 仍是"无止损"那份(下游 render/摘要按老结构读);
    # 各止损档位放 `trades_by_stop`,由止损对比表单独消费。
    baseline_key = None if None in log else stop_pcts[0]
    return {"trades": log[baseline_key],
             "trades_by_stop": log,
             "stop_pcts": list(stop_pcts),
             "gate_log": gate_log, "skipped": skipped,
             "n_dates": len(trade_dates),
             "dates": {"start": trade_dates[0] if trade_dates else None,
                        "end": trade_dates[-1] if trade_dates else None}}


# ────────────────────────────── 指标(组合口径回撤) ──────────────────────────────

def _ran_strategies(res: dict) -> list[str]:
    """本次实际跑了哪些策略(按 STRATEGIES 顺序,末尾追加 Q3b 等动态加入的)。

    不能直接遍历 `STRATEGIES` 常量 —— Q3b 是运行时按 `flows` 是否传入动态
    加进策略集的,写死常量会让它在报告里不显示。
    """
    ran = list(res.get("trades") or {})
    order = list(STRATEGIES) + [s for s in ran if s not in STRATEGIES]
    return [s for s in order if s in ran]


def portfolio_metrics(trades: list[dict]) -> dict:
    """按**组合日口径**算指标(修 M3.a 的逐笔累加回撤 bug)。

    同日多笔 → 等权平均成 1 个组合日收益;再在日序列上算累计/回撤/夏普。
    """
    if not trades:
        return {"n": 0, "n_days": 0, "win_rate": None, "mean_ret": None,
                 "median_ret": None, "max_dd": None, "total_ret": None,
                 "sharpe": None, "max_consec_loss": None}
    by_day: dict[str, list[float]] = {}
    for t in trades:
        by_day.setdefault(t["date"], []).append(t["ret"])
    days = sorted(by_day)
    day_rets = [statistics.mean(by_day[d]) for d in days]

    rets = [t["ret"] for t in trades]
    n = len(rets)
    wins = sum(1 for r in rets if r > 0)

    # 组合净值 → 真实最大回撤(复利口径)
    nav = 1.0; peak = 1.0; max_dd = 0.0
    for r in day_rets:
        nav *= (1 + r)
        peak = max(peak, nav)
        max_dd = min(max_dd, nav / peak - 1)
    # 最大连续亏损日
    cur = best = 0
    for r in day_rets:
        cur = cur + 1 if r < 0 else 0
        best = max(best, cur)

    std = statistics.pstdev(day_rets) if len(day_rets) > 1 else 0.0
    mean_day = statistics.mean(day_rets)
    sharpe = (mean_day / std * (244 ** 0.5)) if std > 0 else None

    return {"n": n, "n_days": len(days), "win_rate": wins / n,
             "mean_ret": statistics.mean(rets),
             "median_ret": statistics.median(rets),
             "max_dd": max_dd, "total_ret": nav - 1.0,
             "sharpe": sharpe, "max_consec_loss": best,
             "mean_day_ret": mean_day}


def bootstrap_ci(rets: list[float], n_boot: int = 2000,
                  seed: int = 42) -> tuple[float, float] | None:
    """均值的 bootstrap 95% CI。下界 >0 才说明收益在统计上真实存在。"""
    if len(rets) < 5:
        return None
    import random
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(rets, k=len(rets)))
                    for _ in range(n_boot))
    lo = means[int(n_boot * 0.025)]
    hi = means[int(n_boot * 0.975)]
    return lo, hi


def benchmark_daily(bars: dict[str, pd.DataFrame], dates: list[str]) -> dict[str, float]:
    """基准:焦点池等权,14:50 → 次日 14:50(与策略同口径,含成本才公平→这里不扣,标注)。"""
    out: dict[str, float] = {}
    for i, d in enumerate(dates[:-1]):
        nd = dates[i + 1]
        rr = []
        for c, bdf in bars.items():
            a = _bar_at(bdf, d, AS_OF_1450)
            b = _bar_at(bdf, nd, AS_OF_1450)
            if a is None or b is None:
                continue
            p0 = float(a["close"]); p1 = float(b["close"])
            if p0 > 0:
                rr.append(p1 / p0 - 1)
        if rr:
            out[d] = statistics.mean(rr)
    return out


def verdict(m: dict) -> str:
    """评估口径 §五 红黄绿灯。"""
    if not m["n"] or m["mean_ret"] is None:
        return "—"
    mean = m["mean_ret"]; dd = m["max_dd"]; sh = m["sharpe"] or 0
    if mean <= 0 or (dd is not None and dd <= -0.10):
        return "🔴红灯"
    if mean >= 0.003 and sh >= 0.8 and (dd is None or dd > -0.08):
        return "🟢绿灯"
    return "🟡黄灯"


# ────────────────────────────── 报告 ──────────────────────────────

def render_markdown(res: dict, bars, daily) -> str:
    dates = trading_dates(bars)
    d0, d1 = res["dates"]["start"], res["dates"]["end"]
    bench = benchmark_daily(bars, [d for d in dates if d0 <= d <= d1] if d0 else dates)
    bench_mean = statistics.mean(bench.values()) if bench else float("nan")

    L: list[str] = []
    L.append(f"# 午盘 Q · M3.b 分时回测报告({datetime.now().strftime('%Y-%m-%d')})")
    L.append("")
    L.append("> **真分时口径**:14:30 首判 / 14:50 复核用 baostock 5min 真实 bar,"
             "买卖价取 14:50 bar 收盘 —— 不再用日线收盘价代理。")
    L.append("> ⚠️ 非投资建议;历史回测≠未来保证。")
    L.append("")
    L.append(f"- 区间:**{d0} → {d1}**,交易日 **{res['n_dates']}**")
    L.append(f"- 票池:焦点池 **{len(bars)}** 只(有分时 bar 的)")
    L.append(f"- 成本:{TOTAL_COST_BPS:.0f}bp/次(买5+卖10);顺延持有按天累计双边")
    L.append(f"- 基准(焦点池等权 14:50→次日14:50,未扣成本):日均 **{bench_mean * 100:+.3f}%**")
    st = res["skipped"]
    L.append(f"- 成交可行性剔除:买入日涨停不可买 **{st['买入涨停']}** 笔;"
             f"连续 3 日无法退出 **{st['无法退出']}** 笔")
    L.append("")

    gl = res["gate_log"]
    if gl:
        from collections import Counter
        cnt = Counter(g["s2"] for g in gl)
        nflip = sum(1 for g in gl if g["flipped"])
        L.append(f"- 闸门(14:50)状态分布:{dict(cnt)};复核翻转撤单 **{nflip}** 日")
        L.append("")

    L.append("## 一、总览(组合口径)")
    L.append("")
    L.append("| 策略 | 笔数 | 交易日 | 胜率 | 均值/笔 | 中位/笔 | 组合累计 | **最大回撤** | 夏普 | 最长连亏 | 判定 |")
    L.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|:-:|")
    metrics = {}
    for s in _ran_strategies(res):
        m = portfolio_metrics(res["trades"][s])
        metrics[s] = m
        if not m["n"]:
            L.append(f"| {s} | 0 | — | — | — | — | — | — | — | — | — |")
            continue
        L.append(f"| **{s}** | {m['n']} | {m['n_days']} | {m['win_rate']*100:.1f}% | "
                 f"{m['mean_ret']*100:+.2f}% | {m['median_ret']*100:+.2f}% | "
                 f"{m['total_ret']*100:+.1f}% | {m['max_dd']*100:.1f}% | "
                 f"{m['sharpe']:.2f} | {m['max_consec_loss']} 日 | {verdict(m)} |")
    L.append("")
    L.append("> 回撤为**组合净值口径**(同日多笔等权→日序列→复利回撤),"
             "非 M3.a 的逐笔累加(那会把 -49% 这种非组合数字当回撤)。")
    L.append("")

    L.append("## 二、统计显著性 + 超额收益")
    L.append("")
    L.append("| 策略 | 均值/笔 | bootstrap 95% CI | 显著? | 日均α(扣基准) | 有α? |")
    L.append("|---|--:|:-:|:-:|--:|:-:|")
    for s in _ran_strategies(res):
        tr = res["trades"][s]
        if not tr:
            L.append(f"| {s} | — | — | — | — | — |")
            continue
        rets = [t["ret"] for t in tr]
        ci = bootstrap_ci(rets)
        al = [t["ret"] - bench.get(t["date"], 0.0) for t in tr if t["date"] in bench]
        a = statistics.mean(al) if al else float("nan")
        ci_s = f"[{ci[0]*100:+.2f}%, {ci[1]*100:+.2f}%]" if ci else "样本不足"
        # 三态,不是两态:CI 整段 <0 是"**显著为负**"(有证据地亏钱),
        # 与"跨 0 → 无结论"是完全不同的信息。只判 ci[0]>0 会把前者误标成
        # "不显著",掩盖掉最该看见的结论。
        if not ci:
            sig = "—"
        elif ci[0] > 0:
            sig = "✅ 显著为正"
        elif ci[1] < 0:
            sig = "❌ **显著为负**"
        else:
            sig = "⚪ 跨0·无结论"
        L.append(f"| **{s}** | {statistics.mean(rets)*100:+.2f}% | {ci_s} | {sig} | "
                 f"{a*100:+.3f}% | {'✅' if a > 0 else '❌'} |")
    L.append("")
    L.append("> **三态判读**:CI 整段 >0 = 收益显著为正;整段 <0 = **显著为负**"
             "(不是「没证据」,是**有证据地亏钱**);跨 0 = 样本不足以定论。")
    L.append("> CI 下界 >0 才说明均值收益**不是少数极端交易的偶然结果**。"
             "α ≤ 0 意味着不如等权持有整个焦点池 —— 选股没创造价值。")
    L.append("")

    L.append("## 三、按闸门状态分组")
    L.append("")
    L.append("| 策略 | 闸门 | 笔数 | 胜率 | 均值/笔 |")
    L.append("|---|---|--:|--:|--:|")
    for s in _ran_strategies(res):
        by: dict[str, list[float]] = {}
        for t in res["trades"][s]:
            by.setdefault(t["gate"], []).append(t["ret"])
        for g, rr in sorted(by.items(), key=lambda kv: -len(kv[1])):
            w = sum(1 for r in rr if r > 0) / len(rr)
            L.append(f"| {s} | {g} | {len(rr)} | {w*100:.1f}% | "
                     f"{statistics.mean(rr)*100:+.2f}% |")
    L.append("")

    # ── 止损档位对比(评估口径 §八.3 预留的决策点) ──
    tbs = res.get("trades_by_stop") or {}
    if len(tbs) > 1:
        L.append("## 三·补、止损档位对比")
        L.append("")
        L.append("> 评估口径 §八.3 预注册:「首版无止损,观察 60 日样本回测里"
                 "『次日开盘 -3% 以上』事件的比例和成本,再决定是否加」。本节即该实验。")
        L.append("")
        L.append("**触发口径(诚实标注)**:用**次日采样时刻的收盘价**判触发,"
                 "**不用日线 low** —— 日线 low 是全天最低点,按它成交等于假设"
                 "「总能在最低价止损掉」,会系统性高估止损效果。"
                 f"检查时点:{'/'.join(_STOP_CHECK_TIMES)}(共 {len(_STOP_CHECK_TIMES)} 次)。"
                 "触发后按该时刻收盘价成交(而非 stop 价),更贴近实盘滑价。")
        L.append("")
        for s in _ran_strategies(res):
            base = portfolio_metrics(tbs.get(None, {}).get(s, []))
            if not base["n"]:
                continue
            L.append(f"### {s}")
            L.append("")
            L.append("| 止损 | 笔数 | 胜率 | 均值/笔 | 组合累计 | **最大回撤** | 夏普 | 触发率 | 判定 |")
            L.append("|---|--:|--:|--:|--:|--:|--:|--:|:-:|")
            for sp in res.get("stop_pcts", []):
                tr = tbs.get(sp, {}).get(s, [])
                m = portfolio_metrics(tr)
                if not m["n"]:
                    continue
                fired = sum(1 for t in tr if "止损" in (t.get("note") or ""))
                label = "无(基准)" if sp is None else f"{sp*100:.0f}%"
                L.append(f"| {label} | {m['n']} | {m['win_rate']*100:.1f}% | "
                         f"{m['mean_ret']*100:+.2f}% | {m['total_ret']*100:+.1f}% | "
                         f"{m['max_dd']*100:.1f}% | {m['sharpe']:.2f} | "
                         f"{fired/m['n']*100:.0f}% | {verdict(m)} |")
            L.append("")
        L.append("**怎么读这张表**:止损的核心价值是**压回撤**,不一定提均值"
                 "(它会把「本来能扛回来」的票也砍掉 → 胜率通常下降)。"
                 "重点看**回撤能否压进 -10% 红线内**,以及均值的代价有多大。")
        L.append("")

    L.append("## 四、口径与已知限制(诚实标注)")
    L.append("")
    L.append("**相比 M3.a 修好的**:")
    L.append("")
    L.append("1. **真 14:30/14:50 价** —— 不再用日线收盘代理。"
             "(09-17 实测该代理偏差其实很小:信号侧均值 +0.033pp、|偏差|>1pp 仅 6%,"
             "故 M3.a 的方向性结论并非降级造成的假象。)")
    L.append("2. **成交可行性** —— 买入日涨停一字剔除;T+1 卖出触及一字/跌停按"
             "评估口径 §八.2 **强制顺延持有并累计双边成本**(M3.a 直接按收盘价成交,"
             "Q3 的正收益曾全靠 3 笔一字板撑着)。")
    L.append("3. **组合口径回撤** —— 修 M3.a 逐笔累加的错口径。")
    L.append("")
    L.append("**仍存在的限制**:")
    L.append("")
    L.append("1. **Q3 无法回测** —— 东财 `fflow/kline` 只返当天分时资金流"
             "(`lmt` 给多大都拿不到历史),Q3 核心信号缺历史。仍须靠定时任务未来采样。")
    L.append("2. **bar 是抽样时刻,非全量** —— 只存 "
             f"{len(('0935','1030','1425','1430','1445','1450','1500'))} 个时刻。"
             "故 `DistanceToDayHigh`/`IntradayLow` 用的是"
             "\"采样点极值\"而非真实日内极值 → **分母偏小,条件偏松**,"
             "真实入选会比这里更严。")
    L.append("3. **买卖价用 14:50 bar 收盘代替 VWAP** —— 方案设计是"
             "14:50-15:00 / 次日14:30-14:50 的 VWAP;5min bar 拿不到 tick 级 VWAP。")
    L.append("4. **一字板判据基于抽样点** —— `high==low` 只能证明这几个时刻无波动,"
             "比全量 bar 宽松;倾向**多判几笔不可成交**(保守,不高估收益)。")
    L.append("5. **闸门用焦点池广度代理全A** —— 同 M3.a,非真全A广度。")
    L.append("")
    return "\n".join(L)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="午盘Q M3.b 分时回测(真14:30/14:50价)")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--daily-source", choices=("bars", "master"), default="bars",
                    help="日线上下文(MA/昨收/均量)来源。bars=从分时聚合(全A可用,默认);"
                         "master=读主档(只有焦点池126只,全A会静默跳过5000+只)")
    ap.add_argument("--with-q3b", action="store_true",
                    help="加跑 Q3b(日线资金流口径的资金面确认)。⚠️ Q3b 不是 Q3,"
                         "是另一条策略,结论不可互推(见 q3b_hits docstring)")
    ap.add_argument("--stops", default=None,
                    help="止损档位对比,逗号分隔的百分数(负数),如 \"-3,-5,-8\";"
                         "留空=只跑无止损基准。信号只算一次,各档共用入选")
    ap.add_argument("--min-coverage", type=float, default=0.8,
                    help="票的交易日覆盖率下限(剔采集截断的残缺票,见 load_bars)")
    a = ap.parse_args()

    bars = load_bars(min_coverage=a.min_coverage)
    if not bars:
        logger.error("无分时 bar(或全被覆盖率门槛剔除),先跑 fetch_midday_q_bars")
        return 1
    daily = load_daily() if a.daily_source == "master" else daily_from_bars(bars)
    if a.daily_source == "master":
        missing = len(bars) - sum(1 for c in bars if c in daily)
        if missing:
            logger.warning("⚠️ --daily-source=master 下有 %d 只票无主档日线,"
                           "将被静默跳过;全A 请用默认 bars", missing)

    logger.info("跑 M3.b 回测 %s → %s ...", a.start or "最早", a.end or "最新")
    stop_pcts: tuple[float | None, ...] = (None,)
    if a.stops:
        # None(基准) + 各档位;信号只算一次,各档共用入选
        parsed = tuple(float(x) / 100.0 for x in a.stops.split(","))
        stop_pcts = (None,) + parsed
        logger.info("止损档位对比:基准 + %s",
                    ", ".join(f"{p*100:.0f}%" for p in parsed))
    flows = load_q3b_flow() if a.with_q3b else None
    if a.with_q3b and not flows:
        logger.error("--with-q3b 但无资金流数据,先跑 fetch_q3b_fundflow")
        return 1
    res = backtest(bars, daily, a.start, a.end, stop_pcts=stop_pcts, flows=flows)

    md = render_markdown(res, bars, daily)
    out = Path(a.out) if a.out else (
        settings.PROJECT_ROOT / "docs" / "每日分析_午盘Q" /
        f"M3b_分时回测报告_{datetime.now().strftime('%Y%m%d')}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    logger.info("落报告: %s", out)

    # 控制台摘要
    print("\n" + "=" * 92)
    print(f"M3.b 真分时回测 {res['dates']['start']} → {res['dates']['end']} · "
          f"{res['n_dates']} 交易日 · 票池 {len(bars)} 只")
    print(f"成交剔除:买入涨停 {res['skipped']['买入涨停']} 笔,"
          f"无法退出 {res['skipped']['无法退出']} 笔")
    print("=" * 92)
    print(f'{"策略":<5}{"笔数":>6}{"胜率":>9}{"均值/笔":>10}{"组合累计":>11}'
          f'{"最大回撤":>10}{"夏普":>8}{"判定":>9}')
    print("-" * 92)
    for s in _ran_strategies(res):
        m = portfolio_metrics(res["trades"][s])
        if not m["n"]:
            print(f"{s:<5}{0:>6}{'—':>9}{'—':>10}{'—':>11}{'—':>10}{'—':>8}{'—':>9}")
            continue
        print(f'{s:<5}{m["n"]:>6}{m["win_rate"]*100:>8.1f}%'
              f'{m["mean_ret"]*100:>9.2f}%{m["total_ret"]*100:>10.1f}%'
              f'{m["max_dd"]*100:>9.1f}%{m["sharpe"]:>8.2f}{verdict(m):>10}')
    print("=" * 92)
    print("Q3 未回测:东财分时资金流只返当天,无历史(须未来采样)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
