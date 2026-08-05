"""FastAPI Web 应用:A 股策略辅助工具展示层。

页面:/ Dashboard(今日概览)、/stock/{code} 个股评估(K线+技术+基本面+资金流+预测)。
数据只读 data/analysis(离线 run.py 产出)。非投资建议。
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


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request, name="dashboard.html",
        context={"d": da.dashboard(), "records": da.list_records()})


@app.get("/stock/{code}", response_class=HTMLResponse)
def stock(request: Request, code: str):
    rec = da.get_record(code)
    if rec is None:
        return HTMLResponse(f"<h2>无此股票数据:{code}</h2>", status_code=404)
    return templates.TemplateResponse(
        request=request, name="stock.html",
        context={"r": rec, "kline": da.get_kline(code)})


@app.get("/api/stock/{code}")
def api_stock(code: str):
    rec = da.get_record(code)
    if rec is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"record": rec, "kline": da.get_kline(code)}
