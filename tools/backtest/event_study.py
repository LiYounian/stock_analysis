"""事件级前瞻收益(event study)—— 事件驱动策略的验证框架(F7)。

用途:给定事件表(某票某日发生某类事件)+ 该票 K 线(+可选基准),算**事件后 N 日前瞻收益**
与相对基准 **Alpha**,用于验证 PEAD/增减持等事件的历史有效性(如漂移窗 5/10/20 日胜率)。

与 BT.1(engine.py 信号回测)的关系:**独立新增,不改 engine.py**。BT.1 回放逐日买卖信号;
本模块只做"事件对齐 + 前瞻窗测量",是事件类策略的评估入口。

防未来函数纪律(与 BT.1 同源):
  · 进场锚定在**事件日当日或其后第一个交易日**(t0);前瞻收益只用 t0 及**之后**的价 → 不回看。
  · 事件本身在 t0 已公开(业绩预告/快报/增减持公告都有公告日),不构成前视。
  · **本模块用于历史评估,不喂给"实时专家"的强度**——实时专家强度来自事件属性(超预期幅度),
    绝不能用"未来 N 日已实现收益"当实时信号(那才是未来函数)。二者严格分开。

依赖:仅 pandas;不触网、不 import web/report/serialize/engine。
"""
from __future__ import annotations

import pandas as pd


def _first_idx_on_or_after(dates: list, t0: pd.Timestamp) -> int | None:
    """返回首个 >= t0 的交易日下标(事件日非交易日则顺延)。无则 None。"""
    for i, d in enumerate(dates):
        if d >= t0:
            return i
    return None


def forward_returns(event_dates, kline_df: pd.DataFrame, *,
                    windows=(5, 10, 20), price_col: str = "close",
                    benchmark_df: pd.DataFrame | None = None) -> list[dict]:
    """逐事件算前瞻收益(+可选 Alpha)。

    Args:
        event_dates: 事件日可迭代(str/Timestamp)。
        kline_df: 该票 K 线,含 'date' 与 price_col(升序或乱序皆可,内部排序)。
        windows: 前瞻交易日窗口。
        benchmark_df: 可选基准 K 线(同结构);给了则算 Alpha = 个股前瞻 − 基准前瞻。

    Returns:
        list[{事件日, 进场日, 进场价, 前瞻{N: 收益}, alpha{N: 值}|{}}];
        窗口越界(t0+N 超出数据)该窗记 None(不编造、不回看)。
    """
    if kline_df is None or len(kline_df) == 0 or price_col not in kline_df.columns:
        return []
    df = kline_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    dates = df["date"].tolist()
    P = df[price_col].astype(float).tolist()

    bdates = bP = None
    if benchmark_df is not None and len(benchmark_df) and price_col in benchmark_df.columns:
        b = benchmark_df.copy()
        b["date"] = pd.to_datetime(b["date"])
        b = b.sort_values("date").reset_index(drop=True)
        bdates, bP = b["date"].tolist(), b[price_col].astype(float).tolist()

    def _fwd(prices, idx, n):
        j = idx + n
        if idx is None or j >= len(prices):
            return None
        base = prices[idx]
        return None if not base else round(prices[j] / base - 1.0, 6)

    out = []
    for ed in event_dates:
        t0 = pd.to_datetime(ed)
        i = _first_idx_on_or_after(dates, t0)
        rec = {"事件日": str(t0.date()), "进场日": None, "进场价": None,
               "前瞻": {}, "alpha": {}}
        if i is None:
            out.append(rec)
            continue
        rec["进场日"] = str(dates[i].date())
        rec["进场价"] = round(P[i], 4)
        bi = _first_idx_on_or_after(bdates, t0) if bdates is not None else None
        for n in windows:
            fr = _fwd(P, i, n)
            rec["前瞻"][n] = fr
            if bi is not None and fr is not None:
                bfr = _fwd(bP, bi, n)
                if bfr is not None:
                    rec["alpha"][n] = round(fr - bfr, 6)
        out.append(rec)
    return out


def summarize(events_returns: list[dict], windows=(5, 10, 20)) -> dict:
    """把多事件前瞻收益汇成:各窗均值收益、胜率(>0 占比)、样本数、平均 Alpha。"""
    out = {}
    for n in windows:
        rs = [e["前瞻"].get(n) for e in events_returns if e["前瞻"].get(n) is not None]
        al = [e["alpha"].get(n) for e in events_returns if e.get("alpha", {}).get(n) is not None]
        out[n] = {
            "样本数": len(rs),
            "平均收益": round(sum(rs) / len(rs), 6) if rs else None,
            "胜率": round(sum(1 for r in rs if r > 0) / len(rs), 6) if rs else None,
            "平均Alpha": round(sum(al) / len(al), 6) if al else None,
        }
    return out
