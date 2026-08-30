"""F5 单测:合议上页面。锁语义:council_summary 抽取、screen 综合分排序、stock 页渲染合议区块。

不依赖磁盘数据:用合成记录 + monkeypatch(data-independent)。
"""
import pytest
from fastapi.testclient import TestClient

from tools.analysis import council
from web import data_access as da
from web.app import app

client = TestClient(app)


def _rec(code, bull=True):
    """造一条含 council 块的中心记录(bull=True 全看多 / False 全看空)。"""
    if bull:
        sig = {"trend": {"评级": "偏多", "得分": 80, "依据": ["多头"]},
               "ob_os": {"verdict": "超卖", "resonance": 3},
               "reversal": {"拐点标签": "反弹启动", "拐点评分": 80}}
        flow = {"今日主力净流入": 1e8, "今日主力净占比": 5.0, "近5日主力合计": 3e8, "主力连续净流入天数": 3}
        sent = {"净情绪分": 0.6, "样本数": 20}
    else:
        sig = {"trend": {"评级": "偏空", "得分": -80, "依据": ["空头"]},
               "ob_os": {"verdict": "超买", "resonance": 3},
               "reversal": {"拐点标签": "无", "拐点评分": 0}}
        flow = {"今日主力净流入": -1e8, "今日主力净占比": -5.0, "近5日主力合计": -3e8, "主力连续净流入天数": 0}
        sent = {"净情绪分": -0.6, "样本数": 20}
    rec = {"schema_version": "1.0",
           "meta": {"code": code, "name": "测试" + code, "sector": "半导体", "industry": "芯片", "as_of": "2026-08-08"},
           "snapshot": None, "valuation": None, "fundamental": None,
           "signals": sig, "prediction": None,
           "sentiment": sent, "fundflow": flow, "events": [],
           "timeseries_refs": {}, "provenance": {}}
    rec["council"] = council.build_council_block(rec)
    return rec


# ———————————— council_summary ————————————
def test_council_summary_extracts_default():
    s = da.council_summary(_rec("000001", bull=True))
    assert s["综合方向"] == "看多" and s["综合分"] > 0 and s["是否冲突"] is False


def test_council_summary_none_for_legacy_record():
    assert da.council_summary({"meta": {"code": "000001"}}) is None
    assert da.council_summary({}) is None


# ———————————— screen 综合分排序(D9)————————————
def test_screen_page_sorts_by_council_score(monkeypatch):
    bull, bear = _rec("000001", True), _rec("000002", False)
    recs = {"000001": bull, "000002": bear}
    monkeypatch.setattr(da.store, "iter_records", lambda date="latest": list(recs.values()))
    monkeypatch.setattr(da.store, "get_view",
                        lambda name, date="latest": {"presets": {"测试组": ["000002", "000001"]}, "aggregate": {}})
    monkeypatch.setattr(da.store, "list_dates", lambda kind: ["2026-08-08"])
    page = da.screen_page()
    rows = page["presets"]["测试组"]
    # 看多(综合分高)应排在看空之前(尽管 preset 里 000002 在前)
    assert rows[0]["code"] == "000001" and rows[0]["council_dir"] == "看多"
    assert rows[1]["code"] == "000002"
    assert rows[0]["council_score"] >= rows[1]["council_score"]


# ———————————— stock 页渲染合议区块 ————————————
@pytest.fixture
def _patch_stock(monkeypatch):
    rec = _rec("000001", bull=True)
    monkeypatch.setattr(da, "get_record", lambda code, date="latest": rec if code == "000001" else None)
    monkeypatch.setattr(da, "get_kline", lambda code, date="latest": {"dates": [], "close": []})
    monkeypatch.setattr(da, "news_list", lambda code, date="latest": [])
    monkeypatch.setattr(da, "available_dates", lambda: ["2026-08-08"])
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    return rec


def test_stock_page_renders_council(_patch_stock):
    r = client.get("/stock/000001")
    assert r.status_code == 200
    assert "专家合议" in r.text                       # 区块标题
    assert 'class="expert-cb"' in r.text              # 勾选框
    assert 'id="councilData"' in r.text               # 前端重合成数据
    assert "/static/council.js" in r.text             # 前端脚本已引入
    assert 'id="councilAttr"' in r.text               # 归因表容器
    # 默认专家组的专家名出现在勾选区(技术趋势已 V2 删除,不应再出现)
    for name in ("拐点", "超买超卖", "情绪三层"):
        assert name in r.text
    assert "技术趋势" not in r.text, "技术趋势 已从默认专家组移除,勾选区不应再渲染"


def test_stock_page_without_council_still_ok(monkeypatch):
    """旧记录(无 council 块)不应报错,只是不渲染合议区块(向后兼容)。"""
    rec = _rec("000009", bull=True)
    rec.pop("council")
    monkeypatch.setattr(da, "get_record", lambda code, date="latest": rec if code == "000009" else None)
    monkeypatch.setattr(da, "get_kline", lambda code, date="latest": {"dates": [], "close": []})
    monkeypatch.setattr(da, "news_list", lambda code, date="latest": [])
    monkeypatch.setattr(da, "available_dates", lambda: ["2026-08-08"])
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    r = client.get("/stock/000009")
    assert r.status_code == 200 and "专家合议" not in r.text
