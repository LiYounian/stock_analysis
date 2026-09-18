"""工具共用底座：K 线加载（688 量能自校）、防未来断言、档位渲染、浓缩块拼装。

各工具窗口复用这些，保证口径一致（G6 口径统一）。
"""
from __future__ import annotations

from typing import Optional, Sequence, Any
import os
import pandas as pd

from tools.config import exchange

# 主档 K 线列（date/open/high/low/close/volume/amount/turnover/pct_chg）
_KL_DATE = "date"


def data_root(root: Optional[str] = None) -> str:
    """数据根：默认主仓（本文件上溯三级）；worktree 里跑须显式传 --data-root 指回主仓。"""
    if root:
        return root
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def assert_no_future(df: pd.DataFrame, as_of: str, date_col: str = _KL_DATE) -> pd.DataFrame:
    """防未来（公理·硬闸）：截断到 as_of 当日及之前，返回截断后的副本。

    绝不返回含 > as_of 的行；调用方只能看到 as_of 可见的历史。
    """
    if date_col not in df.columns:
        raise KeyError(f"防未来断言缺日期列 {date_col!r}")
    d = df.copy()
    d[date_col] = pd.to_datetime(d[date_col])
    cutoff = pd.to_datetime(as_of)
    out = d[d[date_col] <= cutoff].sort_values(date_col).reset_index(drop=True)
    return out


def is_star_code(code: str) -> bool:
    """科创板/创业板注册制中腾讯源曾把成交量再 ×100 的板块（688/689）。

    委托单一真源 `tools.config.exchange.is_star_market`——"哪段是科创板"的判据只写一份，
    腾讯 volume 单位归一的采集层与本底座共用同一处（见 exchange.py is_star_market docstring）。
    """
    return exchange.is_star_market(code)


def load_kline(
    code: str, as_of: str, root: Optional[str] = None, min_bars: int = 1
) -> Optional[pd.DataFrame]:
    """读主档 K 线 parquet，截 ≤as_of，688/689 成交量按 amount/close 反推校正（H2 未合前自校）。

    返回按日期升序的 DataFrame；文件缺失或 as_of 前无 bar 返回 None。
    校正判据：688/689 且 volume 相对 amount/close 反推股数偏大 >20× → ÷100（H2 根因：源头多乘 100）。
    """
    base = os.path.join(data_root(root), "data", "master", "kline", f"{code}.parquet")
    if not os.path.exists(base):
        return None
    df = pd.read_parquet(base)
    df = assert_no_future(df, as_of)
    if len(df) < max(1, min_bars):
        return None
    if is_star_code(code) and {"amount", "close", "volume"} <= set(df.columns):
        # 反推股数 = amount / close（元 / 元/股）；volume 若约为其 100 倍则源头多乘了 100
        implied = (df["amount"] / df["close"]).replace([float("inf")], pd.NA)
        ratio = (df["volume"] / implied).dropna()
        if len(ratio) and ratio.median() > 20:
            df = df.copy()
            df["volume"] = df["volume"] / 100.0
    return df


def volume_ratio(df: pd.DataFrame, window: int = 5) -> Optional[float]:
    """当日量比 = 当日 volume / 前 window 日均 volume（不含当日）。"""
    if "volume" not in df.columns or len(df) < window + 1:
        return None
    prev = df["volume"].iloc[-(window + 1) : -1].mean()
    if not prev:
        return None
    return float(df["volume"].iloc[-1] / prev)


def pos60(df: pd.DataFrame, window: int = 60) -> Optional[float]:
    """收盘在近 window 日 [低,高] 区间的分位（0~1）。"""
    if not {"high", "low", "close"} <= set(df.columns) or len(df) < 2:
        return None
    seg = df.tail(window)
    hi, lo = seg["high"].max(), seg["low"].min()
    if hi <= lo:
        return None
    return float((df["close"].iloc[-1] - lo) / (hi - lo))


def 格档(value: Optional[float], 档位表: Sequence[tuple], 缺省: str = "无档") -> tuple[str, str]:
    """把数值映射到档位。档位表 = [(上界, 档名, 一句解释), ...] 升序；value ≤ 上界即命中。

    返回 (档名, 一句解释)。value 为 None → (缺省, "")。语义锁测试须锁死这张表。
    """
    if value is None:
        return (缺省, "")
    for 上界, 档名, 解释 in 档位表:
        if value <= 上界:
            return (档名, 解释)
    last = 档位表[-1]
    return (last[1], last[2])


def 浓缩块(lines: Sequence[str], max_lines: int = 8) -> str:
    """把若干行拼成浓缩块，超出上限截断并标注（G3：≤8 行）。"""
    kept = [ln for ln in lines if str(ln).strip()][:max_lines]
    return "\n".join(kept)
