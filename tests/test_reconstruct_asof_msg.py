"""消息面历史重建原型的防未来函数测试(确定性,无网络)。

锁语义:as-of 重建只用『公告日期 ≤ T』的信息——这是历史重建可回测的红线,
未来任何重写都不能破坏(否则回测有未来泄漏,结论作废)。
"""
import pandas as pd

from tools.backtest.reconstruct_asof_msg import reconstruct_asof


def _events():
    return pd.DataFrame({
        "code": ["000001", "000002", "000003", "000004"],
        "简称": ["A", "B", "C", "D"],
        "报告期": ["20240930"] * 4,
        "公告日期": pd.to_datetime(
            ["2024-08-02", "2024-10-15", "2024-11-14", "2024-11-30"]),
        "预告类型": ["略增", "预增", "略增", "预减"],
        "变动幅度": [10.0, 50.0, 5.0, -20.0],
    })


def test_no_future_leak():
    """重建集合内 max(公告日期) 必 <= as_of T。"""
    ev = _events()
    for asof in ["2024-08-01", "2024-08-15", "2024-10-20", "2024-12-31"]:
        panel = reconstruct_asof(ev, asof)
        if not panel.empty:
            assert panel["公告日期"].max() <= pd.Timestamp(asof), \
                f"未来泄漏:as_of={asof} 含更晚公告日"


def test_asof_monotonic_growth():
    """as-of 越晚,已公开事件数单调不减(时间推进只会看到更多消息)。"""
    ev = _events()
    counts = [len(reconstruct_asof(ev, d)) for d in
              ["2024-08-01", "2024-08-15", "2024-10-20", "2024-11-20", "2024-12-31"]]
    assert counts == [0, 1, 2, 3, 4], counts
    # 单调不减
    assert all(counts[i] <= counts[i + 1] for i in range(len(counts) - 1))


def test_boundary_inclusive():
    """公告日 == T 的记录应被纳入(当日已公开)。"""
    ev = _events()
    panel = reconstruct_asof(ev, "2024-10-15")
    assert "000002" in set(panel["code"])  # 公告日正好 2024-10-15
