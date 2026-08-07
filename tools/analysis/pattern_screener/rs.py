"""相对强度 RS(V1 模块二 F2.2)。

定稿口径:**20 日收益率差**(标的收益 − 基准收益),用于:
  ① 个股 vs 所属行业板块;② 板块指数 vs 沪深 300。
后续升级为 Rank(全市场分位)——接口预留 `rank_rs`,本轮 NotImplementedError。
窗口/达标阈值走 Config(`strategy.THRESHOLDS["形态选股"]["RS"]`)。
需求见 docs/计划/V1_形态选股与市场状态系统.md F2.2。
"""
from __future__ import annotations

from tools.config.strategy import THRESHOLDS

_RS = THRESHOLDS["形态选股"]["RS"]


def _closes(x) -> list[float]:
    """DataFrame(取 close)/ Series / 序列 → list[float]。"""
    if hasattr(x, "columns") and "close" in getattr(x, "columns"):
        return [float(v) for v in x["close"].tolist()]
    if hasattr(x, "tolist"):
        return [float(v) for v in x.tolist()]
    return [float(v) for v in x]


def _ret(closes: list[float], win: int) -> float:
    """近 win 日收益率(百分数)。样本不足抛 ValueError。"""
    if len(closes) < win + 1:
        raise ValueError(f"样本不足:需 {win + 1} 根,仅 {len(closes)}")
    base = closes[-1 - win]
    return (closes[-1] / base - 1.0) * 100.0 if base else 0.0


def compute(target, benchmark, win: int = None) -> float:
    """RS = 标的近 win 日收益% − 基准近 win 日收益%(收益率差,单位:百分点)。

    target/benchmark 可为 kline DataFrame / close Series / 收盘价序列。
    """
    win = int(win or _RS["窗口"])
    return round(_ret(_closes(target), win) - _ret(_closes(benchmark), win), 4)


def is_strong(rs_value: float, kind: str = "个股vs板块") -> bool:
    """RS 是否达标(≥ Config 阈值)。kind ∈ {个股vs板块, 板块vs沪深300}。"""
    key = "个股vs板块_达标" if kind == "个股vs板块" else "板块vs沪深300_达标"
    return rs_value >= _RS.get(key, 0.0)


def rank_rs(*args, **kwargs):
    """RS Rating(全市场分位排名)——V2 升级位,V1 未实现。"""
    raise NotImplementedError("RS Rank(全市场分位)为 V2 升级项;V1 用收益率差 compute()")
