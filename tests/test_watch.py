"""自选池实时盯盘(方案1)单测:腾讯报价+形态合并、涨跌幅排序、行情源异常降级。

hermetic:mock 报价拉取 + SEPA view,不触网、不读盘。
"""
from web import realtime


def _fake_pool(monkeypatch):
    from tools.config.stock_pool import Stock
    monkeypatch.setattr(realtime.stock_pool, "get_pool", lambda: [
        Stock("300017", "网宿科技", "CDN/边缘计算", "计算机", "A"),
        Stock("300308", "中际旭创", "光模块", "光模块", "A"),
        Stock("000938", "紫光股份", "ICT", "AI算力", "A"),
    ])


def _reset_cache(monkeypatch):
    monkeypatch.setattr(realtime, "_quote_cache", {"ts": 0.0, "by_code": None})


def test_gtimg_prefix():
    assert realtime._gtimg_prefix("300017") == "sz"   # 创业板
    assert realtime._gtimg_prefix("000938") == "sz"   # 深主板
    assert realtime._gtimg_prefix("601838") == "sh"   # 沪主板
    assert realtime._gtimg_prefix("688001") == "sh"   # 科创板
    assert realtime._gtimg_prefix("830799") == "bj"   # 北交所


def test_watch_merges_quote_and_tags_sorted(monkeypatch):
    _fake_pool(monkeypatch)
    _reset_cache(monkeypatch)
    # 紫光缺报价(停牌)→价格留空
    monkeypatch.setattr(realtime, "_fetch_quotes", lambda codes: {
        "300017": {"price": 15.15, "pct_chg": 5.6, "amount_wan": 154428.0},
        "300308": {"price": 800.0, "pct_chg": -1.2, "amount_wan": 990000.0},
    })
    monkeypatch.setattr(realtime, "_sepa_tags_by_code",
                        lambda date="latest": {"300017": ["VCP收缩中(收盘)", "接近枢纽"]})

    out = realtime.watch_quotes("2026-08-25")
    assert out["quote_ok"] is True
    rows = out["rows"]
    assert len(rows) == 3
    # 涨跌幅降序:网宿(+5.6) → 中际旭创(-1.2) → 紫光(无报价沉底)
    assert [r["code"] for r in rows] == ["300017", "300308", "000938"]
    ws = rows[0]
    assert ws["price"] == 15.15 and ws["pct_chg"] == 5.6 and ws["amount_wan"] == 154428.0
    assert ws["sepa_tags"] == ["VCP收缩中(收盘)", "接近枢纽"]  # 合并当日形态(收盘态)
    assert rows[2]["price"] is None and rows[2]["pct_chg"] is None  # 紫光停牌:价格留空


def test_watch_degrades_when_quote_source_fails(monkeypatch):
    """行情源异常:仍出名单+形态,价格留空,quote_ok=False,页面不崩。"""
    _fake_pool(monkeypatch)
    _reset_cache(monkeypatch)

    def boom(codes):
        raise ConnectionError("腾讯 gtimg 拉取失败")
    monkeypatch.setattr(realtime, "_fetch_quotes", boom)
    monkeypatch.setattr(realtime, "_sepa_tags_by_code", lambda date="latest": {})

    out = realtime.watch_quotes("2026-08-25")
    assert out["quote_ok"] is False and "腾讯" in out["quote_err"]
    assert len(out["rows"]) == 3                              # 名单仍在
    assert all(r["price"] is None for r in out["rows"])       # 价格全空,不崩
