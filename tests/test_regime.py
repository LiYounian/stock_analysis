"""市场状态 Market Regime(V1 模块一)单测。

锁语义:五因子平权(情绪分=有效因子子分均值×100)、宽度=达标占比、缺因子降级不崩、
五档边界读 Config(改边界分档随之变)、指数多头 MA 排列判定。
"""
import pandas as pd
import pytest

from tools.analysis.pattern_screener import regime as rg
from tools.collectors import index, market
from tools.pipeline import regime as pl
from tools.store import repo as store


def _idx(closes, vols=None):
    d = {"date": pd.date_range("2024-01-01", periods=len(closes), freq="D"), "close": closes}
    if vols is not None:
        d["volume"] = vols
    return pd.DataFrame(d)


# ---------- 指数多头(MA 排列)----------
def test_factor_指数多头():
    up = _idx([float(i) for i in range(1, 25)])            # 递增→多头排列
    assert rg.factor_指数多头(up)[0] == 1.0
    down = _idx([float(i) for i in range(24, 0, -1)])      # 递减→空头
    assert rg.factor_指数多头(down)[0] == 0.0
    assert rg.factor_指数多头(_idx([1.0, 2.0]))[0] is None  # 样本不足


# ---------- 量能 / 宽度 / 科技共振 / 涨跌停 ----------
def test_factor_量能():
    df = _idx([10.0] * 10, vols=[100.0] * 9 + [180.0])     # 末根放量、接近天量
    sub, why = rg.factor_量能(df)
    assert sub is not None and 0 <= sub <= 1
    assert rg.factor_量能(_idx([10.0, 11.0]))[0] is None    # 无量字段→None


def test_factor_宽度_uses_达标占比():
    sub, _ = rg.factor_宽度(0.05)                           # =参考满档→1.0
    assert sub == pytest.approx(1.0)
    assert rg.factor_宽度(None)[0] is None                  # 缺 view→None


def test_factor_科技共振_pool_empty_degrades():
    assert rg.factor_科技共振(None)[0] is None              # 龙头池空→降级
    assert rg.factor_科技共振([1.0, -1.0, 2.0])[0] == pytest.approx(2 / 3, abs=1e-3)


def test_factor_涨跌停():
    assert rg.factor_涨跌停(None)[0] is None                # 无家数→降级
    assert rg.factor_涨跌停({"涨停": 30, "跌停": 10})[0] > 0.5   # 涨多→偏高


# ---------- 五档标签读 Config(改边界分档随之变)----------
def test_label_reads_config():
    assert rg.label_of(10) == "冰点" and rg.label_of(50) == "震荡" and rg.label_of(90) == "牛市共振"
    cfg = {"五档": [["低", 50], ["高", 100]]}                # 自定边界
    assert rg.label_of(40, cfg) == "低" and rg.label_of(60, cfg) == "高"


# ---------- analyze:平权 + 降级 ----------
def test_analyze_equal_weight_and_degrade():
    up = _idx([float(i) for i in range(1, 25)], vols=[100.0] * 23 + [150.0])
    r = rg.analyze(index_df=up, 达标占比=0.05, leader_pcts=None, 涨跌停=None)
    # 有效因子:指数多头(1.0)+量能+宽度(1.0);科技共振/涨跌停降级
    assert r["有效因子数"] == 3 and r["因子贡献"]["科技共振"]["可用"] is False
    subs = [r["因子贡献"][n]["子分"] for n in ("指数多头", "量能", "宽度")]
    assert r["情绪分"] == pytest.approx(round(sum(subs) / 3 * 100, 2))   # 平权均值×100
    assert any("科技共振" in d for d in r["降级"]) and any("待策略端标定" in d for d in r["降级"])
    assert r["标签"] == rg.label_of(r["情绪分"])


def test_analyze_all_missing_neutral():
    r = rg.analyze(index_df=None, 达标占比=None, leader_pcts=None, 涨跌停=None)
    assert r["有效因子数"] == 0 and r["情绪分"] == 0.0 and r["标签"] == "冰点"


# ---------- 编排 run_regime 端到端(mock 采集/ store)----------
def test_run_regime_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path)
    up = _idx([float(i) for i in range(1, 25)], vols=[100.0] * 23 + [150.0])
    monkeypatch.setattr(index, "load_index", lambda code: up)
    real_get = store.get_view                       # 只替宽度那次读,市场状态读回走真实 tmp
    monkeypatch.setattr(store, "get_view",
                        lambda name, *a, **k: {"达标占比": 0.05} if name == "形态选股"
                        else real_get(name, *a, **k))
    r = pl.run_regime(as_of="2024-06-01", fetch=False)
    assert r["情绪分"] > 0 and r["标签"] in [x[0] for x in rg._CFG["五档"]]
    assert store.get_view("市场状态", date="2024-06-01")["标签"] == r["标签"]
