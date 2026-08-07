"""板块轮动 RRG(相对轮动图)—— 合议体系 P3「板块轮动」专家的计算内核(F8)。

设计权威:docs/参考/选股与收益支点策略_网络调研.md §2.1;设计稿 §五(RRG 独立同级专家 P3);
实现需求 F8。定位:把 V1 已有的「个股/板块 RS(收益率差)」扩到**行业层的二阶动量**,
用两个绕 100 中枢的标准化指标把每个行业落到四象限,给个股按所属行业象限一个方向票。

口径(与 pattern_screener/rs.py 同源思路,均为"相对基准的强弱",此处加二阶动量):
    RS 线      rs[t]        = 100 × 行业指数[t] / 基准指数[t]        # 相对强度线(基准=沪深300)
    RS-Ratio   ratio[t]     = 100 × rs[t] / SMA(rs, win_ratio)[t]   # rs 相对自身均线,>100=走强
    RS-Momentum mom[t]      = 100 × ratio[t] / SMA(ratio, win_mom)[t]# ratio 的变化率,>100=动量转强(领先反转)
四象限(中枢=100):
    领先 = 强(ratio≥100)+ 动量强(mom≥100)      → 看多
    改善 = 弱(ratio<100)+ 动量转强(mom≥100)     → 看多(早期,置信弱)
    走弱 = 强(ratio≥100)+ 动量转弱(mom<100)     → 中性(强但动能流失,不追不空)
    落后 = 弱(ratio<100)+ 动量弱(mom<100)       → 看空

参数真源:tools/config/strategy.THRESHOLDS["板块轮动"](窗口/中枢/归一尺度/充分样本)。
数据来源:collectors/board.load_board_kline(行业指数)+ collectors/index.load_index(沪深300 基准),
          **只经 store 读、不触网**;缺数据 → 该行业不入表 → 专家层弃权(中性+置信度0,可见)。

依赖方向:分析层。依赖 config + collectors(只读 store);**不 import web/serialize/council/contracts**。
本模块只产"分析结果 dict",由 experts.py 包装成 ExpertVerdict(职责分离,不在此引契约)。

⚠️ 数据对齐说明(落地前需标定,见 docs/策略/板块轮动RRG.md):
  - 行业名对齐:record.meta.industry / board_membership(baostock 证监会行业)/ board_kline(申万一级)
    三处命名口径可能不一致;不一致时本专家**优雅弃权**(查不到该行业的 board_kline 即中性+置信度0),
    绝不伪造信号。名称映射的标定属后续工作。
  - 日期对齐:沿用 rs.py 既有约定——按尾部等长对齐(min 长度取尾),不做逐日 date-join(P3 骨架简化)。
"""
from __future__ import annotations

import logging

from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("analysis.rrg")

_C = THRESHOLDS["板块轮动"]

# 四象限 → (方向, 是否用于正贡献强度)
象限枚举 = ("领先", "改善", "走弱", "落后")


# ————————————————————————————————————————————————
# 纯计算(无 store / 无网络,便于单测)
# ————————————————————————————————————————————————
def _closes(x) -> list[float]:
    """DataFrame(取 close)/ Series / 序列 → list[float](与 rs.py 同款抽取)。"""
    if hasattr(x, "columns") and "close" in getattr(x, "columns"):
        return [float(v) for v in x["close"].tolist()]
    if hasattr(x, "tolist"):
        return [float(v) for v in x.tolist()]
    return [float(v) for v in x]


def _min_bars() -> int:
    """产出一个 RS-Momentum 最新值所需的最少 RS 点数 = win_ratio + win_mom − 1。"""
    return int(_C["RS_Ratio窗口"]) + int(_C["RS_Momentum窗口"]) - 1


def rs_line(board_closes: list[float], bench_closes: list[float]) -> list[float]:
    """RS 线 = 100 × 行业 / 基准,尾部等长对齐(基准某根为0则该行业整体判不可用→上层弃权)。"""
    n = min(len(board_closes), len(bench_closes))
    b, k = board_closes[-n:], bench_closes[-n:]
    if any(ki == 0 for ki in k):
        raise ValueError("基准存在 0 收盘,RS 不可计算")
    return [100.0 * bi / ki for bi, ki in zip(b, k)]


def _sma_series(seq: list[float], win: int) -> list[float]:
    """对每个 t(t+1≥win)算尾部 win 均线,返回与达标位置一一对应的均线序列。"""
    win = int(win)
    return [sum(seq[t + 1 - win:t + 1]) / win for t in range(len(seq)) if t + 1 >= win]


def rs_ratio_series(rs: list[float], win: int | None = None) -> list[float]:
    """RS-Ratio = 100 × rs / SMA(rs, win);长度 = len(rs) − win + 1(不足则空)。"""
    win = int(win or _C["RS_Ratio窗口"])
    if len(rs) < win:
        return []
    sma = _sma_series(rs, win)                      # 对应 rs[win-1:]
    tail = rs[win - 1:]
    return [100.0 * tail[i] / sma[i] if sma[i] else 100.0 for i in range(len(tail))]


def rs_momentum_series(rs_ratio: list[float], win: int | None = None) -> list[float]:
    """RS-Momentum = 100 × ratio / SMA(ratio, win);长度 = len(ratio) − win + 1。"""
    win = int(win or _C["RS_Momentum窗口"])
    if len(rs_ratio) < win:
        return []
    sma = _sma_series(rs_ratio, win)
    tail = rs_ratio[win - 1:]
    return [100.0 * tail[i] / sma[i] if sma[i] else 100.0 for i in range(len(tail))]


