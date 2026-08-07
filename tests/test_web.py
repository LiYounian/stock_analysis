"""Web 路由冒烟测试(FastAPI TestClient,读真实 data/analysis 缓存)。

若无缓存数据(data/analysis 为空),用例自动跳过(不误报)。
"""
import pytest
from fastapi.testclient import TestClient

from web import data_access as da
from web.app import app

client = TestClient(app)

_RECS = da.list_records()                 # 最新日期下的个股记录(经 store)
_HAS_DATA = len(_RECS) > 0
_A_CODE = _RECS[0]["meta"]["code"] if _RECS else "000000"
skip_no_data = pytest.mark.skipif(not _HAS_DATA, reason="无 data/analysis 缓存,先 run.py")


@skip_no_data
def test_dashboard_ok():
    r = client.get("/")
    assert r.status_code == 200
    assert "今日概览" in r.text
    assert "非投资建议" in r.text          # 免责必现


@skip_no_data
def test_stock_page_ok():
    r = client.get(f"/stock/{_A_CODE}")
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
    r = client.get(f"/api/stock/{_A_CODE}")
    assert r.status_code == 200
    body = r.json()
    assert "record" in body and "kline" in body


# —— 历史/日期(点2)——
@skip_no_data
def test_available_dates_and_date_param():
    """有数据时 available_dates 非空;各页接受 ?date= 且回退不 500。"""
    dates = da.available_dates()
    assert dates and all(len(d) == 10 for d in dates)     # YYYY-MM-DD
    r = client.get(f"/?date={dates[0]}")
    assert r.status_code == 200
    assert 'class="date-select"' in r.text                 # 顶栏日期下拉已渲染
    # 非法日期回退最新,不报错
    assert client.get("/?date=1999-01-01").status_code == 200


# —— 新闻详情(点3)——
def _code_with_news():
    for r in _RECS:
        c = r["meta"]["code"]
        if da.news_list(c):
            return c
    return None


@skip_no_data
def test_news_list_and_detail():
    code = _code_with_news()
    if not code:
        pytest.skip("dev 样本当日无原始新闻")
    lst = client.get(f"/news/{code}")
    assert lst.status_code == 200 and "当日新闻" in lst.text
    detail = client.get(f"/news/{code}/0")
    assert detail.status_code == 200
    # 详情含来源;正文/链接任一存在(取决于该条数据)
    item = da.news_detail(code, 0)
    if item.get("source"):
        assert item["source"] in detail.text
    # 越界 → 404
    assert client.get(f"/news/{code}/99999").status_code == 404


def test_news_detail_missing_code_404():
    assert client.get("/news/000000/0").status_code == 404
