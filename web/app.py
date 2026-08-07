"""FastAPI Web 应用:A 股策略辅助工具展示层。

页面:/ 概览、/screen 选股、/news 新闻(公告流)、/news/{code} 个股新闻列表、
      /news/{code}/{idx} 新闻详情、/stock/{code} 个股评估。
所有页面支持 `?date=YYYY-MM-DD` 查看历史(缺省最新);数据只读 data/analysis(离线 run.py 产出)。非投资建议。
启动:uvicorn web.app:app --reload --port 8000
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

from web import data_access as da

_HERE = Path(__file__).resolve().parent
app = FastAPI(title="A股策略辅助工具")
app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
# cache_size=0 规避 Jinja2 3.1.6 LRUCache 的 unhashable bug
_env = Environment(loader=FileSystemLoader(str(_HERE / "templates")),
                   autoescape=select_autoescape(["html", "xml"]), cache_size=0)
templates = Jinja2Templates(env=_env)


def _nav(date: str) -> dict:
    """全局导航上下文:可选日期列表 + 当前展示日期(供 base.html 日期下拉)。"""
    return {"dates": da.available_dates(), "cur_date": da.as_of(date)}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, date: str = "latest"):
    return templates.TemplateResponse(
        request=request, name="dashboard.html",
        context={"d": da.dashboard(date), "records": da.list_records(date), **_nav(date)})


@app.get("/screen", response_class=HTMLResponse)
def screen(request: Request, date: str = "latest"):
    return templates.TemplateResponse(
        request=request, name="screen.html",
        context={"s": da.screen_page(date), **_nav(date)})


@app.get("/news", response_class=HTMLResponse)
def news(request: Request, date: str = "latest"):
    """每日信息流:全市场新闻流(含 AI 分析),标题可点进详情。"""
    return templates.TemplateResponse(
        request=request, name="news.html",
        context={"flow": da.news_flow(date), "anns": da.news_page(date),
                 "s": {"as_of": da.as_of(date)}, **_nav(date)})


@app.get("/news/{code}", response_class=HTMLResponse)
def news_stock(request: Request, code: str, date: str = "latest"):
    """某票当日原始新闻列表(标题可点进详情)。"""
    rec = da.get_record(code, date)
    name = (rec or {}).get("meta", {}).get("name", code) if rec else code
    items = da.news_list(code, date)
    return templates.TemplateResponse(
        request=request, name="news_list.html",
        context={"code": code, "name": name, "items": items,
                 "s": {"as_of": da.as_of(date)}, **_nav(date)})


@app.get("/news/{code}/{idx}", response_class=HTMLResponse)
def news_item(request: Request, code: str, idx: int, date: str = "latest"):
    """单条新闻详情:完整正文 + 来源 + 原文链接。"""
    item = da.news_detail(code, idx, date)
    if item is None:
        return HTMLResponse(f"<h2>无此新闻:{code} #{idx}</h2>", status_code=404)
    rec = da.get_record(code, date)
    name = (rec or {}).get("meta", {}).get("name", code) if rec else code
    return templates.TemplateResponse(
        request=request, name="news_detail.html",
        context={"code": code, "name": name, "idx": idx, "item": item,
                 "s": {"as_of": da.as_of(date)}, **_nav(date)})


@app.get("/stock/{code}", response_class=HTMLResponse)
def stock(request: Request, code: str, date: str = "latest"):
    rec = da.get_record(code, date)
    if rec is None:
        return HTMLResponse(f"<h2>无此股票数据:{code}</h2>", status_code=404)
    news = da.news_list(code, date)
    return templates.TemplateResponse(
        request=request, name="stock.html",
        context={"r": rec, "kline": da.get_kline(code, date),
                 "news": news, "news_count": len(news), **_nav(date)})


@app.get("/api/stock/{code}")
def api_stock(code: str, date: str = "latest"):
    rec = da.get_record(code, date)
    if rec is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"record": rec, "kline": da.get_kline(code, date)}