def classify(rs_ratio: float, rs_momentum: float) -> str:
    """(RS-Ratio, RS-Momentum) → 四象限名(中枢=config 象限中枢)。"""
    c = float(_C["象限中枢"])
    strong = rs_ratio >= c
    mom_up = rs_momentum >= c
    if strong and mom_up:
        return "领先"
    if strong and not mom_up:
        return "走弱"
    if (not strong) and mom_up:
        return "改善"
    return "落后"


def _tanh(x: float) -> float:
    import math
    return math.tanh(x)


def _direction_strength(象限: str, rs_ratio: float, rs_momentum: float) -> tuple[str, float]:
    """象限 + 两指标偏离中枢 → (方向, 强度[-1,1])。强度符号与方向严格一致(过契约守门)。

    偏离量 r=ratio−中枢、m=mom−中枢(百分点),用 tanh(·/scale) 有界归一:
      领先 → 看多,强度 = +tanh((r+m)/scale)           # 强度动量双正,叠加
      改善 → 看多,强度 = +tanh(m/scale)                # 仅动量为正,信号更弱(早期轮入)
      落后 → 看空,强度 = −tanh((|r|+|m|)/scale)        # 双负,叠加
      走弱 → 中性,强度 = 0                              # 强但动能流失,不追高也不做空
    """
    c = float(_C["象限中枢"])
    scale = float(_C["强度归一scale"])
    r = rs_ratio - c
    m = rs_momentum - c
    if 象限 == "领先":
        return "看多", abs(_tanh((r + m) / scale))
    if 象限 == "改善":
        return "看多", abs(_tanh(m / scale))
    if 象限 == "落后":
        return "看空", -abs(_tanh((abs(r) + abs(m)) / scale))
    return "中性", 0.0                                # 走弱


def compute_series(board_closes: list[float], bench_closes: list[float]) -> dict | None:
    """由两条收盘序列算某行业最新 RRG 状态(纯计算)。样本不足返回 None(→上层弃权)。

    返回 {象限, 方向, 强度, RS_Ratio, RS_Momentum, 数据充分度, 依据}。
    """
    try:
        rs = rs_line(board_closes, bench_closes)
    except ValueError:
        return None
    n = len(rs)
    if n < _min_bars():                              # 连一个动量值都算不出
        return None
    ratio_seq = rs_ratio_series(rs)
    mom_seq = rs_momentum_series(ratio_seq)
    if not mom_seq or not ratio_seq:
        return None
    ratio, mom = round(ratio_seq[-1], 4), round(mom_seq[-1], 4)
    象限 = classify(ratio, mom)
    方向, 强度 = _direction_strength(象限, ratio, mom)
    充分度 = "充分" if n >= int(_C["充分样本"]) else "部分降级"
    依据 = [f"{象限}象限·RS-Ratio {ratio}·RS-Momentum {mom}"]
    return {"象限": 象限, "方向": 方向, "强度": round(强度, 4),
            "RS_Ratio": ratio, "RS_Momentum": mom,
            "数据充分度": 充分度, "依据": 依据}


# ————————————————————————————————————————————————
# store 读取 + 缓存(批量落库/选股时,基准与各行业各算一次)
# ————————————————————————————————————————————————
_UNSET = object()
_BENCH_CACHE = _UNSET          # 基准收盘序列(沪深300),一次读取全程复用
_INDUSTRY_CACHE: dict = {}     # {行业名: row|None},按需惰性计算并记忆(含"算不出"的 None)


def clear_cache() -> None:
    """清空基准/行业缓存。批量任务跨交易日或单测隔离时调用。"""
    global _BENCH_CACHE, _INDUSTRY_CACHE
    _BENCH_CACHE = _UNSET
    _INDUSTRY_CACHE = {}


def _bench_closes() -> list[float] | None:
    """基准(沪深300)收盘序列;缺失/失败 → None(记一次 warning,不抛)。"""
    global _BENCH_CACHE
    if _BENCH_CACHE is _UNSET:
        try:
            from tools.collectors import index
            _BENCH_CACHE = _closes(index.load_index(_C["基准"]))
        except Exception as e:
            logger.warning("RRG 基准 %s 不可用:%s;板块轮动专家将整体弃权", _C["基准"], e)
            _BENCH_CACHE = None
    return _BENCH_CACHE


def industry_row(name: str) -> dict | None:
    """查某行业的最新 RRG 状态(惰性 + 记忆)。数据缺/算不出 → None(→专家弃权)。

    绝不触网、绝不抛异常(所有 IO 包在 try 内);上层 experts.build 依赖它恒稳。
    """
    if not name:
        return None
    if name in _INDUSTRY_CACHE:
        return _INDUSTRY_CACHE[name]
    row = None
    bench = _bench_closes()
    if bench:
        try:
            from tools.collectors import board
            bdf = board.load_board_kline(name)
            row = compute_series(_closes(bdf), bench)
        except FileNotFoundError:
            row = None                               # 该行业无 board_kline(名称口径不一致亦落此)
        except Exception as e:                       # noqa: BLE001 —— 任何异常都降级为弃权,不炸批量
            logger.warning("RRG 行业 %s 计算失败:%s;该行业弃权", name, e)
            row = None
    _INDUSTRY_CACHE[name] = row
    return row
