"""entry_price_tool（①塔基·价位回填）· P1a 样板工具。

公理 G5：价位一律程序回填、单调夹逼，LLM 不产数字。

## A7 口径修复（2026-09-18）

历史缺陷：**首入场=加仓=MA5**（拍板③未落地），且当 MA5≈MA20 时止损退化成 −0.1%
这种无效止损（301551 事故）。本版把三件事讲清：

1. **首入场 ≠ 加仓价**（两者区分）：
   · 首入场限价 = **开盘附近限价规则**（限价=D0 收盘、不追高开）——次日回踩到才成交，
     对齐 `entry_rule="limit"`（与 d3_score 记分 / model_a 撮合同口径，见 A8）。
   · 加仓位(回踩) = **MA5**（回踩加仓，通常低于首入场限价，是补仓位不是首入场位）。
2. **止损给最小距离下限**（ATR / 最小百分比）：结构止损（MA20 或旁路 max(MA5,当日低)）
   与下限止损取**更远的一档**，保证止损幅 ≥ 下限，绝不出现 −0.1% 无效止损。
3. **按 T 层选 method**：T1（破前高）→ 突破确认旁路（止损=max(MA5,当日低)、给追高硬顶）；
   T2/T3/未入层 → 回踩系（止损=MA20）。

语义锁（tests/pyramid/test_entry_rule.py）：首入场≠加仓价；止损幅≥下限；T 层→method 映射。
"""
from __future__ import annotations

from typing import Optional

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import load_kline, 浓缩块
from tools.pyramid.entry_rule import DEFAULT_ENTRY_RULE

_ENTRY_METHODS = ("回踩MA5", "回踩MA20", "回踩前低", "突破确认", "缩量企稳", "突破新高确认")

# 止损最小距离下限（A7）：结构止损太近时兜底，防 −0.1% 无效止损。
_MIN_STOP_PCT = 0.02      # 最小止损幅 2%（硬下限）
_ATR_MULT = 1.0           # ATR 倍数（波动自适应：高波动票止损自动放宽）
_ATR_N = 14               # ATR 窗口
_旁路_CAP_MULT = 1.03     # T1 突破追高硬顶（当日高×此倍数）


def _atr(df, n: int = _ATR_N) -> Optional[float]:
    """真实波幅均值 ATR(n)。列不足/样本不足 → None。"""
    if df is None or len(df) < 2 or not {"high", "low", "close"} <= set(df.columns):
        return None
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    prev_close = df["close"].astype(float).shift(1)
    tr = (high - low).abs()
    tr = tr.combine((high - prev_close).abs(), max)
    tr = tr.combine((low - prev_close).abs(), max)
    tr = tr.dropna()
    if len(tr) == 0:
        return None
    return float(tr.tail(n).mean())


def _form_from_kline(df) -> Optional[dict]:
    if df is None or len(df) < 20:
        return None
    close = df["close"]
    return {
        "ma5": float(close.tail(5).mean()),
        "ma20": float(close.tail(20).mean()),
        "前低": float(df["low"].tail(20).min()),
        "现价": float(close.iloc[-1]),
        "当日high": float(df["high"].iloc[-1]),
        "当日low": float(df["low"].iloc[-1]),
        "atr": _atr(df),
    }


def _method_for_tier(t_tier: Optional[str]) -> str:
    """T 层 → method（A7）。T1（破前高）走突破确认旁路；否则回踩 MA5。"""
    if t_tier == "T1":
        return "突破新高确认"
    return "回踩MA5"


def _stop_floor_frac(现价: float, atr: Optional[float]) -> float:
    """止损最小距离（占现价的比例）：max(最小百分比, ATR 自适应)。"""
    frac = _MIN_STOP_PCT
    if isinstance(atr, (int, float)) and 现价 > 0 and atr > 0:
        frac = max(frac, _ATR_MULT * atr / 现价)
    return frac


