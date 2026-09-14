"""市场状态 Market Regime(V1 模块一;2026-09-14 P0/P1 改造后)单测。

锁语义(改造后):**四因子**平权(指数多头/量能/宽度/涨跌停,情绪分=有效因子子分均值×100)、
宽度=above_ma20_ratio 直用(取消 ref)、缺因子降级不崩、五档边界读 Config 且**冰点/熊市档可触发**
(旧等分占位下永不触发的回归护栏)、指数多头 MA 排列判定。
"""
import pandas as pd
import pytest

from tools.analysis.pattern_screener import regime as rg
from tools.collectors import index
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


def test_factor_宽度_uses_above_ma20():
    # 改造后:above_ma20_ratio 直用(取消 ref/0.05 归一),clamp 到 [0,1]
    assert rg.factor_宽度(0.05)[0] == pytest.approx(0.05)
    assert rg.factor_宽度(0.42)[0] == pytest.approx(0.42)
    assert rg.factor_宽度(1.5)[0] == pytest.approx(1.0)     # 越界→clamp
    assert rg.factor_宽度(None)[0] is None                  # 缺→None


def test_科技共振_已移除():
    # P0 改造:移除科技共振(龙头池长期空、恒降级)→ 降为 4 因子
    assert "科技共振" not in rg._CFG["因子"]
    assert not hasattr(rg, "factor_科技共振")
    assert len(rg._CFG["因子"]) == 4


def test_factor_涨跌停():
    assert rg.factor_涨跌停(None)[0] is None                # 无家数→降级
    assert rg.factor_涨跌停({"涨停": 30, "跌停": 10})[0] > 0.5   # 涨多→偏高


# ---------- 五档标签读 Config(改边界分档随之变)----------
def test_label_reads_config():
    # 改造后语义锚边界 25/45/60/75
    assert rg.label_of(10) == "冰点" and rg.label_of(50) == "震荡" and rg.label_of(90) == "牛市共振"
    cfg = {"五档": [["低", 50], ["高", 100]]}                # 自定边界
    assert rg.label_of(40, cfg) == "低" and rg.label_of(60, cfg) == "高"


def test_五档全可触发_回归护栏():
    # 诊断动因:旧等分边界(20/40/60/80)下情绪分下不去 40→冰点/熊市共振永不触发。
    # 锁住新标定使五档都可达(防未来重写无声改坏标定)。
    got = {rg.label_of(s) for s in (20, 35, 55, 70, 85)}
    assert got == {"冰点", "熊市共振", "震荡", "分化", "牛市共振"}


# ---------- analyze:平权 + 降级 ----------
def test_analyze_equal_weight_and_degrade():
    up = _idx([float(i) for i in range(1, 25)], vols=[100.0] * 23 + [150.0])
    # 四因子:指数多头(1.0)+量能+宽度;涨跌停缺→降级。宽度=above_ma20_ratio 直用。
    r = rg.analyze(index_df=up, 宽度占比=0.42, 涨跌停=None)
    assert r["总因子数"] == 4
    assert r["有效因子数"] == 3 and r["因子贡献"]["涨跌停"]["可用"] is False
    assert r["因子贡献"]["宽度"]["子分"] == pytest.approx(0.42)
    subs = [r["因子贡献"][n]["子分"] for n in ("指数多头", "量能", "宽度")]
    assert r["情绪分"] == pytest.approx(round(sum(subs) / 3 * 100, 2))   # 平权均值×100
    assert r["标签"] == rg.label_of(r["情绪分"])


def test_analyze_涨跌停接线_不再恒降级():
    up = _idx([float(i) for i in range(1, 25)], vols=[100.0] * 23 + [150.0])
    r = rg.analyze(index_df=up, 宽度占比=0.42, 涨跌停={"涨停": 30, "跌停": 10})
    assert r["因子贡献"]["涨跌停"]["可用"] is True and r["有效因子数"] == 4


def test_analyze_all_missing_neutral():
    r = rg.analyze(index_df=None, 宽度占比=None, 涨跌停=None)
    assert r["有效因子数"] == 0 and r["情绪分"] == 0.0 and r["标签"] == "冰点"


# ---------- 编排 run_regime 端到端(mock 采集/广度/ store)----------
def test_run_regime_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path)
    up = _idx([float(i) for i in range(1, 25)], vols=[100.0] * 23 + [150.0])
    monkeypatch.setattr(index, "load_index", lambda code: up)
    # mock 广度:最近交易日 above_ma20_ratio + 涨跌停家数
    from tools.analysis.market_forecast import breadth as mfb
    bd = pd.DataFrame(
        {"above_ma20_ratio": [0.4, 0.45], "limit_up": [30.0, 28.0], "limit_down": [10.0, 12.0]},
        index=pd.to_datetime(["2024-05-31", "2024-06-01"]))
    monkeypatch.setattr(mfb, "compute_breadth", lambda *a, **k: bd)
    r = pl.run_regime(as_of="2024-06-01", fetch=False)
    assert r["情绪分"] > 0 and r["标签"] in [x[0] for x in rg._CFG["五档"]]
    assert r["因子贡献"]["宽度"]["子分"] == pytest.approx(0.45)   # 取≤as_of 最近行
    assert r["因子贡献"]["涨跌停"]["可用"] is True
    assert store.get_view("市场状态", date="2024-06-01")["标签"] == r["标签"]
