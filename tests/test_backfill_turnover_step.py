"""每日 turnover 兜底 step(`ops.backfill_turnover`)的行为锁。

背景:采集回退网(baostock 补齐)best-effort,失败时当日 turnover 静默落 NaN → S04 单日
放量哑火 / 筹码集中度·成本降级。止血是把 `ops.backfill_turnover --apply` 挂进盘后落地后
(pull_refresh.sh ①.7,②screenall 之前)每日跑一遍,用本票 volume 自证回填。

这些断言锁住"为什么要这个 step"的语义(防未来 prompt / 代码重写时无意删规则):
  · **补 NaN**:落地后的当日缺失行被 volume 自证还原;
  · **在数据落地后跑**:兜底针对主档最新(尾部)行——当日 bar 已 append 进主档才轮到它;
  · **不引入未来函数**:当日行是主档最新行,参考只能取历史行(过去 N 根),不看未来;
  · **幂等**:填过即非 NaN、二次跑不重填、值不变;无法自证的行诚实留 NaN;
  · **补齐网主动告警**:回填量冲高 ⇒ 当日补齐网大面积失效被 volume 救回 → 落 marker。
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from ops import backfill_turnover as bt
from tools.config import units
from tools.store import repo as store


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """把主档写到 tmp,绝不碰共享生产 store(--apply 会真写盘)。"""
    monkeypatch.setattr(store, "_MASTER_DIR", tmp_path / "master")
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path / "raw")
    (tmp_path / "master").mkdir()
    (tmp_path / "raw").mkdir()
    yield store


def _clean(code_n=120, *, float_shares=1.0e8, seed=0):
    """造一票自洽主档:turnover% = 100 × volume / 流通股(与真实主档同构)。"""
    rng = np.random.default_rng(seed)
    vol = 2.0e6 * (1 + 0.4 * rng.random(code_n))
    dates = pd.bdate_range("2026-01-01", periods=code_n)
    close = 10 + np.cumsum(rng.normal(0, 0.1, code_n))
    return pd.DataFrame({
        "date": dates, "open": close, "high": close + 0.2, "low": close - 0.2,
        "close": close, "volume": vol, "amount": vol * close,
        "turnover": vol / float_shares * 100.0, "pct_chg": 0.0,
    })


def _put_with_trailing_gap(code, *, n_gap=1, float_shares=1.0e8, drop_volume=False):
    """落一票主档,并把**最近 n_gap 根**的 turnover 抹成 NaN(模拟当日 fallback_advance)。

    drop_volume=True 时把这些行的 volume 也抹掉 → 无从自证,该 step 应 refuse 留 NaN。
    """
    df = _clean(float_shares=float_shares)
    truth = df["turnover"].copy()                     # 抹除前的真值(供断言还原是否正确)
    df.loc[df.index[-n_gap:], "turnover"] = np.nan
    if drop_volume:
        df.loc[df.index[-n_gap:], "volume"] = np.nan
    store.put_master_kline(code, df, meta={"source": "fallback_advance"})
    return truth


def test_daily_step_fills_trailing_nan(iso):
    """当日落地后尾部缺失行被 volume 自证补齐,历史正常行分毫不动。"""
    code = "600001"
    truth = _put_with_trailing_gap(code, n_gap=1)
    rc = bt.main(["--apply", "--codes", code])
    assert rc == 1                                    # 检出并回填 → dirty
    got = store.get_master_kline(code)
    # 尾行已非 NaN,且等于真值(float_shares 恒定 ⇒ ratio 恒定 ⇒ 还原=真 turnover)
    assert not pd.isna(got.iloc[-1]["turnover"])
    assert got.iloc[-1]["turnover"] == pytest.approx(truth.iloc[-1], rel=1e-9)
    # 历史正常行未被改写
    pd.testing.assert_series_equal(
        got.iloc[:-1]["turnover"].reset_index(drop=True),
        truth.iloc[:-1].reset_index(drop=True),
    )


def test_daily_step_reconstruction_uses_only_past_rows(iso):
    """无未来函数:当日行是主档最新行,还原只用它之前的历史行(过去 N 根参考)。

    构造随流通股缓增(CV 内)的序列 → ratio 随时间漂移,使"取哪些参考"可观测;
    断言尾行还原值 == volume_last × median(紧邻其前的 _BACKFILL_REF 根 ratio),
    即 reconstruction 是**过去行**的函数,不可能读到未来(未来行此刻尚不存在)。
    """
    code = "600002"
    n = 120
    dates = pd.bdate_range("2026-01-01", periods=n)
    vol = np.linspace(2.0e6, 3.0e6, n)
    shares = np.linspace(1.0e8, 1.15e8, n)            # 缓增,窗口内 CV 远低于阈值
    turn = 100.0 * vol / shares
    df = pd.DataFrame({
        "date": dates, "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.0,
        "volume": vol, "amount": vol * 10.0, "turnover": turn, "pct_chg": 0.0,
    })
    df.loc[df.index[-1], "turnover"] = np.nan         # 当日(最新行)缺失
    store.put_master_kline(code, df, meta={"source": "fallback_advance"})

    bt.main(["--apply", "--codes", code])
    got = store.get_master_kline(code)

    ref_n = units._BACKFILL_REF
    past_ratios = (turn[:-1] / vol[:-1])[-ref_n:]     # 尾行之前最近 N 根的 ratio(纯历史)
    expected = vol[-1] * float(np.median(past_ratios))
    assert got.iloc[-1]["turnover"] == pytest.approx(expected, rel=1e-9)


def test_daily_step_is_idempotent(iso):
    """二次跑不重填、值不变;第二轮无缺失可回填 → 退出码 0。"""
    code = "600003"
    _put_with_trailing_gap(code, n_gap=2)
    assert bt.main(["--apply", "--codes", code]) == 1
    first = store.get_master_kline(code)["turnover"].tolist()

    rep2 = bt.backfill_code(code, apply=True)          # 直接看第二轮报告
    assert rep2["filled"] == 0 and rep2["refused"] == 0
    assert bt.main(["--apply", "--codes", code]) == 0  # 已无脏,不再回填
    second = store.get_master_kline(code)["turnover"].tolist()
    assert first == second


def test_daily_step_refuses_unprovable_row_and_keeps_nan(iso):
    """volume 也缺 → 无从自证,诚实留 NaN(交下游有声降级),绝不猜。"""
    code = "600004"
    _put_with_trailing_gap(code, n_gap=1, drop_volume=True)
    rep = bt.backfill_code(code, apply=False)          # 报告应记 refused
    assert rep["filled"] == 0 and rep["refused"] >= 1
    rc = bt.main(["--apply", "--codes", code])
    assert rc == 1                                     # refused 也算检出
    got = store.get_master_kline(code)
    assert pd.isna(got.iloc[-1]["turnover"])           # 仍为 NaN,绝不猜


def test_daily_step_writes_alert_marker_when_fills_exceed_threshold(iso, tmp_path):
    """回填量 ≥ 阈值 ⇒ 落主动告警 marker(补齐网大面积失效被 volume 救回的可见信号)。"""
    codes = [f"60010{i}" for i in range(4)]
    for i, c in enumerate(codes):
        _put_with_trailing_gap(c, n_gap=2, float_shares=1.0e8 + i * 1e6)
    marker = tmp_path / "alarm" / "_TURNOVER_BACKFILL_ALARM.json"
    rc = bt.main(["--apply", "--codes", ",".join(codes),
                  "--alert-marker", str(marker), "--alert-threshold", "3"])
    assert rc == 1
    assert marker.exists()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["summary"]["rows_filled"] >= 3
    assert payload["top_codes"]                        # 记了 top 明细


def test_daily_step_no_marker_below_threshold(iso, tmp_path):
    """回填量低于阈值(补齐网正常)⇒ 不落告警,避免噪声。"""
    code = "600200"
    _put_with_trailing_gap(code, n_gap=1)
    marker = tmp_path / "alarm2" / "_TURNOVER_BACKFILL_ALARM.json"
    bt.main(["--apply", "--codes", code,
             "--alert-marker", str(marker), "--alert-threshold", "1000"])
    assert not marker.exists()


def test_daily_step_dry_run_does_not_write(iso):
    """缺省 dry-run:检出但不写盘,尾行仍 NaN(--apply 才真写,防误触生产)。"""
    code = "600005"
    _put_with_trailing_gap(code, n_gap=1)
    rc = bt.main(["--codes", code])                    # 无 --apply
    assert rc == 1
    got = store.get_master_kline(code)
    assert pd.isna(got.iloc[-1]["turnover"])           # dry-run 未写
