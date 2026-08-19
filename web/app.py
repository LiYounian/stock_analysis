"""FastAPI Web 应用:A 股策略辅助工具展示层。

页面:/ 概览、/screen 选股、/news 新闻(公告流)、/news/{code} 个股新闻列表、
      /news/{code}/{idx} 新闻详情、/stock/{code} 个股评估。
所有页面支持 `?date=YYYY-MM-DD` 查看历史(缺省最新);数据只读 data/analysis(离线 run.py 产出)。非投资建议。
启动:uvicorn web.app:app --reload --port 8801
页面另含 /sepa SEPA+VCP 监控(只读 view)。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

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


@app.get("/selection", response_class=HTMLResponse)
def selection(request: Request, date: str = "latest"):
    """选股结果页:选出哪些票/为什么(达标理由)/合议怎么看,可勾选专家实时重排。"""
    return templates.TemplateResponse(
        request=request, name="selection.html",
        context={"s": da.selection_page(date), **_nav(date)})


@app.get("/financial", response_class=HTMLResponse)
def financial(request: Request, date: str = "latest"):
    """财报分析页:带财报块的票的质地评级/审计双闸门/关键红旗/LLM综合归纳一览,可点进个股详情。"""
    return templates.TemplateResponse(
        request=request, name="financial.html",
        context={"f": da.financial_page(date), **_nav(date)})


@app.get("/financial/{code}", response_class=HTMLResponse)
def financial_detail(request: Request, code: str, date: str = "latest"):
    """单只票的详细财报页:财报分析+证据(带来源)+ AI 综合归纳 + 审计标准。"""
    d = da.financial_detail(code, date)
    if d is None:
        return HTMLResponse(f"<p>{code} 无财报数据(该票未采财报,或该日期无记录)。"
                            f"<a href='/financial?date={date}'>返回财报榜</a></p>", status_code=404)
    return templates.TemplateResponse(
        request=request, name="financial_detail.html", context={"d": d, **_nav(date)})


@app.get("/sepa", response_class=HTMLResponse)
def sepa(request: Request, date: str = "latest"):
    """SEPA+VCP 监控:技术合格池 + 重点观察池 + 雷达。只读 view,不重算。"""
    return templates.TemplateResponse(
        request=request, name="sepa.html",
        context={"s": da.sepa_page(date), **_nav(date)})


@app.get("/sepa/{code}", response_class=HTMLResponse)
def sepa_detail(request: Request, code: str, date: str = "latest"):
    """单票收缩结构参考图(不叫 VCP 完成)。"""
    d = da.sepa_detail(code, date)
    if d is None:
        return HTMLResponse(
            f"<p>{code} 无收缩结构图(该日未入观察池或未跑 sepa)。"
            f"<a href='/sepa?date={date}'>返回监控</a></p>", status_code=404)
    return templates.TemplateResponse(
        request=request, name="sepa_detail.html", context={"d": d, **_nav(date)})


@app.get("/selection-analysis", response_class=HTMLResponse)
def selection_analysis(request: Request, report: str = "", date: str = "latest"):
    """选股分析报告页:列出定向分析报告(data/reports/选股分析/*.md),可选一份查看(markdown 渲染)。

    用途:策略提供者给定要定向分析的股票 → 离线分析产出报告落该目录 → 此页选看。
    """
    reports = da.list_analysis_reports()
    pick = report or (reports[0]["name"] if reports else "")
    selected = da.get_analysis_report(pick) if pick else None
    return templates.TemplateResponse(
        request=request, name="selection_analysis.html",
        context={"reports": reports, "selected": selected, **_nav(date)})


@app.get("/fund-flow", response_class=HTMLResponse)
def fund_flow(request: Request, date: str = "latest"):
    """A股资金流向页:大盘 5 单 + 行业/概念板块榜。前端直连东财 push2 30s 轮询,不走后端。"""
    return templates.TemplateResponse(
        request=request, name="fund_flow.html",
        context={**_nav(date)})


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
    # pandas 算出的 NaN/Inf(kline.volume、资金流等)落盘后会让严格 JSON 编码器 500,统一净化为 null
    return da.json_safe({"record": rec, "kline": da.get_kline(code, date)})


# ———— 票池管理:页面 + 增删 API(写操作委托编排层 pool_service)————
@app.get("/pool", response_class=HTMLResponse)
def pool(request: Request):
    return templates.TemplateResponse(
        request=request, name="pool.html",
        context={"p": da.pool_page(), **_nav("latest")})


class PoolAdd(BaseModel):
    code: str
    name: str
    industry: str = ""
    sector: str
    market: str = "A"


@app.post("/api/pool")
def api_pool_add(body: PoolAdd):
    """新增一只票:入池 → 联网采集 → 重建产物。校验失败返回 400。"""
    from tools import pool_service
    try:
        res = pool_service.add_and_collect(
            body.code, body.name, body.industry, body.sector, market=body.market)
        return {"ok": True, **res}
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/pool/{code}/delete")
def api_pool_delete(code: str):
    """删除一只票:出池 → 清缓存 → 重建产物。不存在返回 404。"""
    from tools import pool_service
    try:
        res = pool_service.remove_and_cleanup(code)
        return {"ok": True, **res}
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
