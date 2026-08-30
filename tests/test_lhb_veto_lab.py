"""lhb_veto_lab.py 评测口径单测(纯离线,合成数据,不触网)。

锁的语义:
- compute_returns **T+1 开盘入场**(防未来函数):收益分母 = 上榜日次一交易日开盘价;
- 否决腿(net_buy dir=+1)净超额为负 → 否决/离场有价值,avoided_underperf = −净超额 为正;
- veto 模块自校验:模块否决集 == 净买腿(生产模块 ↔ 诊断口径对齐);
- 结论判定:显著负 + 逐年一致 → 否决稳定;反转扣成本判定。
"""
import numpy as np
import pandas as pd
import pytest

from tools.backtest import lhb_block_lab as LBL
from tools.backtest import lhb_veto_lab as VL


# ———————————— 防未来函数:compute_returns 用 T+1 开盘入场 ————————————
class _FakeBook:
    """注入价格:一票 5 根 K,open/close 已知,验证入场取 open[T+1]。"""

    def __init__(self, rec):
        self._rec = rec

    def get(self, code):
        return self._rec


class _FakeBench:
    def ret(self, ev_date, h):
        return 0.0            # 基准恒 0 → excess == stk_ret,便于断言


def test_compute_returns_enters_on_next_open():
    # 日期 idx: 0..4;上榜日 T = idx1('2024-01-02')。T+1 开盘=open[2]=10.0
    op = np.array([9.0, 9.5, 10.0, 11.0, 12.0])
    close = np.array([9.2, 9.6, 10.5, 11.5, 13.2])
    hi = close + 0.5
    lo = op - 0.5
    dmap = {f"2024-01-0{i+1}": i for i in range(5)}
    rec = (op, hi, lo, close, dmap)
    events = pd.DataFrame([{"code": "000001", "ev_date": "2024-01-02", "direction": 1,
                            "sig": 10.0, "sig_name": "net_buy_ratio", "inst_buy": 0}])
    ret = LBL.compute_returns(events, _FakeBook(rec), _FakeBench(), horizons=(1, 2))
    r1 = ret[ret["h"] == 1].iloc[0]
    # 上榜日 T=idx1;入场=open[idx+1]=open[2]=10.0,退出 H1=close[idx+1]=close[2]=10.5
    # → r = 10.5/10.0 - 1 = 0.05(分母是 T+1 开盘,不是上榜日收盘)
    assert abs(r1["stk_ret"] - 0.05) < 1e-9
    # 绝不用上榜日当日(idx1)收盘作分母(那会给 10.5/9.6-1≈0.094)
    assert abs(r1["stk_ret"] - (close[2] / close[1] - 1)) > 1e-6


# ———————————— 合成 ret:否决腿负超额 + 自校验对齐 ————————————
def _synth_ret(seed=0):
    """构造 20 个交易日、每日若干票:净买腿(dir=+1)前向跑输,净卖腿(dir=-1)H1 微反弹。"""
    rng = np.random.default_rng(seed)
    rows = []
    days = [f"2024-{m:02d}-{d:02d}" for m in (3, 4) for d in range(1, 11)]
    for ev_date in days:
        for _ in range(8):                       # 净买票:H5 系统性跑输
            for h, drag in ((1, -0.01), (5, -0.03), (10, -0.02)):
                stk = drag + rng.normal(0, 0.005)
                rows.append(dict(code=f"6{rng.integers(1000,9999)}", ev_date=ev_date,
                                 direction=1, sig=float(rng.uniform(1, 30)), h=h,
                                 stk_ret=stk, bench_ret=0.0, excess=stk, inst_buy=0))
        for _ in range(5):                       # 净卖票:H1 小反弹
            for h, eff in ((1, 0.008), (5, 0.0), (10, -0.005)):
                stk = eff + rng.normal(0, 0.004)
                rows.append(dict(code=f"6{rng.integers(1000,9999)}", ev_date=ev_date,
                                 direction=-1, sig=float(-rng.uniform(1, 20)), h=h,
                                 stk_ret=stk, bench_ret=0.0, excess=stk, inst_buy=0))
    return pd.DataFrame(rows)


def test_net_buy_leg_negative_excess_means_veto_value():
    ret = _synth_ret()
    panel = VL._panel_by_direction(ret)
    h5_buy = panel["H5"]["net_buy(dir=+1)"]
    assert h5_buy["net_excess"] < 0                       # 净买腿跑输
    assert h5_buy["avoided_underperf_net"] > 0            # 否决避免的跑输为正
    assert h5_buy["net_p"] is not None and h5_buy["net_p"] < 0.1   # 显著


def test_module_self_check_alignment():
    """veto 模块否决集 == 净买腿(dir=+1)。"""
    ret = _synth_ret()
    events = ret[ret["h"] == 5][["code", "ev_date", "direction", "sig"]].copy()
    chk = VL._module_self_check(events, ret)
    assert chk["veto_equals_net_buy_leg"] is True
    assert chk["n_vetoed"] == int((events["direction"] == 1).sum())
    assert chk["vetoed_H5_net_excess"] < 0


def test_verdict_flags():
    ret = _synth_ret()
    panel = VL._panel_by_direction(ret)
    by_year = VL._panel_by_year(ret)
    verdict = VL._verdict(panel, by_year)
    assert verdict["否决腿H5显著负(见光死)"] is True
    assert verdict["否决腿逐年净超额均为负"] is True
    assert isinstance(verdict["建议用法"], str) and verdict["建议用法"]


def test_ratio_bucket_monotone_smoke():
    """分档能跑通并给出各档净超额(不强断言单调,只锁结构)。"""
    ret = _synth_ret()
    buckets = VL._panel_by_ratio_bucket(ret)
    assert "H5" in buckets
    h5 = buckets["H5"]
    if isinstance(h5, list):                 # 样本足够时应分出多档
        assert all("net_excess" in b for b in h5)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
