"""回测层单测(BT.1)。断言锁住"为什么这么设计"的语义:

metrics(纯函数)
  - cum_return / max_drawdown / sharpe / annualized / win_rate 用已知序列钉死数值口径。

engine 信号回测
  - **t+1 成交**:t 日信号只影响 t+1 及以后收益(执行滞后 1 根),不吃当根波动。
  - **成本生效**:仓位变动当日扣单边成本。
  - **基准对比**:输出含"买入持有"基准块。
  - **命门·防未来函数**:用一个**故意非因果**的假策略(看全序列最大值),把未来某根
    改成极端值,历史(更早日期)的逐日回测收益**必须一字不变**——证明引擎逐日切片
    (只喂 ≤t 数据),没把未来泄露进早期信号。
  - 选股/评分策略在 BT.1 抛 NotImplementedError(见 engine.py 决策)。
"""
import numpy as np
import pandas as pd
import pytest

from tools.backtest import engine, metrics
from tools.strategy import registry as reg


# ————————————————————————————————————————————————
# fake 策略(登记进注册表,guard 重名)
# ————————————————————————————————————————————————
def _momentum(df):
    """因果动量:今收 > 昨收 → 买,< → 卖,= → 持;首根持。仅用 ≤t 数据。"""
    closes = df["close"].tolist()
    out = ["持"]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            out.append("买")
        elif closes[i] < closes[i - 1]:
            out.append("卖")
        else:
            out.append("持")
    return out


def _noncausal_newhigh(df):
    """**故意非因果**:买 iff 收盘 == 整段序列最大值。给全序列会偷看未来;
    引擎若逐日切片喂 ≤t 数据,则退化为'创历史新高才买'(因果)。命门测试的探针。"""
    closes = df["close"].tolist()
    mx = max(closes)
    return ["买" if c >= mx else "持" for c in closes]


def _register(name, fn):
    if name not in reg.list_strategies():
        reg.register(name, "信号", fn)


_register("测试_动量", _momentum)
_register("测试_非因果新高", _noncausal_newhigh)


def _mk_kline(closes):
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="D"),
        "open": list(closes),
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": list(closes),
        "volume": [1000.0] * n,
    })


@pytest.fixture
def patch_store(monkeypatch, tmp_path):
    """monkeypatch store.get_raw + 落盘目录,使回测吃内存 kline、不污染 repo。"""
    holder = {"df": None}
    monkeypatch.setattr(engine.store, "get_raw",
                        lambda kind, code: holder["df"].copy())
    monkeypatch.setattr(engine, "_OUT_DIR", tmp_path)
    return holder


_FULL = ("2026-01-01", "2026-12-31")   # 覆盖全部构造日期


# ================================================================
# metrics 数值(已知序列钉死)
# ================================================================
def test_cum_return():
    assert metrics.cum_return([0.1, -0.1]) == pytest.approx(1.1 * 0.9 - 1)
    assert metrics.cum_return([]) == 0.0


def test_max_drawdown():
    # 净值 1→2→1.5→3→1.5:峰值 3 回撤到 1.5 = -0.5(比 2→1.5 的 -0.25 更深)
    assert metrics.max_drawdown([1, 2, 1.5, 3, 1.5]) == pytest.approx(-0.5)
    assert metrics.max_drawdown([1, 2, 3]) == pytest.approx(0.0)   # 单调升无回撤
    assert metrics.max_drawdown([]) == 0.0


def test_sharpe():
    # r=[.01,.02,.03]:mean=.02,std(ddof=1)=.01 → sharpe=2*sqrt(244)
    assert metrics.sharpe([0.01, 0.02, 0.03]) == pytest.approx(2 * np.sqrt(244))
    # 无波动 → 无夏普(防除零)
    assert metrics.sharpe([0.01, 0.01, 0.01]) == 0.0
    assert metrics.sharpe([0.01]) == 0.0


def test_annualized():
    # periods_per_year == 期数时,指数为 1 → 年化 == 累计
    r = [0.1, -0.05, 0.2]
    assert metrics.annualized(r, periods_per_year=len(r)) == pytest.approx(
        metrics.cum_return(r))
    assert metrics.annualized([]) == 0.0


def test_win_rate():
    assert metrics.win_rate([0.1, -0.2, 0.3, -0.1]) == pytest.approx(0.5)
    assert metrics.win_rate([]) == 0.0


