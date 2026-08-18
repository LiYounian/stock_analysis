"""策略C/D 小市值组合单测(移植自聚宽「价值选股与RSRS择时」)。"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from tools.strategy import registry as reg
from tools.strategy import small_cap as sc


def _rec(mktcap_yi, pct_chg=0.5):
    """最小可用中心记录 fixture:只填选股用到的 valuation + snapshot 字段。"""
    return {
        "valuation": {"mktcap_yi": mktcap_yi},
        "snapshot": {"close": 10.0, "pct_chg": pct_chg},
    }


@pytest.fixture
def loose_universe(monkeypatch):
    """策略C 用:把票池置空(→ 不限池),便于覆盖其余过滤。"""
    monkeypatch.setattr(sc, "_load_universe", lambda: set())


@pytest.fixture
def enough_kline(monkeypatch):
    """kline_loader 默认返回 500 根 close(满足 ≥120,非次新)。"""
    df = pd.DataFrame({"close": [10.0] * 500})
    monkeypatch.setattr(sc, "_default_kline_loader", lambda code: df)


def test_registered():
    for name in ("策略C_小市值组合", "策略D_自选池小市值组合"):
        meta = reg.get(name)
        assert meta.kind == "选股"
        assert callable(meta.fn)


# —— 策略C ————————————————————————————————————————————————
def test_c_pick_smallest_market_cap(loose_universe, enough_kline):
    records = {f"002{i:03d}": _rec(float(v))
               for i, v in enumerate([50, 30, 10, 40, 20], 1)}
    out = reg.run("策略C_小市值组合", records, top_k=3, as_of=date(2026, 2, 15))
    assert out["codes"] == ["002003", "002005", "002002"]
    assert out["embargo"] is False


def test_c_exclude_by_code_head(loose_universe, enough_kline):
    records = {
        "300001": _rec(1.0), "688001": _rec(2.0), "830001": _rec(3.0),
        "400001": _rec(4.0), "900001": _rec(5.0),
        "002001": _rec(50.0), "002002": _rec(40.0),
    }
    out = reg.run("策略C_小市值组合", records, top_k=5, as_of=date(2026, 2, 15))
    assert set(out["codes"]) == {"002001", "002002"}


def test_c_exclude_limit_up_down(loose_universe, enough_kline):
    records = {
        "002001": _rec(10.0, pct_chg=9.9),      # 涨停
        "002002": _rec(20.0, pct_chg=-9.8),     # 跌停
        "002003": _rec(30.0, pct_chg=3.5),
        "002004": _rec(40.0, pct_chg=0.0),
    }
    out = reg.run("策略C_小市值组合", records, top_k=5, as_of=date(2026, 2, 15))
    assert set(out["codes"]) == {"002003", "002004"}


def test_c_exclude_missing_snapshot(loose_universe, enough_kline):
    records = {
        "002001": {"valuation": {"mktcap_yi": 10.0}, "snapshot": None},
        "002002": {"valuation": {"mktcap_yi": 20.0}},   # 无 snapshot 字段
        "002003": _rec(30.0),
    }
    out = reg.run("策略C_小市值组合", records, top_k=5, as_of=date(2026, 2, 15))
    assert out["codes"] == ["002003"]


def test_c_exclude_new_listings(loose_universe, monkeypatch):
    monkeypatch.setattr(sc, "_default_kline_loader",
                        lambda c: pd.DataFrame({"close": [10.0] * (50 if c == "002001" else 200)}))
    records = {"002001": _rec(10.0), "002002": _rec(20.0)}
    out = reg.run("策略C_小市值组合", records, top_k=5, as_of=date(2026, 2, 15))
    assert out["codes"] == ["002002"]


def test_c_exclude_missing_kline(loose_universe, monkeypatch):
    monkeypatch.setattr(sc, "_default_kline_loader", lambda c: None)
    out = reg.run("策略C_小市值组合", {"002001": _rec(10.0)}, top_k=5,
                  as_of=date(2026, 2, 15))
    assert out["codes"] == []


def test_c_exclude_missing_mktcap(loose_universe, enough_kline):
    records = {
        "002001": {"valuation": {}, "snapshot": {"pct_chg": 0}},
        "002002": {"valuation": {"mktcap_yi": None}, "snapshot": {"pct_chg": 0}},
        "002003": {"valuation": {"mktcap_yi": 0}, "snapshot": {"pct_chg": 0}},
        "002004": _rec(10.0),
    }
    out = reg.run("策略C_小市值组合", records, top_k=5, as_of=date(2026, 2, 15))
    assert out["codes"] == ["002004"]


def test_c_universe_intersection(monkeypatch, enough_kline):
    monkeypatch.setattr(sc, "_load_universe", lambda: {"002001", "002002"})
    records = {"002001": _rec(30.0), "002002": _rec(20.0),
               "002999": _rec(1.0), "600001": _rec(0.5)}   # 后两只不在票池 → 剔
    out = reg.run("策略C_小市值组合", records, top_k=5, as_of=date(2026, 2, 15))
    assert set(out["codes"]) == {"002001", "002002"}


def test_c_pool_shrinking_layers(loose_universe, enough_kline):
    records = {f"002{i:03d}": _rec(float(i)) for i in range(1, 30)}
    out = reg.run("策略C_小市值组合", records, top_k=1, as_of=date(2026, 2, 15))
    assert out["codes"] == ["002001"]                       # 最小市值
    assert len(out["candidates"]) == 3                       # top_k×3
    assert out["monthly_pool_size"] == 20                    # top_k×20


def test_c_empty_records(loose_universe, enough_kline):
    for rec in ({}, None):
        out = reg.run("策略C_小市值组合", rec, top_k=5, as_of=date(2026, 2, 15))
        assert out["codes"] == [] and out["candidates"] == []


# —— embargo(C/D 共享)————————————————————————————————————————————————
@pytest.mark.parametrize("d,expected", [
    (date(2026, 1, 15), True), (date(2026, 1, 28), True), (date(2026, 1, 29), False),
    (date(2026, 2, 10), False), (date(2026, 3, 19), False),
    (date(2026, 3, 20), True), (date(2026, 4, 28), True), (date(2026, 4, 29), False),
    (date(2026, 12, 21), False), (date(2026, 12, 22), True),
])
def test_embargo_month(d, expected):
    assert sc.is_embargo_month(d) is expected


def test_embargo_does_not_block_output(loose_universe, enough_kline):
    """空仓月仍返回选股结果,由展示层决定用不用(分析层不代买 ETF)。"""
    out = reg.run("策略C_小市值组合", {"002001": _rec(10.0)}, top_k=5,
                  as_of=date(2026, 1, 15))
    assert out["embargo"] is True
    assert out["codes"] == ["002001"]


# —— 策略D:自选池版(不限池 + 不剥板块头 + 关次新过滤)——————————————————
def test_d_no_universe_no_board_head_strip(monkeypatch, enough_kline):
    """D 不读 universe、不剥 30/68 —— 创业/科创/沪深主板都保留,按市值升序。"""
    monkeypatch.setattr(sc, "_load_universe", lambda: {"999999"})   # 就算票池排除也不影响 D
    records = {
        "300308": _rec(500.0),      # 创业板
        "688001": _rec(400.0),      # 科创
        "002222": _rec(300.0),      # 深主板
        "601838": _rec(1500.0),     # 沪主板(最大市值,不进 top_k=2)
    }
    out = reg.run("策略D_自选池小市值组合", records, top_k=2, as_of=date(2026, 2, 15))
    assert set(out["codes"]) == {"002222", "688001"}


def test_d_still_filters_limit_and_pause(enough_kline):
    """涨跌停/停牌 D 沿用 C 规则。"""
    records = {
        "300308": _rec(50.0, pct_chg=9.9),      # 涨停 → 剔
        "300502": _rec(60.0, pct_chg=-9.8),     # 跌停 → 剔
        "300418": {"valuation": {"mktcap_yi": 80.0}, "snapshot": None},  # 停牌 → 剔
        "002222": _rec(100.0),                   # 通过
        "000938": _rec(150.0),                   # 通过
    }
    out = reg.run("策略D_自选池小市值组合", records, top_k=5, as_of=date(2026, 2, 15))
    assert out["codes"] == ["002222", "000938"]


def test_d_disables_new_listing_filter(monkeypatch):
    """D 默认关次新过滤:loader 全空/kline 短时也照样入选。

    与 C 的关键差异——本机没落 raw/kline 时,C 会全剔、D 仍能出票。
    """
    monkeypatch.setattr(sc, "_default_kline_loader", lambda c: None)
    records = {"300308": _rec(50.0), "002222": _rec(30.0)}
    out = reg.run("策略D_自选池小市值组合", records, top_k=2, as_of=date(2026, 2, 15))
    assert set(out["codes"]) == {"002222", "300308"}
