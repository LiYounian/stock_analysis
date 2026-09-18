"""D3 基准可得性语义锁（A13/D6）。

锁死：沪深300 覆盖不到目标日（如 000300 只到 09-17、记分执行=09-18）时，
resolve_bench 回退全A等权 market_ew，基准**不再全"缺"**（至少一个源有值）。
bench_return 本身保持 000300 纯口径（其"末日超窗=None"仍由 test_d3_score 锁死，不受影响）。
"""
import os
import pytest

from tools.pyramid import d3_score as D

ROOT = os.environ.get("D3_TEST_DATA_ROOT") or \
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _has_ew():
    return D._load_ew_series(ROOT) is not None


def test_market_ew_return可算():
    ew = D._load_ew_series(ROOT)
    if not ew:
        pytest.skip("无 market_ew 基准")
    days = sorted(ew)
    # 覆盖内一对 → 浮点
    v = D.market_ew_return(days[-3], 1, days[-1], root=ROOT)
    assert isinstance(v, float)
    # 防未来：记分执行日之后的窗口取不到 → None
    assert D.market_ew_return(days[-1], 1, days[-1], root=ROOT) is None


def test_resolve_bench_沪深300优先():
    bdf = D._load_bench_df(ROOT)
    if bdf is None:
        pytest.skip("无沪深300")
    maxd = str(bdf["date"].max())[:10]
    d0 = str(bdf["date"].iloc[-2])[:10]
    r = D.resolve_bench(d0, 1, maxd, root=ROOT)
    assert r["value"] is not None and r["source"] == "沪深300"


def test_resolve_bench_沪深300缺时回退market_ew():
    """A13 核心：as_of=09-17 / score_asof=09-18，000300 无 09-18 → 回退 market_ew。"""
    if not _has_ew():
        pytest.skip("无 market_ew 基准")
    r = D.resolve_bench("2026-09-17", 1, "2026-09-18", root=ROOT)
    # 000300 到 09-17 → 沪深300 该窗为 None → 应回退 market_ew（有值）
    assert r["value"] is not None, "A13 回退失败：基准仍缺"
    assert r["source"] == "market_ew"


def test_D6_基准可得性_记分卡不再全缺():
    """D6：as_of=09-17 记分执行=09-18 时，口径B 至少一部分票有基准（非全"基准缺"）。"""
    pick_dir = os.path.join(ROOT, "docs", "每日分析", "选股")
    if not os.path.isdir(pick_dir) or not any(
        f.startswith("2026-09-17_金字塔_") for f in os.listdir(pick_dir)
    ):
        pytest.skip("无 9-17 选股文件")
    if not _has_ew():
        pytest.skip("无 market_ew 基准")
    sc = D.build_scorecard("2026-09-17", windows=(1,), score_asof="2026-09-18", root=ROOT)
    covered = 0
    for p in sc["sources"]:
        s = p["summary"].get(1, {})
        if s.get("n") and s.get("命中率B") is not None:
            covered += s.get("基准覆盖", 0)
    assert covered > 0, "D6 失败：所有票口径B 全'基准缺'"
