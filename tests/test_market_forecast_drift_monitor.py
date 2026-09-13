"""大盘预测·漂移探针(drift_monitor)单测。

锁语义(硬红线,防未来 prompt/代码重写无意删规则):
  · **配对口径正确**:生产 p_up ⋈ as_of 的已实现 fwd_ret;pred_dir/real_dir 映射正确;
    输出记录 schema 与 walk_forward 对齐(可直接喂 BT.score)。
  · **防未来函数**:as_of 的 T+h 尚未到期(fwd_ret 为 NaN)→ 该日不评分(不编造);
    追加"更晚"的预测不得改变更早日的记录(逐日字节级一致);滚动指标严格 trailing
    (改动窗口之后的记录不改变更早窗口的滚动值)。
  · **滚动指标可算**:小样本 fixture 下命中/Brier/IC/校准表均能算出且数值正确。
构造数据、不依赖真实行情(monkeypatch _realized_panel)。
"""
import numpy as np
import pandas as pd
import pytest

from tools.analysis.market_forecast import drift_monitor as DM
from tools.backtest import market_forecast_backtest as BT


# ————————————————————————— fixtures —————————————————————————
def _panel(dates, fwd, mom1=None):
    """构造 _realized_panel 形态的表:index=date,列 fwd_ret/direction/tech_mom1。"""
    idx = pd.to_datetime(dates)
    df = pd.DataFrame(index=idx)
    df.index.name = "date"
    df["fwd_ret"] = fwd
    df["direction"] = np.sign(pd.Series(fwd, index=idx))
    df["tech_mom1"] = mom1 if mom1 is not None else 0.0
    return df


def _prod_df(dates, p_ups, target="proxy", horizon=1):
    return pd.DataFrame({
        "as_of": [str(d) for d in dates],
        "target": target,
        "horizon": horizon,
        "p_up": p_ups,
        "direction": None,
        "src_file": "x",
    })


# ————————————————————————— 配对口径 —————————————————————————
def test_records_pairing_and_schema(monkeypatch):
    dates = ["2026-01-05", "2026-01-06", "2026-01-07"]
    panel = _panel(dates, fwd=[0.02, -0.01, 0.00], mom1=[0.1, -0.2, 0.3])
    monkeypatch.setattr(DM, "_realized_panel", lambda *a, **k: panel)
    prod = _prod_df(dates, p_ups=[0.62, 0.40, 0.51])
    rec = DM.records_from_production("proxy", 1, prod_df=prod)

    # schema 与 walk_forward 对齐 → 可直接喂 BT.score
    assert list(rec.columns) == DM.REC_COLS
    assert len(rec) == 3
    # pred_dir = p_up>=0.5 ? +1 : -1
    assert rec.loc[0, "pred_dir"] == 1 and rec.loc[1, "pred_dir"] == -1
    assert rec.loc[2, "pred_dir"] == 1          # 0.51 >= 0.5
    # real_dir = sign(fwd);平盘=0
    assert rec.loc[0, "real_dir"] == 1
    assert rec.loc[1, "real_dir"] == -1
    assert rec.loc[2, "real_dir"] == 0
    # 复用评分栈不报错
    s = BT.score(rec)
    assert "hit_rate" in s


def test_unrealized_dropped_no_fabrication(monkeypatch):
    """T+h 未到期(fwd_ret=NaN)的 as_of 必须被丢弃,不得编造评分(防未来)。"""
    dates = ["2026-01-05", "2026-01-06", "2026-01-07"]
    panel = _panel(dates, fwd=[0.02, np.nan, np.nan])   # 后两日未到期
    monkeypatch.setattr(DM, "_realized_panel", lambda *a, **k: panel)
    prod = _prod_df(dates, p_ups=[0.6, 0.6, 0.6])
    rec = DM.records_from_production("proxy", 1, prod_df=prod)
    assert len(rec) == 1
    assert str(rec.loc[0, "date"])[:10] == "2026-01-05"


def test_future_prediction_does_not_alter_earlier_records(monkeypatch):
    """追加更晚的预测/收益,更早日的记录必须逐字节不变(无未来函数)。"""
    dates = ["2026-01-05", "2026-01-06"]
    panel_a = _panel(dates, fwd=[0.02, -0.01])
    monkeypatch.setattr(DM, "_realized_panel", lambda *a, **k: panel_a)
    rec_a = DM.records_from_production("proxy", 1, prod_df=_prod_df(dates, [0.6, 0.4]))

    dates2 = dates + ["2026-01-07", "2026-01-08"]
    panel_b = _panel(dates2, fwd=[0.02, -0.01, 0.9, -0.9])   # 未来加极端行情
    monkeypatch.setattr(DM, "_realized_panel", lambda *a, **k: panel_b)
    rec_b = DM.records_from_production("proxy", 1,
                                      prod_df=_prod_df(dates2, [0.6, 0.4, 0.99, 0.01]))

    a = rec_a.set_index("date")
    b = rec_b.set_index("date").loc[a.index]
    for c in DM.REC_COLS[1:]:
        np.testing.assert_array_equal(a[c].to_numpy(), b[c].to_numpy())


# ————————————————————————— 校准 / Brier —————————————————————————
def test_brier_and_climatology():
    rec = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]),
        "p_up": [1.0, 0.0, 0.5, 0.5],
        "pred_dir": [1, -1, 1, 1],
        "real_dir": [1, -1, 1, -1],
        "fwd_ret": [0.01, -0.01, 0.01, -0.01],
        "mom1": 0.0,
    })
    # y=[1,0,1,0];p=[1,0,.5,.5] → (0+0+.25+.25)/4 = .125
    assert DM.brier_score(rec) == pytest.approx(0.125)
    # base_rate=0.5 → clim=0.25
    assert DM.climatology_brier(rec) == pytest.approx(0.25)


