"""策略 5「半导体多因子」全A screener 单测(hermetic,不触网)。

锁死红线:
  · 限半导体池 178 只(config/semi_universe.json);codes ∩ 池 才进因子
  · 3 因子 + winsor+zscore 加权(rd/rev × 0.6 + rd/mcap × 0.2 + 营收增速 × 0.2)
  · 缺 fundamental.总市值 / financial.derived.研发费用率 / 营收增速 → 剔
  · view schema 完整(扫描数/universe_size/有效样本/入选清单.明细/权重/规则)
  · fetch=False 时不触网(load 失败即跳过)
"""
from __future__ import annotations

import pandas as pd
import pytest

from tools.pipeline import screen_semi_factor as sf


@pytest.fixture
def patch_universe(monkeypatch):
    """假半导体池:A/B/C 三只(D/E 池外)。同时 patch pipeline 的池加载 + strategy 内部的池加载。"""
    from tools.strategy import semi_factor as sf_strategy
    monkeypatch.setattr(sf, "_load_semi_universe", lambda: {"A", "B", "C"})
    monkeypatch.setattr(sf_strategy, "_load_universe", lambda: {"A", "B", "C"})


@pytest.fixture
def loose_strategy_universe(monkeypatch):
    """把 strategy 内部限池置空(单独覆盖 pipeline 限池,不双限)。"""
    from tools.strategy import semi_factor as sf_strategy
    monkeypatch.setattr(sf_strategy, "_load_universe", lambda: set())


def _fund(mktcap_yi):
    return {"总市值": mktcap_yi, "PE_TTM": 30.0, "PB": 3.0}


def _fin(rd_pct, rev_yoy_pct, 营收):
    return {
        "报告期": "20260331",
        "derived": {"研发费用率": rd_pct, "营收增速": rev_yoy_pct},
        "利润表摘要": {"营业总收入": 营收},
    }


def _kline(pct_chg=0.5, n=200):
    dates = pd.bdate_range("2024-01-01", periods=n)
    closes = [10.0] * n
    df = pd.DataFrame({"date": dates, "open": closes, "high": closes,
                       "low": closes, "close": closes, "volume": [1e6] * n,
                       "pct_chg": [0.0] * (n - 1) + [pct_chg]})
    return df.set_index("date")


