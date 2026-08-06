"""策略层注册表(分析层内)——选股/评分/信号三类可扩充策略的统一登记与调用。

定位:见 docs/信息流转与层职责.md §2.3(B) 策略层 —— 权威接口定义。
职责:**组合分析器输出 + 阈值/规则 → 选股/评分/信号**;策略写成标准签名的函数,
用装饰器 @strategy 登记进注册表;调用方按 name 取用,不关心内部实现(可插拔/可并行/可独测)。

依赖方向(严格上层依赖下层,见 docs/开发规范.md §5.1):
本模块属分析/策略层,只依赖基座(config/contracts)与同层的 screener;
**不被基座依赖、不 import web/report/serialize**。

三类策略的标准签名(区分靠 kind 枚举 + 注册表命名空间):

    选股 Screen  fn(records: dict[code, record]) -> list[code]
        输入全池中心记录,输出通过条件的候选代码列表。

    评分 Score   fn(record: dict) -> dict{"score": float, "依据": list[str]}
        输入单票中心记录,输出综合分 + 可读依据。

    信号 Signal  fn(kline_df) -> list[str]  # 每根 K 线一个,取值 ∈ {"买","卖","持"}
        输入单票时序(历史 K 线,pandas.DataFrame 含 'close' 列,或收盘价序列),
        输出逐日买卖持信号,供回测层回放。首根无历史一律"持"。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("strategy.registry")

# 策略类型枚举(命名空间划分的单一真源)
STRATEGY_KINDS: tuple[str, ...] = ("选股", "评分", "信号")


@dataclass(frozen=True)
class StrategyMeta:
    """一个策略的元信息(注册表条目)。"""
    name: str
    kind: str
    fn: Callable
    params_schema: Optional[dict] = None
    doc: str = ""


# 注册表:name -> StrategyMeta(全局单一真源)
_REGISTRY: dict[str, StrategyMeta] = {}


def strategy(name: str, kind: str, params_schema: Optional[dict] = None):
    """装饰器:把一个函数登记为策略。

    Args:
        name: 策略唯一名(注册表键)。重名抛 ValueError。
        kind: 策略类型,必须 ∈ STRATEGY_KINDS,否则抛 ValueError。
        params_schema: 可选,入参 schema(dict),供调用方/未来 Agent 发现。

    用法::

        @strategy("均线金叉", "信号")
        def golden_cross(kline_df): ...
    """
    if kind not in STRATEGY_KINDS:
        raise ValueError(f"非法策略类型 kind={kind!r},须 ∈ {STRATEGY_KINDS}")

    def deco(fn: Callable) -> Callable:
        if name in _REGISTRY:
            raise ValueError(f"策略重名: {name!r} 已注册(kind={_REGISTRY[name].kind})")
        _REGISTRY[name] = StrategyMeta(
            name=name, kind=kind, fn=fn,
            params_schema=params_schema, doc=(fn.__doc__ or "").strip(),
        )
        logger.debug("注册策略 name=%s kind=%s", name, kind)
        return fn

    return deco


def register(name: str, kind: str, fn: Callable, params_schema: Optional[dict] = None) -> StrategyMeta:
    """命令式登记(非装饰器场景,如批量包装现有 screener 预设)。语义同 @strategy。"""
    strategy(name, kind, params_schema)(fn)
    return _REGISTRY[name]


def get(name: str) -> StrategyMeta:
    """按名取策略元信息(含函数)。未注册抛 KeyError。"""
    if name not in _REGISTRY:
        raise KeyError(f"未注册策略: {name!r}(现有: {sorted(_REGISTRY)})")
    return _REGISTRY[name]


def list_strategies(kind: Optional[str] = None) -> list[str]:
    """列出已注册策略名。传 kind 则按类型过滤(kind 非法抛 ValueError)。"""
    if kind is not None and kind not in STRATEGY_KINDS:
        raise ValueError(f"非法策略类型 kind={kind!r},须 ∈ {STRATEGY_KINDS}")
    names = [n for n, m in _REGISTRY.items() if kind is None or m.kind == kind]
    return sorted(names)


def run(name: str, *args, **kwargs):
    """按名取策略并调用,返回策略结果。未注册抛 KeyError。"""
    return get(name).fn(*args, **kwargs)


# ————————————————————————————————————————————————
# 复用现有 screener 预设:各 filter 组合包装成"选股"策略注册进来
# (不改 screener 源码;screen.PRESETS 是每方案的 filter 列表)
# ————————————————————————————————————————————————
def _register_screener_presets() -> None:
    from tools.screener import screen as _sc

    for _name, _filters in _sc.PRESETS.items():
        def _make(filters):
            def _screen_strategy(records: dict[str, dict]) -> list[str]:
                """选股:复用 screener 预设 filter 组合(见 tools/screener/screen.py)。"""
                return _sc.screen(records, filters)
            return _screen_strategy

        register(_name, "选股", _make(_filters),
                 params_schema={"records": "dict[code, 中心记录]"})


# ————————————————————————————————————————————————
# 示例策略:评分 / 信号 各一(供回测与调用方参考签名)
# ————————————————————————————————————————————————
@strategy("买卖倾向评分", "评分",
          params_schema={"record": "单票中心记录 dict"})
def score_buy_sell(record: dict) -> dict:
    """评分示例:读中心记录已算好的 prediction.买卖倾向,归一为 {score, 依据}。

    展示层/组装层已产出"买卖倾向"结论与得分;评分策略只做**只读汇总**,不重算指标
    (守 docs/开发规范.md §5.1:消费方只读中心记录)。数据缺失时 score=0、依据说明缺失。
    """
    bs = ((record or {}).get("prediction") or {}).get("买卖倾向") or {}
    score = bs.get("得分")
    依据 = list(bs.get("依据") or [])
    if not isinstance(score, (int, float)):
        return {"score": 0.0, "依据": ["prediction.买卖倾向 缺失,score 记 0"]}
    return {"score": float(score), "依据": 依据 or [f"结论={bs.get('结论')}"]}


@strategy("均线金叉", "信号",
          params_schema={"kline_df": "pandas.DataFrame(含 close) 或收盘价序列",
                          "short": "短均线窗口(默认 5)", "long": "长均线窗口(默认 20)"})
def golden_cross(kline_df, short: int = 5, long: int = 20) -> list[str]:
    """信号示例:短均线上穿长均线 → 买;下穿 → 卖;其余 → 持。

    输入:pandas.DataFrame(需含 'close' 列)或直接的收盘价序列(list/Series)。
    输出:与输入等长的 list[str],逐日取值 ∈ {"买","卖","持"};
          前 long 根不足以判金叉,一律"持"(无未来函数,仅用 t 及以前数据)。
    """
    closes = _extract_closes(kline_df)
    n = len(closes)
    out = ["持"] * n
    if n < long + 1:
        return out

    def _ma(seq: list, end: int, w: int):
        # end 为闭区间末位下标;窗口不足返回 None
        if end + 1 < w:
            return None
        window = seq[end + 1 - w: end + 1]
        return sum(window) / w

    for t in range(1, n):
        s_now, s_prev = _ma(closes, t, short), _ma(closes, t - 1, short)
        l_now, l_prev = _ma(closes, t, long), _ma(closes, t - 1, long)
        if None in (s_now, s_prev, l_now, l_prev):
            continue
        if s_prev <= l_prev and s_now > l_now:
            out[t] = "买"
        elif s_prev >= l_prev and s_now < l_now:
            out[t] = "卖"
    return out


def _extract_closes(kline_df) -> list[float]:
    """从 DataFrame(取 'close' 列)或序列里抽出收盘价 list[float]。"""
    if hasattr(kline_df, "columns") and "close" in getattr(kline_df, "columns"):
        return [float(x) for x in kline_df["close"].tolist()]
    if hasattr(kline_df, "tolist"):          # pandas.Series 等
        return [float(x) for x in kline_df.tolist()]
    return [float(x) for x in kline_df]


# 导入即注册内置策略(screener 预设 + 示例)
_register_screener_presets()
