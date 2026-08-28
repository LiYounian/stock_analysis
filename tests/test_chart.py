"""chart.py 单测 + web 只读视图 + serialize 契约合规回归。"""
import glob
import json

import pandas as pd
import pytest

from tools.analysis import chart
from tools.config import settings


def _fake_kline(n=130):
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="D"),
        "open": range(n), "high": range(n), "low": range(n),
        "close": [float(10 + i * 0.1) for i in range(n)],
        "volume": [1000.0] * n, "amount": [1.0] * n,
        "turnover": [0.05] * n, "pct_chg": [0.0] * n,
    })


def test_build_chart_shape(monkeypatch):
    monkeypatch.setattr(chart.market, "load_kline", lambda code: _fake_kline())
    d = chart.build_chart("000021", limit=120)
    assert set(d) == {"dates", "open", "high", "low", "close",
                      "ma5", "ma20", "ma60", "volume",
                      "boll_up", "boll_mid", "boll_low",      # 布林带叠加主图
                      "dif", "dea", "macd_hist",              # MACD 子图
                      "rsi6", "rsi12", "rsi24",               # RSI 子图
                      "kdj_k", "kdj_d", "kdj_j"}              # KDJ 子图
    assert all(len(v) == 120 for v in d.values())  # 所有序列等长且截到 limit
    assert d["ma20"][-1] is not None              # MA 已预算
    assert d["ma60"][0] is None or isinstance(d["ma60"][0], float)
    # 新增指标末值为 None 或 float(不报错、类型合规)
    for k in ("boll_up", "boll_mid", "boll_low", "dif", "dea", "macd_hist",
              "rsi6", "rsi12", "rsi24", "kdj_k", "kdj_d", "kdj_j"):
        assert d[k][-1] is None or isinstance(d[k][-1], float)


def test_build_chart_indicator_values(monkeypatch):
    """展示层数字必须 == 分析层 technical 同源函数末值(锁口径、防未来漂移)。"""
    from tools.analysis import technical as ta
    fake = _fake_kline()
    monkeypatch.setattr(chart.market, "load_kline", lambda code: fake)
    d = chart.build_chart("000021", limit=120)
    bl, md, kd = ta.boll(fake["close"]), ta.macd(fake["close"]), ta.kdj(fake)
    assert d["boll_up"][-1] == round(float(bl["upper"].iloc[-1]), 2)
    assert d["boll_low"][-1] == round(float(bl["lower"].iloc[-1]), 2)
    assert d["dif"][-1] == round(float(md["dif"].iloc[-1]), 3)
    assert d["macd_hist"][-1] == round(float(md["macd"].iloc[-1]), 3)
    assert d["rsi12"][-1] == round(float(ta.rsi(fake["close"], 12).iloc[-1]), 2)
    assert d["kdj_k"][-1] == round(float(kd["k"].iloc[-1]), 2)


def test_build_chart_missing(monkeypatch):
    def boom(code):
        raise FileNotFoundError
    monkeypatch.setattr(chart.market, "load_kline", boom)
    assert chart.build_chart("999999")["dates"] == []


def test_web_get_kline_reads_view_not_compute():
    """展示层 get_kline 只读视图,源码不 import 分析器(§9.3 依赖方向)。"""
    from web import data_access as da
    import inspect
    src = inspect.getsource(da)
    assert "import technical" not in src and "analysis import" not in src


_RECS = [f for f in glob.glob(str(settings.PROJECT_ROOT / "data/analysis/*.json"))
         if not f.endswith(("panel.json", "screen.json"))]


@pytest.mark.skipif(not _RECS, reason="无 data/analysis 记录")
def test_serialized_records_pass_contract():
    """已落盘的中心记录应全部过 contracts 校验(§9.2)。"""
    from tools.contracts import record as contracts
    bad = []
    for f in _RECS:
        rec = json.loads(open(f, encoding="utf-8").read())
        errs = contracts.validate_record(rec)
        if errs:
            bad.append((f.rsplit("/", 1)[-1], errs[:2]))
    assert not bad, f"契约不合规记录: {bad[:3]}"