def test_calibration_table_up_rate(monkeypatch):
    # 高概率档实际也高上涨率;低概率档低上涨率 → calib_gap 合理
    rec = pd.DataFrame({
        "date": pd.to_datetime([f"2026-01-{i:02d}" for i in range(1, 7)]),
        "p_up": [0.30, 0.32, 0.70, 0.72, 0.68, 0.28],
        "pred_dir": [-1, -1, 1, 1, 1, -1],
        "real_dir": [-1, 1, 1, 1, 1, -1],
        "fwd_ret": [-0.01, 0.01, 0.01, 0.01, 0.01, -0.01],
        "mom1": 0.0,
    })
    tbl = DM.calibration_table(rec)
    by = {r["bucket_idx"]: r for r in tbl}
    # bucket 0 (<=0.35):三条,上涨 1/3
    assert by[0]["n"] == 3
    assert by[0]["emp_up_rate"] == pytest.approx(1 / 3, abs=1e-3)
    # bucket 4 (>0.65):三条,全上涨
    assert by[4]["n"] == 3
    assert by[4]["emp_up_rate"] == 1.0


# ————————————————————————— 滚动指标 —————————————————————————
def _lin_rec(n, p_ups, fwds):
    return pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=n, freq="B"),
        "p_up": p_ups,
        "pred_dir": [1 if p >= 0.5 else -1 for p in p_ups],
        "real_dir": [int(np.sign(f)) if f != 0 else 0 for f in fwds],
        "fwd_ret": fwds,
        "mom1": 0.0,
    })


def test_rolling_metrics_computable_small_sample():
    rng = np.random.default_rng(0)
    n = 40
    fwds = rng.standard_normal(n) * 0.01
    p_ups = np.clip(0.5 + 0.3 * np.sign(fwds) + rng.standard_normal(n) * 0.05, 0.01, 0.99)
    rec = _lin_rec(n, list(p_ups), list(fwds))
    roll = DM.rolling_metrics(rec, window=20, min_periods=10)
    assert len(roll) == n
    # 前 9 行样本不足 → NaN;第 10 行起有值
    assert roll["roll_hit"].iloc[:9].isna().all()
    assert roll["roll_hit"].iloc[9:].notna().all()
    # 命中率 ∈ [0,1]
    assert roll["roll_hit"].dropna().between(0, 1).all()


def test_rolling_is_trailing_only():
    """滚动严格 trailing:改动窗口之后的记录,不得改变更早窗口的滚动值。"""
    n = 30
    fwds = [0.01, -0.01] * (n // 2)
    p_ups = [0.6, 0.4] * (n // 2)
    rec_a = _lin_rec(n, p_ups, fwds)
    roll_a = DM.rolling_metrics(rec_a, window=10, min_periods=5)

    rec_b = rec_a.copy()
    rec_b.loc[20:, "p_up"] = 0.99          # 破坏第 20 行起
    rec_b.loc[20:, "fwd_ret"] = -0.5
    rec_b.loc[20:, "pred_dir"] = 1
    rec_b.loc[20:, "real_dir"] = -1
    roll_b = DM.rolling_metrics(rec_b, window=10, min_periods=5)

    # 第 0..19 行(其 trailing 窗口不含被破坏的 ≥20 行)必须逐值一致
    for col in ("roll_hit", "roll_brier", "roll_ic"):
        a = roll_a[col].iloc[:20].to_numpy()
        b = roll_b[col].iloc[:20].to_numpy()
        np.testing.assert_array_equal(np.nan_to_num(a, nan=-9), np.nan_to_num(b, nan=-9))


def test_trend_brier_direction_semantics():
    """Brier 越小越好:后半段误差下降(delta<0)应判为效力'走强'。"""
    s = pd.Series([0.30, 0.30, 0.20, 0.20])
    t = DM._trend(s, higher_is_better=False)
    assert t["delta"] < 0
    assert t["trend"] == "走强"
    # 命中率越大越好:同样 delta<0 应判'走弱'
    t2 = DM._trend(s, higher_is_better=True)
    assert t2["trend"] == "走弱"


# ————————————————————————— 报警 —————————————————————————
def test_alarms_trigger_on_bad_model():
    """命中<0.5、IC<0、边际<0 → 报警触发且给出原因。"""
    n = 30
    # 预测与实际反着来:pred +1 时实际跌
    fwds = [-0.01, 0.01] * (n // 2)
    p_ups = [0.7, 0.3] * (n // 2)
    rec = _lin_rec(n, p_ups, fwds)
    overall = BT.score(rec)
    roll = DM.rolling_metrics(rec, window=15, min_periods=10)
    al = DM.evaluate_alarms(overall, rec, roll)
    assert al["triggered"] is True
    assert any("命中率" in r for r in al["reasons"])
    assert any("IC" in r for r in al["reasons"])


def test_alarms_quiet_on_decent_model():
    """预测与实际同向、命中高、IC 正 → 不报警。"""
    n = 40
    rng = np.random.default_rng(3)
    fwds = rng.standard_normal(n) * 0.01
    # p_up 与 fwd 强正相关
    p_ups = np.clip(0.5 + 4.0 * fwds, 0.05, 0.95)
    rec = _lin_rec(n, list(p_ups), list(fwds))
    overall = BT.score(rec)
    roll = DM.rolling_metrics(rec, window=20, min_periods=10)
    al = DM.evaluate_alarms(overall, rec, roll)
    assert al["triggered"] is False, al["reasons"]
