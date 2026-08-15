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


@skip_no_data
def test_error_prediction_pages_do_not_crash():
    """回归:次新股 K线不足时 predict() 返回 {"error":...}(真值但无 买卖倾向/持有期建议 键)。

    dashboard.html / stock.html 必须防空(not prediction.error),否则 jinja UndefinedError 炸整页。
    锁死语义:凡 error-prediction 记录,首页 + 个股页均须 200。
    """
    err_codes = [r["meta"]["code"] for r in _RECS
                 if isinstance(r.get("prediction"), dict) and r["prediction"].get("error")]
    if not err_codes:
        pytest.skip("当前缓存无 error-prediction(次新股)记录")
    assert client.get("/").status_code == 200          # 首页含全部记录
    for code in err_codes:
        assert client.get(f"/stock/{code}").status_code == 200, f"error-prediction 个股页炸了: {code}"


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


def test_json_safe_sanitizes_nonfinite():
    """json_safe 递归把 NaN/Inf/-Inf → None,其余原样(锁死 JSON 净化语义)。"""
    src = {"a": float("nan"), "b": [1.0, float("inf"), {"c": float("-inf")}], "d": 3, "e": "x"}
    assert da.json_safe(src) == {"a": None, "b": [1.0, None, {"c": None}], "d": 3, "e": "x"}


def test_api_stock_json_survives_nan(monkeypatch):
    """回归:pandas 落盘的 NaN(如 kline.volume)不得让 /api/stock 500。

    离线管线 json.dumps(allow_nan) 会把 NaN 写进 data/analysis,读回后严格 JSON 编码器
    会抛 ValueError→500。锁死:接口在返回边界净化 NaN→null,恒 200。
    """
    monkeypatch.setattr(da, "get_record", lambda code, date="latest": {"meta": {"code": code}})
    monkeypatch.setattr(da, "get_kline",
                        lambda code, date="latest": {"volume": [1.0, float("nan"), float("inf")]})
    r = client.get("/api/stock/999999")
    assert r.status_code == 200
    assert r.json()["kline"]["volume"] == [1.0, None, None]


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