# ================================================================
# 信号回测:t+1 成交 / 成本 / 基准
# ================================================================
def test_execution_lag_next_day(patch_store):
    """t 日'买'信号只让 t+1 及以后进仓,不吃当根收益(证明次日成交)。"""
    closes = [10, 11, 12, 11, 10, 11]
    patch_store["df"] = _mk_kline(closes)
    r = engine._signal_backtest_single("测试_动量", "000001", *_FULL,
                                       price="close", cost_rate=0.0)
    ret = r["ret"]
    # 动量:t1 收 11>10 出'买'(target[1]=1),t+1 成交 → 首个进仓收益在 bar2(k=2)
    # bar1 的收益(k=1)对应 pos_eff[1]=target[0]=0 → 必须为 0(没提前进仓)
    assert ret.iloc[0] == pytest.approx(0.0)
    # bar2 收益 = 12/11-1(此时已持仓)
    assert ret.iloc[1] == pytest.approx(12 / 11 - 1)
    # bar3 仍持仓(target[2]=1) → -1/12
    assert ret.iloc[2] == pytest.approx(11 / 12 - 1)


def test_cost_applied_on_turnover(patch_store):
    """建/平仓当日扣单边成本;对照零成本版本,差额 == cost_rate。"""
    closes = [10, 11, 12, 11, 10, 11]
    patch_store["df"] = _mk_kline(closes)
    free = engine._signal_backtest_single("测试_动量", "000001", *_FULL,
                                          price="close", cost_rate=0.0)["ret"]
    costed = engine._signal_backtest_single("测试_动量", "000001", *_FULL,
                                            price="close", cost_rate=0.01)["ret"]
    # bar2(k=2)建仓:turnover=1 → 扣 0.01
    assert costed.iloc[1] == pytest.approx(free.iloc[1] - 0.01)
    # bar4(k=4)平仓:pos_eff 0 收益 + 扣 0.01 = -0.01
    assert costed.iloc[3] == pytest.approx(-0.01)


def test_backtest_dict_and_benchmark(patch_store):
    """公开 backtest 返回 需求.md 约定 dict,含买入持有基准与超额、免责。"""
    closes = [10, 11, 12, 13, 12, 14]
    patch_store["df"] = _mk_kline(closes)
    out = engine.backtest("测试_动量", "000001", *_FULL, cost_bps=5.0)
    assert out["类型"] == "信号"
    assert out["代码"] == ["000001"]
    for k in ("累计收益", "年化", "最大回撤", "夏普", "胜率", "交易次数"):
        assert k in out["绩效"]
    assert set(out["基准"]) >= {"累计收益", "年化", "最大回撤", "夏普"}
    assert out["超额"] == pytest.approx(out["绩效"]["累计收益"] - out["基准"]["累计收益"])
    assert "非投资建议" in out["免责"]
    assert out["明细ref"].endswith(".json")


def test_screen_strategy_not_implemented(patch_store):
    """选股策略回测本轮不做,必须抛 NotImplementedError(带原因)。"""
    screen_names = reg.list_strategies("选股")
    assert screen_names, "预期注册表里有选股策略(screener 预设)"
    with pytest.raises(NotImplementedError):
        engine.backtest(screen_names[0], "000001", *_FULL)


# ================================================================
# 命门:防未来函数
# ================================================================
def test_no_future_leak(patch_store):
    """把未来某根改成极端值,更早日期的逐日回测收益不得改变。

    用非因果假策略当探针:若引擎一次性把全序列喂给策略(而非逐日切片),
    篡改未来会翻转早期信号 → 早期收益变化 → 本测试失败。逐日切片实现则不变。
    """
    closes = [10, 11, 12, 11, 10, 9]           # 新高在 bar0/1/2
    patch_store["df"] = _mk_kline(closes)
    base = engine._signal_backtest_single("测试_非因果新高", "000001", *_FULL,
                                          price="close", cost_rate=0.0)["ret"]

    fut = list(closes)
    fut[5] = 1000.0                            # 未来最后一根改极端值
    patch_store["df"] = _mk_kline(fut)
    after = engine._signal_backtest_single("测试_非因果新高", "000001", *_FULL,
                                           price="close", cost_rate=0.0)["ret"]

    # bar1..bar4 的收益(不涉及被篡改的 bar5 定价与信号)必须逐一相等
    assert np.allclose(base.values[:4], after.values[:4]), (
        "篡改未来改变了历史回测结果 → 引擎泄露了未来函数")
    # sanity:被改的最后一根(bar5)确实不同,证明篡改真的生效了
    assert base.values[4] != pytest.approx(after.values[4])
