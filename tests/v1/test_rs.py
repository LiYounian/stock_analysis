"""相对强度 RS 单测(V1 F2.2):20 日收益率差,与手算一致 + 达标阈值可配。"""
import pandas as pd
import pytest

from tools.analysis.v1 import rs


def test_rs_equals_return_diff():
    # 标的 20 日 +10%,基准 20 日 +4% → RS = 6 个百分点
    target = [100.0] + [0] * 19 + [110.0]
    target = [100.0 + i * 0 for i in range(20)] + [110.0]   # 21 根,首=100 末=110
    bench = [100.0] * 20 + [104.0]
    assert rs.compute(target, bench, win=20) == pytest.approx(6.0, abs=1e-6)


def test_rs_accepts_dataframe():
    tdf = pd.DataFrame({"close": [100.0] * 20 + [112.0]})
    bdf = pd.DataFrame({"close": [100.0] * 20 + [105.0]})
    assert rs.compute(tdf, bdf, win=20) == pytest.approx(7.0, abs=1e-6)


def test_rs_insufficient_raises():
    with pytest.raises(ValueError):
        rs.compute([100, 101, 102], [100, 100, 100], win=20)


def test_is_strong_threshold():
    assert rs.is_strong(6.0, "个股vs板块") is True      # 默认阈值 0.0
    assert rs.is_strong(-1.0, "个股vs板块") is False


def test_rank_mode_not_implemented():
    with pytest.raises(NotImplementedError):
        rs.rank_rs()