@pytest.fixture
def patch_data(monkeypatch):
    """按 code 分派 fundamental / financial / kline。"""
    funds = {
        "A": _fund(569.0),
        "B": _fund(10113.29),
        "C": _fund(5571.0),
        "D": _fund(500.0),      # 池外
    }
    fins = {
        "A": _fin(15.0, 45.0, 5e9),      # rd/rev 高 → 应排头
        "B": _fin(3.3, 190.0, 1.9e10),
        "C": _fin(1.4, 105.0, 1e10),     # rd/rev 低 → 应垫底
        "D": _fin(30.0, 200.0, 1e10),    # 池外(即使因子完美也不进)
    }
    monkeypatch.setattr(sf.fd, "load_fundamental",
                        lambda code: funds.get(code) or (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(sf.fr_analyzer, "build_financial_block",
                        lambda code, as_of=None: fins.get(code))
    monkeypatch.setattr(sf.market, "load_kline_recent", lambda code: _kline())


def test_universe_filter(patch_universe, patch_data, tmp_path, monkeypatch):
    """池外票被剔:D 因子完美但不在池,不入选。"""
    monkeypatch.setattr("tools.store.repo.put_view", lambda name, view, **_: str(tmp_path / f"{name}.json"))
    v = sf.run_semi_factor_screen(["A", "B", "C", "D"], as_of="2026-08-19", fetch=False, top_k=5)
    codes = [x["code"] for x in v["入选清单"]]
    assert set(codes) == {"A", "B", "C"}                     # D 池外剔
    assert v["universe_size"] == 3
    assert v["扫描数"] == 3                                    # 全A ∩ 池


def test_factor_weight_favors_rd_rev(patch_universe, patch_data, tmp_path, monkeypatch):
    """rd/rev 权最大(0.6):高研发的票排头,低研发垫底。"""
    monkeypatch.setattr("tools.store.repo.put_view", lambda name, view, **_: str(tmp_path / f"{name}.json"))
    v = sf.run_semi_factor_screen(["A", "B", "C"], as_of="2026-08-19", fetch=False, top_k=3)
    codes = [x["code"] for x in v["入选清单"]]
    assert codes[0] == "A"                                     # rd/rev 15% 最高
    assert codes[-1] == "C"                                    # rd/rev 1.4% 最低


def test_missing_mktcap_skipped(patch_universe, tmp_path, monkeypatch):
    """fundamental 缺 → 该票剔;不炸。"""
    def _fund_partial(code):
        if code == "A":
            raise FileNotFoundError()
        return _fund(500.0)
    monkeypatch.setattr(sf.fd, "load_fundamental", _fund_partial)
    monkeypatch.setattr(sf.fr_analyzer, "build_financial_block",
                        lambda c, as_of=None: _fin(5.0, 30.0, 1e10))
    monkeypatch.setattr(sf.market, "load_kline_recent", lambda c: _kline())
    monkeypatch.setattr("tools.store.repo.put_view", lambda name, view, **_: str(tmp_path / f"{name}.json"))
    v = sf.run_semi_factor_screen(["A", "B", "C"], as_of="2026-08-19", fetch=False, top_k=5)
    assert set(x["code"] for x in v["入选清单"]) == {"B", "C"}   # A 剔
    assert v["跳过数(缺数据)"] == 1


def test_missing_financial_derived_skipped(patch_universe, tmp_path, monkeypatch):
    """financial.derived.研发费用率 缺 → 剔。"""
    monkeypatch.setattr(sf.fd, "load_fundamental", lambda c: _fund(500.0))
    def _fin_partial(code, as_of=None):
        if code == "A":
            return None                                        # 完全无 financial 块
        if code == "B":
            return {"derived": {"营收增速": 30.0}, "利润表摘要": {"营业总收入": 1e10}}   # 缺 rd_pct
        return _fin(5.0, 30.0, 1e10)
    monkeypatch.setattr(sf.fr_analyzer, "build_financial_block", _fin_partial)
    monkeypatch.setattr(sf.market, "load_kline_recent", lambda c: _kline())
    monkeypatch.setattr("tools.store.repo.put_view", lambda name, view, **_: str(tmp_path / f"{name}.json"))
    v = sf.run_semi_factor_screen(["A", "B", "C"], as_of="2026-08-19", fetch=False, top_k=5)
    assert set(x["code"] for x in v["入选清单"]) == set()     # C 单只 <2 → 策略E 内部返回空
    assert v["跳过数(缺数据)"] == 2                             # A/B 各剔


def test_view_schema_complete(patch_universe, patch_data, tmp_path, monkeypatch):
    """view schema 关键字段齐全,便于 web 消费。"""
    monkeypatch.setattr("tools.store.repo.put_view", lambda name, view, **_: str(tmp_path / f"{name}.json"))
    v = sf.run_semi_factor_screen(["A", "B", "C"], as_of="2026-08-19", fetch=False, top_k=3)
    for k in ("as_of", "策略", "口径", "扫描数", "universe_size", "有效样本",
              "跳过数(缺数据)", "入选数", "top_k", "权重", "入选清单", "复用", "防未来函数"):
        assert k in v, f"缺字段 {k}"
    item = v["入选清单"][0]
    for k in ("code", "行业", "组合", "明细"):
        assert k in item
    for k in ("综合分", "rd_rev", "rd_mcap", "rev_yoy",
              "rd_rev_z", "rd_mcap_z", "rev_yoy_z"):
        assert k in item["明细"]


def test_universe_missing_falls_back_no_restrict(tmp_path, monkeypatch, loose_strategy_universe):
    """半导体池文件缺失 → 不限池(降级);codes 全过策略E。"""
    monkeypatch.setattr(sf, "_load_semi_universe", lambda: set())
    monkeypatch.setattr(sf.fd, "load_fundamental", lambda c: _fund(500.0))
    monkeypatch.setattr(sf.fr_analyzer, "build_financial_block",
                        lambda c, as_of=None: _fin(5.0, 30.0, 1e10))
    monkeypatch.setattr(sf.market, "load_kline_recent", lambda c: _kline())
    monkeypatch.setattr("tools.store.repo.put_view", lambda name, view, **_: str(tmp_path / f"{name}.json"))
    v = sf.run_semi_factor_screen(["X", "Y"], as_of="2026-08-19", fetch=False, top_k=3)
    assert v["universe_size"] == 0
    assert v["扫描数"] == 2                                     # 全 codes 进(池空=不限)
    assert set(x["code"] for x in v["入选清单"]) == {"X", "Y"}


def test_no_fetch_does_not_touch_network(patch_universe, tmp_path, monkeypatch):
    """fetch=False 时,load 失败即跳过,不调 fetch_fundamental(不触网)。"""
    called = {"fetch": 0}
    monkeypatch.setattr(sf.fd, "load_fundamental",
                        lambda c: (_ for _ in ()).throw(FileNotFoundError()))
    def _guard(codes, as_of=None):
        called["fetch"] += 1
        return {}
    monkeypatch.setattr(sf.fd, "fetch_fundamental", _guard)
    monkeypatch.setattr(sf.fr_analyzer, "build_financial_block",
                        lambda c, as_of=None: None)
    monkeypatch.setattr(sf.market, "load_kline_recent", lambda c: _kline())
    monkeypatch.setattr("tools.store.repo.put_view", lambda name, view, **_: str(tmp_path / f"{name}.json"))
    v = sf.run_semi_factor_screen(["A", "B", "C"], as_of="2026-08-19", fetch=False, top_k=3)
    assert called["fetch"] == 0                                # 一次都不能调
    assert v["入选数"] == 0
