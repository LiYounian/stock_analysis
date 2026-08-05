"""Web 路由冒烟测试(FastAPI TestClient,读真实 data/analysis 缓存)。

若无缓存数据(data/analysis 为空),用例自动跳过(不误报)。
"""
import glob

import pytest
from fastapi.testclient import TestClient

from tools.config import settings
from web.app import app

client = TestClient(app)

_HAS_DATA = len([f for f in glob.glob(str(settings.PROJECT_ROOT / "data/analysis/*.json"))
                 if not f.endswith(("panel.json", "screen.json"))]) > 0
skip_no_data = pytest.mark.skipif(not _HAS_DATA, reason="无 data/analysis 缓存,先 run.py")


@skip_no_data
def test_dashboard_ok():
    r = client.get("/")
    assert r.status_code == 200
    assert "今日概览" in r.text
    assert "非投资建议" in r.text          # 免责必现


@skip_no_data
def test_stock_page_ok():
    code = [f.rsplit("/", 1)[-1][:-5]
            for f in glob.glob(str(settings.PROJECT_ROOT / "data/analysis/*.json"))
            if not f.endswith(("panel.json", "screen.json"))][0]
    r = client.get(f"/stock/{code}")
    assert r.status_code == 200
    assert "K线走势" in r.text
    assert "止盈止损" in r.text


def test_stock_404():
    r = client.get("/stock/000000")
    assert r.status_code == 404


@skip_no_data
def test_screen_ok():
    r = client.get("/screen")
    assert r.status_code == 200
    assert "选股筛选" in r.text


@skip_no_data
def test_news_ok():
    r = client.get("/news")
    assert r.status_code == 200
    assert "每日信息流" in r.text


@skip_no_data
def test_api_stock_json():
    code = [f.rsplit("/", 1)[-1][:-5]
            for f in glob.glob(str(settings.PROJECT_ROOT / "data/analysis/*.json"))
            if not f.endswith(("panel.json", "screen.json"))][0]
    r = client.get(f"/api/stock/{code}")
    assert r.status_code == 200
    body = r.json()
    assert "record" in body and "kline" in body
