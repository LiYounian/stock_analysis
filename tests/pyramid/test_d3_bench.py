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


def test_resolve_bench_沪深300缺时回退market_ew(tmp_path):
    """A13 核心（hermetic）：沪深300 覆盖不到目标窗时回退全A等权 market_ew。

    不依赖真实 000300 采集到哪天（曾因线上刷新 000300 至 09-18 而误红）——
    自造临时 data-root 锁死"沪深300缺窗"前提：
      · 000300 指数快照只到 D0（09-17，缺 09-18）→ 沪深300 对 D0→D+1 窗返回 None；
      · market_ew 净值覆盖到 D+1（09-18）→ 回退基准有值。
    验 resolve_bench 落到 market_ew 且数值正确。
    """
    import pandas as pd

    # 沪深300 指数：存为 data/raw/<date>/index_kline/000300.parquet（全历史快照），只到 09-17
    bench_dir = tmp_path / "data" / "raw" / "2026-09-17" / "index_kline"
    bench_dir.mkdir(parents=True)
    pd.DataFrame({"date": ["2026-09-16", "2026-09-17"],
                  "close": [4000.0, 4020.0]}).to_parquet(bench_dir / "000300.parquet")
    # 全A等权净值：data/analysis/backtest/finval/market_ew.parquet，覆盖到 09-18
    ew_dir = tmp_path / "data" / "analysis" / "backtest" / "finval"
    ew_dir.mkdir(parents=True)
    pd.DataFrame({"date": ["2026-09-16", "2026-09-17", "2026-09-18"],
                  "ew_index": [1.00, 1.01, 1.02]}).to_parquet(ew_dir / "market_ew.parquet")

    root = str(tmp_path)
    # 前提自检：沪深300 该窗确实缺（D+1 超快照末日），回退源确实有值
    assert D.bench_return("2026-09-17", 1, "2026-09-18", root=root) is None
    r = D.resolve_bench("2026-09-17", 1, "2026-09-18", root=root)
    assert r["value"] is not None, "A13 回退失败：基准仍缺"
    assert r["source"] == "market_ew"
    # 数值正确：ew 09-17→09-18 = 1.02/1.01-1 = 0.99%
    assert r["value"] == round((1.02 / 1.01 - 1) * 100, 2)


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
