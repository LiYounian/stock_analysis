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
                      "ma5", "ma20", "ma60", "volume"}   # 含 OHLC 支持蜡烛图
    assert len(d["dates"]) == len(d["open"]) == len(d["close"]) == 120
    assert len(d["dates"]) == 120                 # 截到 limit
    assert d["ma20"][-1] is not None              # MA 已预算
    assert d["ma60"][0] is None or isinstance(d["ma60"][0], float)


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
