#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
qwen3.8-max 真 Agent 日内选股（function-calling / ReAct 降级）
=============================================================
把千问 3.8-max 当真 Agent：注册数据工具，让模型自主决定调哪个/几次/先看谁，
多轮推理后给出今日下午日内选股。全程留痕（Agent 化 vs 填表版 对比实验）。

研究模拟，非投资建议。不真交易、不 push、不合 main。

运行（必须走 zsh -ic 以带上网关 env）：
    zsh -ic '~/.conda/envs/stock_analysis/bin/python tools/experimental/agent_select_qwen.py'

环境变量：QWEN_BASE_URL / QWEN_API_KEY，模型 id = qwen3.8-max
"""
import os
import re
import json
import glob
import time
import datetime
from pathlib import Path

import pandas as pd

# ----------------------------------------------------------------------------
# 路径（生产数据只读，绝对路径）
# ----------------------------------------------------------------------------
DATE = "2026-09-17"
PREV = "2026-09-16"
DATA = Path("/Users/yqg/Documents/projects/stock_analysis/data")
KLINE_DIR = DATA / "master" / "kline"
INTRADAY_DIR = DATA / "intraday" / DATE
CAND_FILE = INTRADAY_DIR / "noon_candidates_stage1.json"
T1145_FILE = INTRADAY_DIR / "T1145_full.json"
SNAP_FILE = INTRADAY_DIR / "noon_screen_snapshot.json"
ANALYSIS_PREV = DATA / "analysis" / PREV
ROSTER_DIR = DATA / "sector_roster"
OUT_DIR = DATA / "analysis" / DATE

MODEL = "qwen3.8-max"

# ----------------------------------------------------------------------------
# 数据加载（进程内加载一次）
# ----------------------------------------------------------------------------
def _load_json(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


CAND = _load_json(CAND_FILE, {})
CAND_LEDGER = CAND.get("台账", [])
_t1145 = _load_json(T1145_FILE, {})
T1145 = _t1145.get("quotes", {}) if isinstance(_t1145, dict) else {}
_snap = _load_json(SNAP_FILE, {})
SNAP = _snap.get("quotes", {}) if isinstance(_snap, dict) else {}

SECTOR_FOCUS = _load_json(ANALYSIS_PREV / "sector_focus.json", {})
SECTOR_DAILY = _load_json(ANALYSIS_PREV / "sector_daily.json", {})
SECTOR_REGIME = _load_json(ANALYSIS_PREV / "sector_regime.json", {})

# 反向索引：code -> {sector, role}
CODE2SECTOR = {}
for rf in glob.glob(str(ROSTER_DIR / "*.json")):
    rj = _load_json(rf, {})
    sector = rj.get("板块", Path(rf).stem)
    roles = rj.get("roles", {})
    for role_name, members in roles.items():
        for m in members:
            c = m.get("code")
            if c and c not in CODE2SECTOR:
                CODE2SECTOR[c] = {"sector": sector, "role": role_name,
                                  "pos60": m.get("pos60"), "选级": m.get("选级")}

# 板块强弱：sector -> daily 摘要
SECTOR_STRENGTH = {}
for row in (SECTOR_DAILY.get("分板块", []) if isinstance(SECTOR_DAILY, dict) else []):
    SECTOR_STRENGTH[row.get("板块")] = {
        "冷热": row.get("冷热"),
        "focus_score": row.get("focus_score"),
        "动量": (row.get("当日价量") or {}).get("动量_截面档"),
        "涨停数": (row.get("当日价量") or {}).get("涨停数"),
        "新闻净催化": (row.get("新闻催化") or {}).get("净催化"),
    }

MACRO = {}
if isinstance(SECTOR_FOCUS, dict):
    rp = SECTOR_FOCUS.get("风险偏好", {})
    MACRO = {
        "风险偏好": rp.get("风险偏好") if isinstance(rp, dict) else rp,
        "宏观情景": SECTOR_FOCUS.get("宏观情景"),
        "宏观净方向": SECTOR_FOCUS.get("宏观净方向"),
        "hs300方向": rp.get("hs300方向") if isinstance(rp, dict) else None,
    }

_KLINE_CACHE = {}


def _kline(code):
    if code in _KLINE_CACHE:
        return _KLINE_CACHE[code]
    p = KLINE_DIR / f"{code}.parquet"
    df = None
    if p.exists():
        try:
            df = pd.read_parquet(p)
            df = df.sort_values("date").reset_index(drop=True)
        except Exception:
            df = None
    _KLINE_CACHE[code] = df
    return df


def _limit_pct(code):
    """涨停线（百分比）按板块前缀。"""
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("8", "4", "920")):
        return 30.0
    return 10.0


def _price_of(code):
    q = T1145.get(code)
    if q and q.get("price"):
        return float(q["price"]), q
    s = SNAP.get(code)
    if s and s.get("price"):
        return float(s["price"]), None
    df = _kline(code)
    if df is not None and len(df):
        return float(df["close"].iloc[-1]), None
    return None, None


def _name_of(code):
    q = T1145.get(code)
    if q and q.get("name"):
        return q["name"]
    for row in CAND_LEDGER:
        if row.get("code") == code and row.get("name") and row["name"] != code:
            return row["name"]
    return code


# ----------------------------------------------------------------------------
# 工具实现
# ----------------------------------------------------------------------------
def tool_list_candidates(**_):
    """候选池台账（84 只，只读）。"""
    slim = [
        {k: row.get(k) for k in ("候选排名", "code", "name", "完整分",
                                 "数据面综合分", "消息面方向", "消息面分",
                                 "候选来源", "理由")}
        for row in CAND_LEDGER
    ]
    return {"as_of": CAND.get("as_of"), "slot": CAND.get("slot"),
            "候选数": len(slim), "台账": slim}


def tool_get_metrics(code=None, **_):
    """代码算的量化指标。"""
    if not code:
        return {"error": "missing code"}
    code = str(code).zfill(6) if code.isdigit() else code
    price, q = _price_of(code)
    df = _kline(code)
    out = {"code": code, "name": _name_of(code), "现价": price}
    if df is None or len(df) < 20:
        out["error"] = "K线不足20日，指标不可算，三价位需人工确认"
        return out

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    last_close = float(close.iloc[-1])
    p = price if price else last_close

    ma5 = round(float(close.iloc[-5:].mean()), 3)
    ma10 = round(float(close.iloc[-10:].mean()), 3)
    ma20 = round(float(close.iloc[-20:].mean()), 3)
    lo20 = round(float(low.iloc[-20:].min()), 3)
    hi20 = round(float(high.iloc[-20:].max()), 3)
    n60 = min(60, len(df))
    lo60 = round(float(low.iloc[-n60:].min()), 3)
    hi60 = round(float(high.iloc[-n60:].max()), 3)
    pos60 = round((p - lo60) / (hi60 - lo60), 3) if hi60 > lo60 else None
    dist20 = round((hi20 - p) / hi20 * 100, 2) if hi20 else None
    dist60 = round((hi60 - p) / hi60 * 100, 2) if hi60 else None

    # ATR%（近14日）
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = float(tr.iloc[-14:].mean())
    atr_pct = round(atr / p * 100, 2) if p else None

    # 今日涨跌% 与 涨停不可买
    if q and q.get("pct_chg") is not None:
        pct = float(q["pct_chg"])
    elif SNAP.get(code) and SNAP[code].get("pct_chg") is not None:
        pct = float(SNAP[code]["pct_chg"])
    else:
        prev = float(df["close"].iloc[-2]) if len(df) >= 2 else last_close
        pct = round((p - prev) / prev * 100, 2) if prev else None
    lim = _limit_pct(code)
    limit_locked = bool(pct is not None and pct >= lim - 0.5)

    bull = bool(ma5 > ma10 > ma20 and p >= ma5)
    vr = None
    if q and q.get("vol_ratio") is not None:
        vr = round(float(q["vol_ratio"]), 2)

    out.update({
        "当日涨跌pct": pct,
        "MA5": ma5, "MA10": ma10, "MA20": ma20,
        "均线多头": bull,
        "lo20": lo20, "lo60": lo60, "hi20": hi20, "hi60": hi60,
        "距20高pct": dist20, "距60高pct": dist60,
        "pos60": pos60,
        "量比": vr,
        "涨停线pct": lim,
        "涨停不可买": limit_locked,
        "ATRpct": atr_pct,
        "获利盘": None,
        "获利盘说明": "获利盘难精确算，用 pos60 代理（越接近1越高位高抛压）",
    })
    return out


def tool_get_kline(code=None, n=30, **_):
    if not code:
        return {"error": "missing code"}
    code = str(code).zfill(6) if str(code).isdigit() else code
    df = _kline(code)
    if df is None:
        return {"code": code, "error": "无K线"}
    n = int(n or 30)
    sub = df.tail(n)
    rows = [
        {"date": str(r["date"])[:10], "open": round(float(r["open"]), 2),
         "high": round(float(r["high"]), 2), "low": round(float(r["low"]), 2),
         "close": round(float(r["close"]), 2), "volume": float(r["volume"]),
         "pct_chg": float(r.get("pct_chg", 0) or 0)}
        for _, r in sub.iterrows()
    ]
    return {"code": code, "name": _name_of(code), "n": len(rows), "kline": rows}


def tool_get_intraday(code=None, **_):
    if not code:
        return {"error": "missing code"}
    code = str(code).zfill(6) if str(code).isdigit() else code
    q = T1145.get(code)
    s = SNAP.get(code)
    return {"code": code, "T1145": q, "snapshot": s,
            "说明": "T1145=11:30午休冻结全量盘中；snapshot=午盘快照"}


def tool_get_sector_context(code=None, **_):
    if not code:
        return {"error": "missing code"}
    code = str(code).zfill(6) if str(code).isdigit() else code
    info = CODE2SECTOR.get(code)
    if not info:
        return {"code": code, "板块": None,
                "结论": "无板块催化·纯策略（该股不在10大重点板块角色表内）",
                "大盘regime": MACRO}
    sector = info["sector"]
    strength = SECTOR_STRENGTH.get(sector, {})
    return {
        "code": code, "板块": sector, "板块角色": info.get("role"),
        "角色选级": info.get("选级"), "板块强弱": strength,
        "大盘regime": MACRO,
        "说明": "板块角色=龙头/中军/补涨先锋/弹性股；跟涨/补涨角色最高只给观察档",
    }


TOOL_IMPL = {
    "list_candidates": tool_list_candidates,
    "get_metrics": tool_get_metrics,
    "get_kline": tool_get_kline,
    "get_intraday": tool_get_intraday,
    "get_sector_context": tool_get_sector_context,
}

TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "list_candidates",
        "description": "返回今日午盘候选池台账（84只，已过策略0合议筛/财报龙虎闸门）。每只含 code/name/完整分/数据面综合分/消息面方向/消息面分/候选来源/理由。先调它看池子。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_metrics",
        "description": "代码算的量化指标：现价/当日涨跌/MA5/10/20/均线多头/lo20/lo60/hi20/hi60/距20高/距60高/pos60/量比/涨停不可买/ATR%。获利盘难算返回None用pos60代理。",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "6位股票代码"}},
            "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "get_kline",
        "description": "返回该股最近n日OHLCV日K（默认30，含pct_chg）。",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"},
            "n": {"type": "integer", "description": "天数，默认30"}},
            "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "get_intraday",
        "description": "返回该股今日盘中快照（T1145午休冻结全量 + 午盘snapshot）。",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"}}, "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "get_sector_context",
        "description": "返回该股所属板块（申万一级角色表）+板块角色(龙头/中军/补涨先锋/弹性股)+板块强弱+大盘regime。取不到返回'无板块催化·纯策略'。",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"}}, "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "submit_final",
        "description": "研究完成后调用一次，提交最终选股结果（5-8只）。只给入场方式类型/档/建议分/理由/风险/卖出计划，绝不写价位数字（价位由程序回填）。",
        "parameters": {"type": "object", "properties": {
            "selections": {"type": "array", "items": {"type": "object", "properties": {
                "code": {"type": "string"},
                "入场方式": {"type": "string", "enum": ["回踩MA5", "回踩MA20", "回踩前低", "突破确认", "缩量企稳"]},
                "档": {"type": "string", "enum": ["推荐", "观察", "剔除"]},
                "建议分": {"type": "number", "description": "0-10"},
                "理由": {"type": "string", "description": "策略依据+板块依据"},
                "风险": {"type": "string"},
                "卖出计划": {"type": "string", "description": "明早午盘前卖出，目标/触发"}},
                "required": ["code", "入场方式", "档", "建议分", "理由", "风险", "卖出计划"]}}},
            "required": ["selections"]}}},
]

SYSTEM_PROMPT = """你是选股分析师 Agent。你有工具可自主调用来研究候选股，请多轮推理后给出今日下午日内选股（明早午盘前卖出，目标绝对收益为正）。硬纪律：
① 回踩不追高：入场只从 {回踩MA5,回踩MA20,回踩前低,突破确认,缩量企稳} 选类型，绝不追涨停/高开，绝不自己写任何价位数字（价位由程序按均线/前低回填）。
② 硬纪律剔除(命中即剔除档、分≤2)：涨停不可买 / 极高位高抛压(获利盘≥0.95 或 pos60≥0.95且贴高)。
③ 单调性铁律由程序保证：止损<买点≤红线。
④ 数值程序算你别生成：MA/止损/红线程序回填，你只判断/选票/给入场方式类型。
⑤ 只用当前时点可得数据(≤今日盘中)，禁编造，禁用未来信息。
⑥ 跟涨/补涨角色最高只给观察档；龙头/中军/策略强票为主线。
先用 list_candidates 看池子，挑最有希望的十几只用 get_metrics/get_kline/get_intraday/get_sector_context 逐个研究，再定 5-8 只。研究充分后调用 submit_final 一次提交结果。"""

USER_PROMPT = ("请为 2026-09-17（今日）下午做日内选股，选出 5-8 只，明早午盘前卖出、"
               "目标绝对收益为正。先看候选池，自主逐只研究最有希望的十几只，再定稿并调用 submit_final。")


# ----------------------------------------------------------------------------
# 价位程序回填 + 硬 veto
# ----------------------------------------------------------------------------
def backfill_prices(code, form, m):
    price = m.get("现价")
    ma5, ma20 = m.get("MA5"), m.get("MA20")
    lo20 = m.get("lo20")
    if price is None:
        return None, None, None, "数据不足-人工确认"
    entry = stop = redline = None
    note = ""
    try:
        if form == "回踩MA5":
            entry, stop, redline = ma5, ma20, price
        elif form == "回踩MA20":
            entry, stop, redline = ma20, lo20, price
        elif form == "回踩前低":
            entry, stop, redline = lo20, (lo20 * 0.98 if lo20 else None), price
        elif form in ("突破确认", "缩量企稳"):
            entry, stop, redline = price, ma20, price * 1.02
        # 单调性夹逼
        if form in ("回踩MA5", "回踩MA20", "回踩前低") and entry is not None:
            entry = min(entry, price)
        if entry is not None and stop is not None and stop >= entry:
            stop = entry * 0.98
        if entry is not None and redline is not None:
            redline = max(redline, entry)
    except Exception as e:
        note = f"回填异常:{e}"
    if None in (entry, stop, redline):
        note = "数据不足-人工确认"
        return (round(entry, 3) if entry else None,
                round(stop, 3) if stop else None,
                round(redline, 3) if redline else None, note)
    return round(entry, 3), round(stop, 3), round(redline, 3), note


def hard_veto(m):
    """返回 (veto:bool, reason:str)。"""
    reasons = []
    if m.get("涨停不可买"):
        reasons.append("涨停不可买")
    pos60 = m.get("pos60")
    dist60 = m.get("距60高pct")
    if pos60 is not None and pos60 >= 0.95 and dist60 is not None and dist60 <= 3:
        reasons.append(f"pos60={pos60}≥0.95且贴高(距60高{dist60}%)")
    # 获利盘=None，用 pos60 代理已覆盖
    return (len(reasons) > 0, "；".join(reasons))


# ----------------------------------------------------------------------------
# Agent loop（function-calling），失败降级 ReAct
# ----------------------------------------------------------------------------
def run_agent(client, trace):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_PROMPT},
    ]
    tool_call_count = 0
    final_selections = None
    max_iters = 40
    usage = {"prompt": 0, "completion": 0, "calls": 0}
    for it in range(max_iters):
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS_SCHEMA,
            tool_choice="auto", temperature=0,
        )
        if getattr(resp, "usage", None):
            usage["prompt"] += resp.usage.prompt_tokens or 0
            usage["completion"] += resp.usage.completion_tokens or 0
            usage["calls"] += 1
        run_agent.last_usage = usage
        msg = resp.choices[0].message
        think = (msg.content or "").strip()
        tcs = msg.tool_calls or []
        trace.append({"iter": it, "think": think,
                      "tool_calls": [{"name": t.function.name,
                                      "args": t.function.arguments} for t in tcs]})
        # 回喂 assistant 消息
        assistant_msg = {"role": "assistant", "content": msg.content or ""}
        if tcs:
            assistant_msg["tool_calls"] = [{
                "id": t.id, "type": "function",
                "function": {"name": t.function.name, "arguments": t.function.arguments},
            } for t in tcs]
        messages.append(assistant_msg)

        if not tcs:
            # 无工具调用：可能是最终文本；尝试解析 JSON
            final_selections = _try_parse_final(think)
            if final_selections:
                trace[-1]["note"] = "从文本解析出最终选股"
                break
            # 否则提示继续
            messages.append({"role": "user", "content": "请继续研究，或研究充分后调用 submit_final 提交最终选股。"})
            continue

        for t in tcs:
            name = t.function.name
            try:
                args = json.loads(t.function.arguments or "{}")
            except Exception:
                args = {}
            if name == "submit_final":
                final_selections = args.get("selections")
                result = {"ok": True, "received": len(final_selections or [])}
            else:
                tool_call_count += 1
                fn = TOOL_IMPL.get(name)
                try:
                    result = fn(**args) if fn else {"error": f"unknown tool {name}"}
                except Exception as e:
                    result = {"error": str(e)}
            # trace 记返回摘要
            trace[-1].setdefault("tool_results", []).append(
                {"name": name, "args": args, "result_summary": _summ(result)})
            messages.append({"role": "tool", "tool_call_id": t.id,
                             "content": json.dumps(result, ensure_ascii=False)})
        if final_selections is not None:
            break
    return final_selections, tool_call_count, "function-calling"


def _try_parse_final(text):
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.S)
    raw = m.group(1) if m else None
    if not raw:
        m2 = re.search(r"(\[\s*\{.*\}\s*\])", text, re.S)
        raw = m2.group(1) if m2 else None
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            obj = obj.get("selections", obj)
        return obj if isinstance(obj, list) else None
    except Exception:
        return None


def _summ(result):
    s = json.dumps(result, ensure_ascii=False)
    return s if len(s) <= 600 else s[:600] + "...(截断)"


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trace = []
    mechanism = "function-calling"
    tool_calls = 0
    selections = None
    err = None

    try:
        from openai import OpenAI
        base = os.getenv("QWEN_BASE_URL")
        key = os.getenv("QWEN_API_KEY")
        if not base or not key:
            raise RuntimeError("QWEN_BASE_URL/QWEN_API_KEY 缺失（须走 zsh -ic）")
        client = OpenAI(base_url=base, api_key=key)
        last_e = None
        for attempt in range(3):
            try:
                selections, tool_calls, mechanism = run_agent(client, trace)
                if selections:
                    break
            except Exception as e:
                last_e = e
                trace.append({"error_retry": attempt, "msg": str(e)})
                time.sleep(2)
        if not selections and last_e:
            raise last_e
    except Exception as e:
        err = str(e)
        trace.append({"fatal": err})

    # 降级：LLM 不可用 -> 规则版
    degraded = False
    if not selections:
        degraded = True
        mechanism = "规则版降级(LLM不可用)"
        selections = _rule_fallback(trace)

    # 价位回填 + veto + 组装产物
    results = []
    for sel in selections or []:
        code = str(sel.get("code", "")).strip()
        if code.isdigit():
            code = code.zfill(6)
        m = tool_get_metrics(code=code)
        form = sel.get("入场方式", "缩量企稳")
        entry, stop, redline, note = backfill_prices(code, form, m)
        veto, vreason = hard_veto(m)
        dang = sel.get("档", "观察")
        score = sel.get("建议分", 5)
        if veto:
            dang = "剔除"
            score = min(score, 2)
        rec = {
            "code": code, "name": m.get("name", code),
            "档": dang, "建议分": score, "入场方式": form,
            "挂单价": entry, "止损价": stop, "不追高上限红线": redline,
            "回填说明": note or ("硬veto剔除:" + vreason if veto else ""),
            "理由": sel.get("理由", ""), "风险": sel.get("风险", ""),
            "卖出计划": sel.get("卖出计划", ""),
            "信号": {"量比": m.get("量比"), "距60高pct": m.get("距60高pct"),
                     "pos60(获利盘代理)": m.get("pos60"),
                     "均线多头": m.get("均线多头"), "涨停不可买": m.get("涨停不可买")},
            "现价": m.get("现价"), "当日涨跌pct": m.get("当日涨跌pct"),
        }
        results.append(rec)

    _write_outputs(results, trace, mechanism, tool_calls, err, degraded)

    print("=" * 60)
    print(f"机制: {mechanism} | 千问调工具次数: {tool_calls} | 选中: {len(results)}只")
    for r in results:
        print(f"  {r['code']} {r['name']} [{r['档']}] 分{r['建议分']} "
              f"{r['入场方式']} 挂单{r['挂单价']} 止损{r['止损价']} 红线{r['不追高上限红线']}")
    u = getattr(run_agent, "last_usage", None)
    if u:
        print(f"token用量: prompt={u['prompt']} completion={u['completion']} "
              f"total={u['prompt']+u['completion']} (LLM请求{u['calls']}次)")
    print("产物目录:", OUT_DIR)


def _rule_fallback(trace):
    """LLM 不可用时：用 metrics 规则选票。"""
    trace.append({"note": "进入规则版降级：按均线多头+非涨停+pos60适中+量比打分"})
    scored = []
    for row in CAND_LEDGER[:60]:
        code = row.get("code")
        m = tool_get_metrics(code=code)
        if m.get("error") or m.get("现价") is None:
            continue
        veto, _ = hard_veto(m)
        if veto:
            continue
        pos60 = m.get("pos60") or 0.5
        bull = m.get("均线多头")
        vr = m.get("量比") or 1.0
        base = float(row.get("完整分", 0)) * 4
        s = base + (1.5 if bull else 0) + (0.5 if 0.3 <= pos60 <= 0.85 else -0.5) \
            + (0.5 if 1.0 <= vr <= 2.5 else 0)
        form = "回踩MA5" if bull else "缩量企稳"
        scored.append((s, {
            "code": code, "入场方式": form, "档": "观察",
            "建议分": round(min(8, max(3, s)), 1),
            "理由": f"规则版:完整分{row.get('完整分')},均线多头={bull},pos60={pos60}",
            "风险": "LLM不可用降级,仅规则近似", "卖出计划": "明早午盘前卖出,止盈+3%或跌破止损走"}))
    scored.sort(key=lambda x: -x[0])
    return [x[1] for x in scored[:6]]


def _write_outputs(results, trace, mechanism, tool_calls, err, degraded):
    # 1) JSON
    with open(OUT_DIR / f"qwen_agent_选股_{DATE}.json", "w", encoding="utf-8") as f:
        json.dump({
            "date": DATE, "机制": mechanism, "千问调工具次数": tool_calls,
            "token用量": getattr(run_agent, "last_usage", None),
            "degraded": degraded, "error": err,
            "免责": "研究模拟，非投资建议。不真交易。",
            "selections": results,
        }, f, ensure_ascii=False, indent=2)

    # 2) trace md
    lines = [f"# 千问3.8-max Agent 选股过程留痕 · {DATE}", "",
             "> 研究模拟，非投资建议。Agent 化 vs 填表版 对比实验。", "",
             f"- 机制: **{mechanism}**",
             f"- 千问自主调用数据工具次数: **{tool_calls}**",
             f"- 降级: {degraded} | error: {err}", ""]
    # 工具调用统计
    tool_hist = {}
    for step in trace:
        for tr in step.get("tool_results", []):
            tool_hist[tr["name"]] = tool_hist.get(tr["name"], 0) + 1
    lines.append("## 工具调用分布（体现自主取数）")
    for k, v in sorted(tool_hist.items(), key=lambda x: -x[1]):
        lines.append(f"- `{k}`: {v} 次")
    lines.append("")
    lines.append("## 相比「填表版」的差异")
    lines.append("- 填表版=单次固定 payload（把所有指标一次性塞给模型，模型只填空）。")
    lines.append("- Agent 版=模型**自主决定**先看谁、调哪个工具、调几次；下面每轮是模型自己的取数与推理决策。")
    lines.append("")
    lines.append("## 逐轮留痕")
    for step in trace:
        if "iter" in step:
            lines.append(f"### 轮次 {step['iter']}")
            if step.get("think"):
                lines.append(f"**模型推理**: {step['think']}")
            for tc in step.get("tool_calls", []):
                lines.append(f"- 决定调用 `{tc['name']}` 参数 `{tc['args']}`")
            for tr in step.get("tool_results", []):
                lines.append(f"  - `{tr['name']}({tr['args']})` -> {tr['result_summary']}")
            if step.get("note"):
                lines.append(f"- 备注: {step['note']}")
        else:
            lines.append(f"- (系统): {json.dumps(step, ensure_ascii=False)}")
        lines.append("")
    with open(OUT_DIR / f"qwen_agent_trace_{DATE}.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # 3) 操作卡 md
    ol = [f"# 千问3.8-max Agent 选股 · 通俗操作卡 · {DATE}", "",
          "> 研究模拟，非投资建议。明早午盘前卖出。价位为程序按均线/前低回填。", ""]
    for r in results:
        ol.append(f"## {r['name']} {r['code']} ｜ {r['档']} 建议分{r['建议分']}")
        e, s, rd = r["挂单价"], r["止损价"], r["不追高上限红线"]
        if e is None:
            ol.append("  - 价位：数据不足，**人工确认**")
        else:
            ol.append(f"  - 买点：约 {s if s else '-'}–{e} 元 挂限价买（不追高）" if r["入场方式"].startswith("回踩")
                      else f"  - 买点：{e} 元一线（{r['入场方式']}，不追高）")
            ol.append(f"  - 止损：跌破 {s} 元 走")
            ol.append(f"  - 红线：高于 {rd} 元 = 追高别买")
        ol.append(f"  - 卖出：{r['卖出计划']}")
        sg = r["信号"]
        ol.append(f"  - 信号：量比{sg['量比']} / 距60高{sg['距60高pct']}% / "
                  f"获利盘代理pos60={sg['pos60(获利盘代理)']} / 均线多头{sg['均线多头']}")
        ol.append(f"  - 理由：{r['理由']}")
        if r.get("回填说明"):
            ol.append(f"  - 备注：{r['回填说明']}")
        ol.append("")
    with open(OUT_DIR / f"qwen_agent_操作卡_{DATE}.md", "w", encoding="utf-8") as f:
        f.write("\n".join(ol))


if __name__ == "__main__":
    main()