class EntryPriceTool:
    name = "entry_price"
    塔层 = "①塔基"
    面 = "技术面"  # 入场价位·价位
    source = "主档 K 线（首入场=开盘附近限价·加仓=回踩MA5·止损带下限）"

    def run(
        self,
        as_of: str,
        code: Optional[str] = None,
        method: Optional[str] = None,
        t_tier: Optional[str] = None,
        root: Optional[str] = None,
        **kw,
    ) -> ToolResult:
        if not code:
            raise ValueError("entry_price 需 --code")
        # method 缺省按 T 层派生；显式传入且合法则尊重
        if method is None or method not in _ENTRY_METHODS:
            method = _method_for_tier(t_tier)
        df = load_kline(code, as_of, root=root, min_bars=20)
        form = _form_from_kline(df)
        if form is None:
            return ToolResult(
                name=self.name,
                塔层=self.塔层,
                as_of=as_of,
                code=code,
                浓缩块="价位: 数据不足(K线<20根或缺失)·人工确认",
                fields={"数据不足": True},
                freshness="missing",
                防未来=True,
                source=self.source,
            )
        ma5, ma20 = form["ma5"], form["ma20"]
        现价 = form["现价"]
        当日high, 当日low = form["当日high"], form["当日low"]
        atr = form["atr"]
        是突破 = method in ("突破确认", "突破新高确认")

        # ── 首入场限价：开盘附近限价规则（限价=D0 收盘、不追高开）。突破/回踩同为 D0 收盘 ──
        # 与 entry_rule="limit" 一致：limit=现价(D0收)，次日回踩到才成交。首入场 ≠ 加仓(MA5)。
        首入场 = 现价

        # ── 加仓位（回踩）= MA5，且不高于首入场（回踩是更低的补仓位）──
        加仓 = min(ma5, 首入场)

        # ── 结构止损：T1 突破旁路=max(MA5,当日低)；回踩系=MA20 ──
        if 是突破:
            cands = [v for v in (ma5, 当日low) if isinstance(v, (int, float))]
            结构止损 = max(cands) if cands else ma20
        else:
            结构止损 = ma20

        # ── 止损下限（A7 核心）：结构止损与下限止损取更远的一档，保证止损幅 ≥ 下限 ──
        frac = _stop_floor_frac(现价, atr)
        下限止损 = 首入场 * (1.0 - frac)
        止损 = min(结构止损, 下限止损)   # 取更低（更远）的一档 → 距离恒 ≥ frac

        # ── 红线（不追高上限）：突破给追高硬顶；回踩不追（=首入场）──
        if 是突破:
            红线 = max(当日high * _旁路_CAP_MULT, 首入场)
        else:
            红线 = 首入场

        # ── 单调性夹逼（防未来重写破坏 G5 铁律）：止损 < 首入场 ≤ 红线；加仓 ≤ 首入场 ──
        if 止损 >= 首入场:
            止损 = 首入场 * (1.0 - frac)
        红线 = max(红线, 首入场)
        加仓 = min(加仓, 首入场)
        止损幅 = (止损 / 首入场 - 1) * 100 if 首入场 else None
        单调ok = 止损 < 首入场 <= 红线 and 加仓 <= 首入场 \
            and 止损幅 is not None and 止损幅 <= -_MIN_STOP_PCT * 100 + 1e-9

        lines = [
            f"入场方式: {method}（T层={t_tier or '—'}｜首入场=开盘附近限价·加仓=回踩MA5）",
            f"首入场限价: {round(首入场,3)}（限价=D0收·不追高开）　现价: {round(现价,3)}",
            f"加仓位(回踩MA5): {round(加仓,3)}　止损: {round(止损,3)}"
            + (f"（{止损幅:+.1f}%·下限{-frac*100:.1f}%）" if 止损幅 is not None else ""),
            f"红线(不追高上限): {round(红线,3)}　单调性: {'OK' if 单调ok else '⚠违规'}",
        ]
        return ToolResult(
            name=self.name,
            塔层=self.塔层,
            as_of=as_of,
            code=code,
            浓缩块=浓缩块(lines),
            fields={
                "入场方式": method,
                "T层": t_tier,
                "entry_rule": "limit",           # A8：与 d3_score/model_a 撮合同口径
                "首入场限价": round(首入场, 3),
                "加仓位": round(加仓, 3),
                "挂单价": round(首入场, 3),        # 兼容旧字段名（=首入场限价）
                "止损价": round(止损, 3),
                "不追高上限": round(红线, 3),
                "现价": round(现价, 3),
                "止损幅pct": round(止损幅, 2) if 止损幅 is not None else None,
                "止损下限pct": round(-frac * 100, 2),
                "atr": round(atr, 3) if isinstance(atr, (int, float)) else None,
                "单调性ok": bool(单调ok),
            },
            freshness="fresh",
            防未来=True,
            source=self.source,
        )


register(EntryPriceTool())
