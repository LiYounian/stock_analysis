"""FastAPI Web 应用:A 股策略辅助工具展示层。

页面:/ 概览、/screen 选股、/news 新闻(公告流)、/news/{code} 个股新闻列表、
      /news/{code}/{idx} 新闻详情、/stock/{code} 个股评估。
所有页面支持 `?date=YYYY-MM-DD` 查看历史(缺省最新);数据只读 data/analysis(离线 run.py 产出)。非投资建议。
启动:uvicorn web.app:app --reload --port 8801
"""
from __future__ import annotations

from pathlib import Path
import pandas as pd

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


@app.get("/strategy/max-range", response_class=HTMLResponse)
def max_range_strategy(request: Request, date: str = "latest"):
    """最大范围选股：全 A 强势广度情绪曲线 + 当日/历史入选结果。"""
    return templates.TemplateResponse(
        request=request, name="strategy_max_range.html",
        context={"s": da.max_range_page(date), **_nav(date)})


@app.get("/strategy", response_class=HTMLResponse)
def strategy_center(request: Request, date: str = "latest"):
    return templates.TemplateResponse(request=request, name="strategy_index.html",
        context={"as_of": da.as_of(date), **_nav(date)})


@app.get("/strategy/multi", response_class=HTMLResponse)
def multi_strategy(request: Request, date: str = "latest", strategies: str = ""):
    return templates.TemplateResponse(request=request, name="strategy_multi.html",
        context={"m": da.multi_strategy_page(strategies.split(","), date), **_nav(date)})


@app.get("/strategy/strong", response_class=HTMLResponse)
def strong_strategy(request: Request, date: str = "latest"):
    return templates.TemplateResponse(request=request, name="strategy_strong.html",
        context={"s": da.strong_page(date), **_nav(date)})

@app.get("/strategy/rub", response_class=HTMLResponse)
def rub_strategy(request: Request, date: str = "latest"):
    return templates.TemplateResponse(request=request,name="strategy_rub.html",context={"s":da.strategy_page("拉揉搓",date),**_nav(date)})


def _volume_page(request: Request, date: str, view_name: str, slug: str, subtitle: str, conditions: list[str], columns: list[tuple[str, str]]):
    return templates.TemplateResponse(request=request, name="strategy_volume.html", context={
        "s": da.strategy_page(view_name, date), "title": view_name, "slug": slug,
        "subtitle": subtitle, "conditions": conditions, "columns": columns, **_nav(date),
    })


@app.get("/strategy/single-volume", response_class=HTMLResponse)
def single_volume_strategy(request: Request, date: str = "latest"):
    return _volume_page(request, date, "单日放量", "single-volume", "换手放大、放量上涨与长期均线趋势共振",
        ["当日换手率 > 昨日换手率的 1.7 倍", "收盘价较昨收上涨超过 3%", "MA200 上行", "MA50 位于 MA200 上方"],
        [("turnover", "当日换手%"), ("prior_turnover", "昨日换手%")])


@app.get("/strategy/low-volume", response_class=HTMLResponse)
def low_volume_strategy(request: Request, date: str = "latest"):
    return _volume_page(request, date, "低位单日放量", "low-volume", "日线多头、上穿 30 周线与 10 日最大成交量",
        ["收盘站上 MA5/10/20/30/200", "当日上穿动态 30 周均线", "成交量为近 10 日最高"],
        [("ma30w", "30周均线"), ("volume", "成交量")])


@app.get("/strategy/continuous-volume", response_class=HTMLResponse)
def continuous_volume_strategy(request: Request, date: str = "latest"):
    return _volume_page(request, date, "连续放量", "continuous-volume", "连续上涨、成交递增与中长期均线多头",
        ["收盘连续两日走高，且相对昨日/前日均上涨超过 4%", "当日成交量大于昨日", "收盘站上 MA20/50/200", "MA5、MA10 均位于 MA20 上方"],
        [("rise1%", "较昨收%"), ("rise2%", "较前日%")])

@app.get("/strategy/{strategy}/stock/{code}", response_class=HTMLResponse)
def strategy_stock(request: Request, strategy: str, code: str, signal_date: str):
    from tools.collectors import market
    try:
        df=market.load_kline(code).tail(180)
    except FileNotFoundError:
        return HTMLResponse(f"<h2>无K线数据:{code}</h2>",status_code=404)
    kline={"dates":[str(x)[:10] for x in df.date],"open":df.open.astype(float).tolist(),"close":df.close.astype(float).tolist(),"low":df.low.astype(float).tolist(),"high":df.high.astype(float).tolist(),"volume":df.volume.fillna(0).astype(float).tolist()}
    for n in (5,20,60):
        ma = df.close.rolling(n).mean().round(4)
        # json.dumps 会把 float('nan') 写成 NaN，浏览器 JSON.parse 无法解析；必须转 null。
        kline[f"ma{n}"] = [None if pd.isna(v) else float(v) for v in ma]
    signal=df[pd.to_datetime(df.date).dt.strftime('%Y-%m-%d')==signal_date]
    latest=float(df.close.iloc[-1]); entry=float(signal.close.iloc[-1]) if len(signal) else None
    # 所有策略共用同一张日线 K 线图；策略仅通过这两个标准字段提供入选日标记。
    kline["signal_date"] = signal_date if entry is not None else None
    kline["signal_close"] = entry
    return templates.TemplateResponse(request=request,name="strategy_stock.html",context={"code":code,"strategy":strategy,"signal_date":signal_date,"kline":kline,"entry":entry,"latest":latest,"return_pct":None if entry is None else round((latest/entry-1)*100,2),**_nav("latest")})


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


@app.post("/api/pool")
def api_pool_add(body: PoolAdd):
    """新增一只票:入池 → 联网采集 → 重建产物。校验失败返回 400。"""
    from tools import pool_service
    try:
        res = pool_service.add_and_collect(body.code, body.name, body.industry, body.sector)
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
