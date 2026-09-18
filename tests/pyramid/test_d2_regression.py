"""D2 · 骨架回归基线 + 回测底座语义锁。

回归基线：9-17 top30 存档为 fixture（当前 main 基线）；每次改口径/排序重跑对照，列进出票。
⚠️ 窗B 正在改 build_skeleton 排序(A10/A11)——此 fixture 是**轮1初始基线**，
   轮1全部合并后需统筹**重新 bless**（更新 fixture）。届时本用例期望失败=提醒 bless，非 bug。

回测底座：Spearman rank IC 纯函数 + 交易日历（无数据依赖，恒可跑）。
"""
import json
import os
import pytest

from tools.pyramid import d2_backtest as B

ROOT = os.environ.get("D3_TEST_DATA_ROOT") or \
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "d2_skeleton_2026-09-17_top30.json")


# ══════════════ 回测底座纯函数（无数据依赖）══════════════
def test_spearman_单调():
    xs = [1, 2, 3, 4, 5]
    assert B._spearman(xs, [10, 20, 30, 40, 50]) == 1.0        # 完全同序
    assert B._spearman(xs, [50, 40, 30, 20, 10]) == -1.0       # 完全反序


def test_spearman_并列与退化():
    # 并列用平均秩，仍可算
    assert isinstance(B._spearman([1, 1, 2, 3, 3], [1, 2, 3, 4, 5]), float)
    # 一侧零方差 → None（不编相关性）
    assert B._spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None
    # 样本不足 → None
    assert B._spearman([1, 2], [3, 4]) is None


def test_avg_ranks_并列():
    # [10,10,20] → 前两名并列取秩(1,2)平均=1.5，第三名=3
    assert B._avg_ranks([10, 10, 20]) == [1.5, 1.5, 3.0]


def test_filled_ret_口径():
    assert B._filled_ret({"filled": True, "ret_pct": 2.5}) == 2.5
    assert B._filled_ret({"filled": False}) is None          # 踏空剔出
    assert B._filled_ret({"insufficient": True}) is None      # 未满剔出


# ══════════════ 交易日历（需 market_ew/沪深300 之一）══════════════
def test_trading_days_区间():
    days = B._trading_days("2026-09-10", "2026-09-18", ROOT)
    if not days:
        pytest.skip("无基准日历源")
    assert days == sorted(days)                # 升序
    assert all("2026-09-10" <= d <= "2026-09-18" for d in days)
    # 周末不入（09-12/09-13 为周六日）
    assert "2026-09-12" not in days and "2026-09-13" not in days


# ══════════════ D2 回归基线：9-17 top30（需 K线数据·慢·缺则 skip）══════════════
def test_d2_regression_top30对照基线():
    if not os.path.exists(FIXTURE):
        pytest.skip("无 fixture")
    if not os.path.isdir(os.path.join(ROOT, "data", "master", "kline")):
        pytest.skip("无 K线数据，跳过重跑对照")
    fx = json.loads(open(FIXTURE, encoding="utf-8").read())
    base_codes = [r["code"] for r in fx["top30"]]

    from tools.pyramid.d2_compose import build_skeleton
    sk = build_skeleton("2026-09-17", root=ROOT, scan_kline=True)
    cur_codes = [r.code for r in sk["排序"][:30]]

    entered = [c for c in cur_codes if c not in base_codes]   # 新进
    exited = [c for c in base_codes if c not in cur_codes]     # 掉出
    msg = (f"\n9-17 top30 相对基线({fx.get('base_sha')})变化："
           f"\n  进票={entered}\n  出票={exited}"
           f"\n（若窗B排序改动已合入 → 属预期，请统筹重新 bless {os.path.basename(FIXTURE)}）")
    assert cur_codes == base_codes, msg
