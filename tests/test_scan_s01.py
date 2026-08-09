"""S01 参数敏感性 + 入场确认扫描驱动单测。

锁死语义(改坏即挂):
  · _make_cfg 深拷贝、按需覆盖入场/离场参数,**不污染** THRESHOLDS 默认(隔离性)。
  · _find_signals_cfg **把 cfg 转发到入场判定**——深跌阈值收紧后信号应更少(区别于 find_signals 不转发)。
  · run_combo 汇总里含「即死率(硬止损占比)」= 硬止损离场数 / 已离场数。
  · confirm='t1_nobreak' 走可选入场确认(不改离场算法)。
"""
import pandas as pd

from tools.backtest import position_backtest as pb
from tools.backtest import scan_s01
from tools.pipeline import screen_s01 as s01


def _uptrend(n: int = 260, start: float = 100.0, step: float = 0.5) -> dict:
    close = [start + i * step for i in range(n)]
    open_ = [c - 0.2 for c in close]
    high = [c + 0.1 for c in close]
    low = [o - 0.1 for o in open_]
    return {"open": open_, "high": high, "low": low, "close": close}


def _set_trigger(d: dict, t: int, drop_frac: float) -> None:
    prev_c = d["close"][t - 1]
    d["close"][t] = prev_c + 0.1
    d["open"][t] = d["close"][t] - 1.0
    d["low"][t] = prev_c * (1 + drop_frac)
    d["high"][t] = d["close"][t] + 0.1


def _df(d: dict) -> pd.DataFrame:
    n = len(d["close"])
    return pd.DataFrame({
        "date": pd.bdate_range("2020-01-01", periods=n),
        "open": d["open"], "high": d["high"], "low": d["low"],
        "close": d["close"], "volume": [1000.0] * n,
    })


def test_make_cfg_overrides_and_isolation():
    """覆盖生效 + 不污染全局默认 THRESHOLDS。"""
    default_stop = pb._ALL["离场"]["硬止损系数"]
    default_drop = pb._ALL["入场"]["深跌阈值"]
    cfg = scan_s01._make_cfg(hard_stop=0.94, drop_thr=-0.06)
    assert cfg["离场"]["硬止损系数"] == 0.94
    assert cfg["入场"]["深跌阈值"] == -0.06
    # 全局默认未被改动
    assert pb._ALL["离场"]["硬止损系数"] == default_stop
    assert pb._ALL["入场"]["深跌阈值"] == default_drop


def test_find_signals_forwards_cfg_drop_threshold():
    """深跌阈值转发:-0.03 命中的信号日,收紧到 -0.06 时应被过滤掉。"""
    d = _uptrend()
    t = len(d["close"]) - 1
    _set_trigger(d, t, drop_frac=-0.045)               # 盘中跌 4.5%
    df = _df(d)
    loose = scan_s01._find_signals_cfg(df, scan_s01._make_cfg(drop_thr=-0.03))
    strict = scan_s01._find_signals_cfg(df, scan_s01._make_cfg(drop_thr=-0.06))
    assert t in loose                                  # -0.03 阈值:4.5% 深跌达标
    assert t not in strict                             # -0.06 阈值:4.5% 不够深 → 过滤


def test_run_combo_reports_insta_death_rate():
    """run_combo 汇总含即死率;构造必打硬止损的票 → 即死率=1.0。"""
    d = _uptrend()
    t = 255
    _set_trigger(d, t, drop_frac=-0.05)
    # 信号次日盘中继续跌破硬止损位(P0×0.97)
    p0 = d["close"][t]
    d["low"][t + 1] = p0 * 0.90
    d["high"][t + 1] = p0 * 0.95
    d["close"][t + 1] = p0 * 0.93
    d["open"][t + 1] = p0 * 0.95
    df = _df(d)
    s = scan_s01.run_combo({"X": df}, None, scan_s01._make_cfg(), confirm=None)
    assert s["已离场数"] >= 1
    assert s["即死率(硬止损占比)"] == 1.0
    assert "t统计量" in s and "p值(近似,正态双尾)" in s


def test_run_combo_t1_confirm_drops_signal_on_break():
    """T+1 破低时该信号被确认过滤 → 无确认有交易、T+1确认无交易(隔离入场确认语义)。"""
    d = _uptrend()
    t = len(d["close"]) - 2                             # 倒数第二根为信号日,仅它/末根可能触发
    _set_trigger(d, t, drop_frac=-0.05)                # low[t]=prev_c*0.95(深跌)
    d["low"][t + 1] = d["low"][t] - 1.0                # 末根破信号日最低价(且末根无次日可确认)
    df = _df(d)
    s_no = scan_s01.run_combo({"X": df}, None, scan_s01._make_cfg(), confirm=None)
    s_t1 = scan_s01.run_combo({"X": df}, None, scan_s01._make_cfg(), confirm="t1_nobreak")
    assert s_no["交易数"] >= 1                           # 无确认:信号日 t 建仓
    assert s_t1["交易数"] == 0                           # T+1确认:t 破低被拒、末根无次日 → 全滤掉
