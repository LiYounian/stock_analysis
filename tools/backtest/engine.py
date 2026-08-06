"""回测引擎:历史回放策略 → 绩效 → 与买入持有基准对比 → 落盘。

命门(walk-forward,严禁未来函数)
--------------------------------
第 t 个交易日的信号**只用 kline.iloc[:t+1]**(≤t 数据)算出:引擎对每个 t 单独
把切片喂给策略、只取切片最后一根的信号。这样即便被测策略实现里偷看了输入序列的
未来(非因果),引擎的逐日切片也把未来挡在门外——信号 t 永远看不到 t 之后的行。
成交:t 日信号在 **t+1** 按 `price` 列成交(执行滞后 1 根),故信号 t 只能吃到
t+1 及以后的价格波动,天然无法用未来定价。测试 tests/test_backtest.py 用一个
**故意非因果**的假策略 + 篡改未来极端值,钉死"改未来不改历史回测"。

本轮范围(BT.1):只实现 **信号策略(kind="信号")** 回测——最契合现有数据模型
(store.get_raw("kline", code) 单票时序)。
  · **选股策略(kind="选股")本轮不做并显式抛 NotImplementedError**。原因:选股回测
    需要"历史每个调仓时点的**当时可见**中心记录"(records 快照)来在过去某天重放
    选股;而中心记录(data/analysis/{code}.json)只有**最新**一份、无历史时点版本,
    照搬会直接引入未来函数。补齐历史 records 快照是独立工程,留待 BT.2/后续。
  · 评分策略(kind="评分")同理不在 BT.1 范围,一并抛 NotImplementedError。

依赖方向(守 docs/开发规范.md §5.1):只依赖 store(历史 raw)/ strategy(被测策略)
/ config(参数);**不触网、不产中心记录、不重算指标、不 import web/report/serialize**。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from tools.backtest import metrics
from tools.config import settings
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store
from tools.strategy import registry

logger = logging.getLogger("backtest.engine")

# 参数单一真源:config/strategy.py['回测'](函数入参可覆盖)
_BT_CFG = THRESHOLDS.get("回测", {})
_DEFAULT_COST_BPS = float(_BT_CFG.get("cost_bps", 5.0))
_DEFAULT_PRICE = str(_BT_CFG.get("price", "close"))
_DEFAULT_REBALANCE = int(_BT_CFG.get("rebalance_days", 5))
_PERIODS_PER_YEAR = int(_BT_CFG.get("年化交易日", 244))

_DISCLAIMER = "历史回测≠未来保证,非投资建议"
_OUT_DIR = settings.PROJECT_ROOT / "data" / "analysis" / "backtest"


# ————————————————————————————————————————————————
# 对外契约
# ————————————————————————————————————————————————
def backtest(strategy_name: str, codes, start: str, end: str, *,
             rebalance_days: int | None = None, cost_bps: float | None = None,
             price: str | None = None, benchmark: str = "equal") -> dict:
    """回放策略 → 绩效。返回 需求.md 约定的 dict。

    Args:
        strategy_name: 已注册策略名(经 registry)。kind 必须是 "信号"。
        codes: 单票代码 str,或多票 list[str](多票=等权组合日收益)。
        start, end: 回测区间(含端点,"YYYY-MM-DD"),按 kline 'date' 列过滤。
        cost_bps: 单边成本(手续费+滑点,基点);买卖各扣一次。None→config 默认。
        price: 成交价列(次日按此列成交)。None→config 默认("close")。
        benchmark: 基准,当前仅 "equal"=等权**买入持有**(全程满仓)。
        rebalance_days: 仅选股回测用(本轮未实现)。

    Returns:
        {策略, 类型, 区间, 代码, 绩效{累计收益,年化,最大回撤,夏普,胜率,交易次数},
         基准{...买入持有...}, 超额, 明细ref, 免责}

    Raises:
        NotImplementedError: kind ∈ {选股, 评分}(见模块 docstring 决策)。
        KeyError: 策略未注册。
    """
    cost_bps = _DEFAULT_COST_BPS if cost_bps is None else float(cost_bps)
    price = _DEFAULT_PRICE if price is None else str(price)
    rebalance_days = _DEFAULT_REBALANCE if rebalance_days is None else int(rebalance_days)
    cost_rate = cost_bps / 10000.0

    code_list = [codes] if isinstance(codes, str) else list(codes)
    if not code_list:
        raise ValueError("codes 为空,无票可回测")

    kind = registry.get(strategy_name).kind
    if kind == "选股":
        raise NotImplementedError(
            f"选股回测本轮(BT.1)不做:{strategy_name!r} kind=选股。"
            "选股回放需'历史每个调仓时点的当时可见中心记录'快照,现只有最新一份 records、"
            "无历史时点版本,直接用会引入未来函数。补齐历史 records 快照后于 BT.2 实现。")
    if kind != "信号":
        raise NotImplementedError(
            f"BT.1 仅支持 kind='信号';{strategy_name!r} kind={kind!r} 暂不支持。")

    # —— 逐票信号回测,再等权合成组合 ——
    per_code = {}
    for code in code_list:
        per_code[code] = _signal_backtest_single(
            strategy_name, code, start, end, price=price, cost_rate=cost_rate)

    strat_ret, bench_ret = _combine(per_code)
    trades: list[float] = []
    n_trades = 0
    for r in per_code.values():
        trades.extend(r["trades"])
        n_trades += r["n_trades"]

    perf = _perf_block(strat_ret, trades, n_trades)
    bench = _perf_block(bench_ret, [], 0, is_benchmark=True)
    excess = round(perf["累计收益"] - bench["累计收益"], 8)

    equity = (1.0 + strat_ret).cumprod() if len(strat_ret) else pd.Series(dtype=float)
    detail_ref = _dump(strategy_name, code_list, start, end, strat_ret, bench_ret, equity,
                       perf, bench, excess, cost_bps, price)

    return {
        "策略": strategy_name,
        "类型": "信号",
        "区间": [start, end],
        "代码": code_list,
        "绩效": perf,
        "基准": bench,
        "超额": excess,
        "明细ref": detail_ref,
        "免责": _DISCLAIMER,
    }


# ————————————————————————————————————————————————
# 单票信号回测(walk-forward 命门在此)
# ————————————————————————————————————————————————
def _signal_backtest_single(strategy_name: str, code: str, start: str, end: str,
                            *, price: str, cost_rate: float) -> dict:
    """单票:取 kline → 逐日 walk-forward 信号 → t+1 成交 → 日收益/交易/基准。

    返回 {ret: Series(index=date), bench: Series(index=date), trades: list, n_trades}。
    """
    df, dates, P = _load_prices(code, start, end, price)
    n = len(P)
    if n < 2:
        logger.warning("回测 %s 有效样本 %d 根 (<2),按空处理", code, n)
        empty = pd.Series(dtype=float)
        return {"ret": empty, "bench": empty, "trades": [], "n_trades": 0}

    signals = _walk_forward_signals(strategy_name, df)
    target = _targets(signals)                       # 逐日目标仓位 ∈ {0,1}

    # 执行滞后 1 根:t 日信号(→target[t])在 t+1 生效 → pos_eff[k]=target[k-1]
    pos_eff = [0] + [target[k - 1] for k in range(1, n)]

    price_ret = [None] + [P[k] / P[k - 1] - 1.0 for k in range(1, n)]
    strat = []
    for k in range(1, n):
        turnover = abs(pos_eff[k] - pos_eff[k - 1])   # 仓位变动 → 成交
        strat.append(pos_eff[k] * price_ret[k] - cost_rate * turnover)

    idx = dates[1:] if hasattr(dates, "__getitem__") else range(1, n)
    ret = pd.Series(strat, index=list(idx), dtype=float)

    # 基准:买入持有(第1日买、末日卖,各扣一次成本)
    bench_vals = list(price_ret[1:])
    bench_vals[0] -= cost_rate
    bench_vals[-1] -= cost_rate
    bench = pd.Series(bench_vals, index=list(idx), dtype=float)

    trades = _extract_trades(pos_eff, P, cost_rate)
    return {"ret": ret, "bench": bench, "trades": trades, "n_trades": len(trades)}


def _walk_forward_signals(strategy_name: str, df: pd.DataFrame) -> list[str]:
    """命门:逐 t 只把 df.iloc[:t+1] 喂给策略,取切片末根信号。O(n²) 但诚实无未来。"""
    n = len(df)
    out: list[str] = []
    for t in range(n):
        sig = registry.run(strategy_name, df.iloc[: t + 1])
        out.append(sig[-1] if len(sig) else "持")
    return out


def _targets(signals: list[str]) -> list[int]:
    """信号 → 目标仓位:买→1、卖→0、持→沿用前值(初始 0=空仓)。"""
    pos, cur = [], 0
    for s in signals:
        if s == "买":
            cur = 1
        elif s == "卖":
            cur = 0
        pos.append(cur)
    return pos


def _extract_trades(pos_eff: list[int], P: pd.Series, cost_rate: float) -> list[float]:
    """从有效仓位序列抽 round-trip 交易收益(供胜率/交易次数)。

    0→1 建仓(entry=当日成交价),1→0 平仓(exit=当日成交价);末日仍持仓则末价平掉。
    每笔扣两次单边成本(买+卖)。
    """
    trades: list[float] = []
    entry = None
    for k in range(len(pos_eff)):
        prev = pos_eff[k - 1] if k > 0 else 0
        cur = pos_eff[k]
        if prev == 0 and cur == 1:
            entry = float(P[k])
        elif prev == 1 and cur == 0 and entry is not None:
            trades.append(float(P[k]) / entry - 1.0 - 2.0 * cost_rate)
            entry = None
    if entry is not None:                              # 末日仍持仓 → 末价平仓
        trades.append(float(P.iloc[-1]) / entry - 1.0 - 2.0 * cost_rate)
    return trades


# ————————————————————————————————————————————————
# 组合合成 / 绩效 / 落盘
# ————————————————————————————————————————————————
def _combine(per_code: dict) -> tuple[pd.Series, pd.Series]:
    """多票等权:按 date 外连接逐日收益,缺失日记 0(该日未持仓),再跨票取均值。"""
    rets = [r["ret"] for r in per_code.values() if len(r["ret"])]
    benchs = [r["bench"] for r in per_code.values() if len(r["bench"])]
    if not rets:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    strat = pd.concat(rets, axis=1).fillna(0.0).mean(axis=1).sort_index()
    bench = pd.concat(benchs, axis=1).fillna(0.0).mean(axis=1).sort_index()
    return strat, bench


def _perf_block(ret: pd.Series, trades: list[float], n_trades: int,
                *, is_benchmark: bool = False) -> dict:
    """把日收益序列压成绩效 dict。"""
    equity = (1.0 + ret).cumprod() if len(ret) else pd.Series(dtype=float)
    block = {
        "累计收益": round(metrics.cum_return(ret), 6),
        "年化": round(metrics.annualized(ret, _PERIODS_PER_YEAR), 6),
        "最大回撤": round(metrics.max_drawdown(equity), 6),
        "夏普": round(metrics.sharpe(ret, periods_per_year=_PERIODS_PER_YEAR), 6),
    }
    if not is_benchmark:
        block["胜率"] = round(metrics.win_rate(trades), 6)
        block["交易次数"] = int(n_trades)
    return block


def _dump(strategy_name, code_list, start, end, strat_ret, bench_ret, equity,
          perf, bench, excess, cost_bps, price) -> str:
    """落 data/analysis/backtest/{strategy}_{codes}.json,返回相对项目根的路径。"""
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = "-".join(code_list) if len(code_list) <= 3 else f"{code_list[0]}等{len(code_list)}票"
    fname = f"{strategy_name}_{tag}.json"
    path = _OUT_DIR / fname
    payload = {
        "策略": strategy_name, "类型": "信号", "区间": [start, end], "代码": code_list,
        "成交假设": {"cost_bps": cost_bps, "price": price, "执行滞后": "t 日信号 t+1 成交"},
        "绩效": perf, "基准": bench, "超额": excess,
        "净值曲线": {str(d): round(float(v), 6) for d, v in equity.items()},
        "免责": _DISCLAIMER,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        return str(path.relative_to(settings.PROJECT_ROOT))
    except ValueError:
        return str(path)


def _load_prices(code: str, start: str, end: str, price: str):
    """取单票 kline(经 store)→ 按 [start,end] 过滤 → 返回 (df, dates, 价格Series)。"""
    df = store.get_raw("kline", code)
    df = df.reset_index(drop=True)
    if "date" in df.columns:
        d = pd.to_datetime(df["date"])
        mask = (d >= pd.to_datetime(start)) & (d <= pd.to_datetime(end))
        df = df.loc[mask].reset_index(drop=True)
        dates = [str(pd.to_datetime(x).date()) for x in df["date"].tolist()]
    else:
        dates = list(range(len(df)))
    col = price if price in df.columns else "close"
    P = df[col].astype(float).reset_index(drop=True)
    return df, dates, P
