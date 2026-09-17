#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
DeepSeek-Agent 化日内选股（对照实验：Agent 版 vs 填表版）。

把 DeepSeek 当真 Agent：定义数据工具（function-calling），让模型自主决定
调哪个工具、调几次、先看谁，多轮推理后从候选池选 5-8 只今日下午日内票。

- 真跑 LLM 必须走 `zsh -ic`（非交互 bash 无网关环境变量）。
- 数值（MA/止损/红线）由代码回填；模型只判断/选票/给入场方式类型。
- 全程留痕：每轮思考 + 工具调用 + 返回摘要 → markdown。

用法：
    zsh -ic '~/.conda/envs/stock_analysis/bin/python \
        /path/to/agent_select_ds.py'

研究模拟，非投资建议。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime

import pandas as pd

# ---------------------------------------------------------------------------
# 0. 常量与路径（生产数据在主仓绝对路径，只读）
# ---------------------------------------------------------------------------
TODAY = "2026-09-17"
KLINE_ASOF = "2026-09-16"
SECTOR_ASOF = "2026-09-16"
MODEL_ID = os.environ.get("LLM_MODEL") or "deepseek-v4-pro"
MAX_ITERS = 40

DATA_ROOT = "/Users/yqg/Documents/projects/stock_analysis/data"
OUT_DIR = f"/Users/yqg/Documents/projects/stock_analysis/data/analysis/{TODAY}"

P_CAND = f"{DATA_ROOT}/intraday/{TODAY}/noon_candidates_stage1.json"
P_T1145 = f"{DATA_ROOT}/intraday/{TODAY}/T1145_full.json"
P_SNAP = f"{DATA_ROOT}/intraday/{TODAY}/noon_screen_snapshot.json"
P_SECTOR_FOCUS = f"{DATA_ROOT}/analysis/{SECTOR_ASOF}/sector_focus.json"
P_SECTOR_DAILY = f"{DATA_ROOT}/analysis/{SECTOR_ASOF}/sector_daily.json"
D_ROSTER = f"{DATA_ROOT}/sector_roster"
D_KLINE = f"{DATA_ROOT}/master/kline"


# ---------------------------------------------------------------------------
# 1. 一次性加载数据
# ---------------------------------------------------------------------------
def _load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


print("[load] 加载生产数据 ...", file=sys.stderr)
CAND = _load_json(P_CAND)
LEDGER = CAND.get("台账", [])
MARKET_BREADTH = CAND.get("market_breadth", {})
T1145 = _load_json(P_T1145).get("quotes", {})
SNAP = _load_json(P_SNAP).get("quotes", {})
SECTOR_FOCUS = _load_json(P_SECTOR_FOCUS)
try:
    SECTOR_DAILY = _load_json(P_SECTOR_DAILY)
except Exception:
    SECTOR_DAILY = {}

# 板块 focus 索引：板块名 -> focus 条目
FOCUS_BY_SECTOR = {b["板块"]: b for b in SECTOR_FOCUS.get("重点板块池", [])}
RISK_PREF = SECTOR_FOCUS.get("风险偏好", {})

# code -> (板块, 角色) 反查（来自 sector_roster 角色表；只覆盖被选中的角色股）
CODE2SECTOR = {}
if os.path.isdir(D_ROSTER):
    for fn in os.listdir(D_ROSTER):
        if not fn.endswith(".json"):
            continue
        try:
            d = _load_json(os.path.join(D_ROSTER, fn))
        except Exception:
            continue
        sec = d.get("板块")
        for role, lst in (d.get("roles") or {}).items():
            for e in lst:
                if isinstance(e, dict) and e.get("code"):
                    CODE2SECTOR.setdefault(e["code"], (sec, role))

LEDGER_BY_CODE = {t["code"]: t for t in LEDGER}
CAND_CODES = [t["code"] for t in LEDGER]


# ---------------------------------------------------------------------------
# 2. 计算辅助
# ---------------------------------------------------------------------------
def _limit_pct(code: str) -> float:
    """按板块判涨停幅度：主板10% / 创业板科创板20% / 北交所30%。"""
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def _load_kline(code: str):
    p = os.path.join(D_KLINE, f"{code}.parquet")
    if not os.path.exists(p):
        return None
    try:
        return pd.read_parquet(p)
    except Exception:
        return None


def _intraday(code: str):
    """今日盘中快照（优先 T1145_full，回退 snapshot）。"""
    q = T1145.get(code)
    if q:
        return q
    s = SNAP.get(code)
    if s:
        return dict(s)
    return None


def _round(x, n=3):
    try:
        return round(float(x), n)
    except Exception:
        return None


def compute_metrics(code: str) -> dict:
    """代码计算并返回一只股票的量化画像。"""
    out = {"code": code}
    intr = _intraday(code)
    df = _load_kline(code)
    ledger = LEDGER_BY_CODE.get(code, {})
    out["name"] = (intr or {}).get("name") or ledger.get("name") or code

    # 现价 / 当日涨跌 / 量比（盘中）
    price = None
    if intr:
        price = intr.get("price")
        out["现价"] = price
        out["当日涨跌%"] = intr.get("pct_chg")
        out["量比"] = intr.get("vol_ratio")
        out["盘中换手%"] = intr.get("turnover")
        out["prev_close"] = intr.get("prev_close")
    else:
        out["现价"] = None
        out["当日涨跌%"] = None
        out["量比"] = None
        out["数据缺"] = "无盘中快照"

    if df is None or len(df) < 20:
        out["数据缺"] = out.get("数据缺", "") + " 无足量K线"
        out["涨停不可买"] = None
        return out

    df = df.sort_values("date").reset_index(drop=True)
    closes = df["close"].astype(float)
    highs = df["high"].astype(float)
    lows = df["low"].astype(float)
    last_close = float(closes.iloc[-1])  # 结算收盘(09-16)
    # 若无盘中价，用结算收盘代理现价
    ref_price = float(price) if price else last_close
    out.setdefault("现价", ref_price)

    ma5 = float(closes.tail(5).mean())
    ma10 = float(closes.tail(10).mean())
    ma20 = float(closes.tail(20).mean())
    out["MA5"] = _round(ma5)
    out["MA10"] = _round(ma10)
    out["MA20"] = _round(ma20)
    out["均线多头"] = bool(ma5 > ma10 > ma20 and ref_price >= ma5)

    lo20 = float(lows.tail(20).min())
    lo60 = float(lows.tail(60).min())
    hi20 = float(highs.tail(20).max())
    hi60 = float(highs.tail(60).max())
    out["近20日最低lo20"] = _round(lo20)
    out["近60日最低lo60"] = _round(lo60)
    out["近20日最高hi20"] = _round(hi20)
    out["近60日最高hi60"] = _round(hi60)
    out["距20高%"] = _round((hi20 - ref_price) / hi20 * 100, 2) if hi20 else None
    out["距60高%"] = _round((hi60 - ref_price) / hi60 * 100, 2) if hi60 else None
    out["pos60"] = _round((ref_price - lo60) / (hi60 - lo60), 3) if hi60 > lo60 else None

    # 获利盘无持仓成本分布，无法精确算 → None，用 pos60 代理
    out["获利盘"] = None
    out["获利盘说明"] = "无持仓成本分布,用pos60代理高位抛压"

    # 涨停不可买：当日涨幅 >= 涨停线 - 0.005
    limit = _limit_pct(code)
    out["涨停线%"] = round(limit * 100, 1)
    pct = out.get("当日涨跌%")
    if pct is not None:
        out["涨停不可买"] = bool(pct / 100.0 >= limit - 0.005)
    else:
        out["涨停不可买"] = None

    # ATR%（近14日）
    if len(df) >= 15:
        prev_close = closes.shift(1)
        tr = pd.concat(
            [
                highs - lows,
                (highs - prev_close).abs(),
                (lows - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = float(tr.tail(14).mean())
        out["ATR%"] = _round(atr / ref_price * 100, 2) if ref_price else None
    else:
        out["ATR%"] = None

    return out


def compute_sector_context(code: str) -> dict:
    """板块消息面/催化/强弱 + 大盘 regime。取不到板块→纯策略。"""
    regime = {
        "大盘风险偏好": RISK_PREF.get("风险偏好"),
        "宏观净方向": RISK_PREF.get("宏观净方向"),
        "hs300方向": RISK_PREF.get("hs300方向"),
        "净广度": RISK_PREF.get("净广度"),
        "涨停家数": RISK_PREF.get("涨停"),
        "跌停家数": RISK_PREF.get("跌停"),
        "市场广度": MARKET_BREADTH,
    }
    hit = CODE2SECTOR.get(code)
    if not hit:
        return {
            "code": code,
            "板块": None,
            "角色": None,
            "板块催化": "无板块催化·纯策略(该股不在重点板块角色表中)",
            "大盘regime": regime,
        }
    sec, role = hit
    focus = FOCUS_BY_SECTOR.get(sec, {})
    is_focus = sec in FOCUS_BY_SECTOR
    return {
        "code": code,
        "板块": sec,
        "角色": role,
        "是否重点板块": is_focus,
        "focus_score": focus.get("focus_score"),
        "冷热": focus.get("冷热"),
        "新闻净催化": focus.get("新闻净催化"),
        "利好条": focus.get("利好条"),
        "利空条": focus.get("利空条"),
        "拥挤档": focus.get("拥挤档"),
        "动量_截面档": focus.get("动量_截面档"),
        "板块理由": focus.get("理由"),
        "大盘regime": regime,
    }


# ---------------------------------------------------------------------------
# 3. 工具实现（供 DeepSeek 调用）
# ---------------------------------------------------------------------------
def tool_list_candidates(**_):
    """返回候选池台账精简视图。"""
    rows = []
    for t in LEDGER:
        rows.append(
            {
                "候选排名": t.get("候选排名"),
                "code": t.get("code"),
                "数据面综合分": t.get("数据面综合分"),
                "消息面方向": t.get("消息面方向"),
                "消息面分": t.get("消息面分"),
                "候选来源": t.get("候选来源"),
                "理由": t.get("理由", ""),
            }
        )
    return {"候选数": len(rows), "market_breadth": MARKET_BREADTH, "台账": rows}


def tool_get_metrics(code, **_):
    return compute_metrics(str(code))


def tool_get_kline(code, n=30, **_):
    df = _load_kline(str(code))
    if df is None:
        return {"code": code, "error": "无K线"}
    df = df.sort_values("date").tail(int(n))
    recs = []
    for _, r in df.iterrows():
        recs.append(
            {
                "date": str(r["date"]),
                "open": _round(r["open"]),
                "high": _round(r["high"]),
                "low": _round(r["low"]),
                "close": _round(r["close"]),
                "volume": int(r["volume"]) if pd.notna(r["volume"]) else None,
                "pct_chg": _round(r["pct_chg"], 2),
            }
        )
    return {"code": code, "as_of": KLINE_ASOF, "n": len(recs), "kline": recs}


def tool_get_intraday(code, **_):
    intr = _intraday(str(code))
    if not intr:
        return {"code": code, "error": "无盘中快照"}
    return {"code": code, "as_of": f"{TODAY} 11:30冻结", **intr}


def tool_get_sector_context(code, **_):
    return compute_sector_context(str(code))


TOOL_IMPL = {
    "list_candidates": tool_list_candidates,
    "get_metrics": tool_get_metrics,
    "get_kline": tool_get_kline,
    "get_intraday": tool_get_intraday,
    "get_sector_context": tool_get_sector_context,
}

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "list_candidates",
            "description": "返回午盘已过一轮筛选的候选池台账(84只),每只含数据面综合分/消息面方向/消息面分/候选来源/理由。选股第一步应先调它看池子。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_metrics",
            "description": "代码计算并返回一只股票的量化画像:name/现价/当日涨跌%/MA5/MA10/MA20/均线多头/近20日最低lo20/近60日最低lo60/近20/60日最高/距高%/pos60/量比/涨停不可买/ATR%/获利盘(无法算返回None用pos60代理)。判断某只是否值得选前必看。",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "6位股票代码"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_kline",
            "description": "返回最近n日日线OHLCV+涨跌幅(结算至2026-09-16),用于看形态(是否回踩企稳/突破/放量)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "n": {"type": "integer", "description": "天数,默认30"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_intraday",
            "description": "返回该股今日盘中快照(11:30午休冻结):现价/开盘/最高/最低/量比/换手/振幅/涨跌%。",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sector_context",
            "description": "返回该股所属板块的消息面/催化/强弱(focus_score/冷热/新闻净催化/拥挤档/角色龙头中军补涨弹性)+大盘regime。取不到板块返回'无板块催化·纯策略'。判断是否主线/跟涨用。",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 4. 系统提示（原样喂给 DeepSeek，测指令遵循度）
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是选股分析师 Agent。你有工具可自主调用来研究候选股，请多轮推理后给出今日下午日内选股（明早午盘前卖出，目标绝对收益为正）。硬纪律：
① 回踩不追高：入场只从 {回踩MA5,回踩MA20,回踩前低,突破确认,缩量企稳} 选类型，绝不追涨停/高开，绝不自己写任何价位数字（价位由程序按均线/前低回填）。
② 硬纪律剔除(命中即剔除档、分≤2)：涨停不可买 / 极高位高抛压(获利盘≥0.95 或 pos60≥0.95且贴高)。
③ 单调性铁律由程序保证：止损<买点≤红线。
④ 数值程序算你别生成：MA/止损/红线程序回填，你只判断/选票/给入场方式类型。
⑤ 只用当前时点可得数据(≤今日盘中)，禁编造，禁用未来信息。
⑥ 跟涨/补涨角色最高只给观察档；龙头/中军/策略强票为主线。
先用 list_candidates 看池子，挑最有希望的十几只用 get_metrics/get_kline/get_intraday/get_sector_context 逐个研究，再定 5-8 只。"""

USER_PROMPT = f"""今天是 {TODAY}（下午盘前），请做今日下午的日内选股，明早午盘前卖出，目标绝对收益为正。
请从候选池中经充分研究后选出 5-8 只。研究流程建议：先 list_candidates 看全池，锁定十几只有希望的（优先数据面综合分高+消息面看多+来源可靠的），逐只用 get_metrics 看量化画像、必要时 get_kline 看形态、get_intraday 看盘中、get_sector_context 看板块角色与催化，最后综合定档。

完成研究后，请在最后一条消息里输出一个 ```json 代码块（不要再调用工具），格式为一个列表，每个元素：
{{
  "code": "股票代码",
  "档": "推荐/观察/剔除",
  "建议分": 0到10的数字,
  "入场方式": "从 回踩MA5/回踩MA20/回踩前低/突破确认/缩量企稳 里选一个(绝不写价位数字)",
  "理由": "点明策略依据+板块依据",
  "风险": "主要风险",
  "卖出计划": "明早午盘前卖出的目标/触发条件"
}}
只输出你决定纳入结果的票（推荐/观察档为主，命中硬纪律的可标剔除档说明原因）。记住：绝不写任何价位数字，价位由程序回填。"""


# ---------------------------------------------------------------------------
# 5. Agent loop（function-calling，失败退 ReAct，再失败退规则版）
# ---------------------------------------------------------------------------
def _summarize_result(name, result):
    """把工具返回压成一句摘要，便于 trace 记录。"""
    try:
        if name == "list_candidates":
            return f"返回{result.get('候选数')}只候选台账"
        if name == "get_metrics":
            return (
                f"{result.get('name')} 现价{result.get('现价')} "
                f"涨{result.get('当日涨跌%')}% 均线多头={result.get('均线多头')} "
                f"pos60={result.get('pos60')} 量比={result.get('量比')} "
                f"涨停不可买={result.get('涨停不可买')} ATR%={result.get('ATR%')}"
            )
        if name == "get_kline":
            return f"返回{result.get('n')}日K线"
        if name == "get_intraday":
            return f"现价{result.get('price')} 涨{result.get('pct_chg')}% 量比{result.get('vol_ratio')}"
        if name == "get_sector_context":
            return (
                f"板块={result.get('板块')} 角色={result.get('角色')} "
                f"冷热={result.get('冷热')} 净催化={result.get('新闻净催化')}"
            )
    except Exception:
        pass
    return json.dumps(result, ensure_ascii=False)[:120]


def run_agent(client):
    """function-calling agent loop。返回 (final_text, trace_events, usage_total)。"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_PROMPT},
    ]
    trace = []
    usage_total = {"prompt": 0, "completion": 0}
    final_text = None

    for it in range(1, MAX_ITERS + 1):
        resp = client.chat.completions.create(
            model=MODEL_ID,
            messages=messages,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",
            temperature=0,
        )
        u = resp.usage
        if u:
            usage_total["prompt"] += u.prompt_tokens or 0
            usage_total["completion"] += u.completion_tokens or 0
        msg = resp.choices[0].message
        think = (msg.content or "").strip()
        tool_calls = msg.tool_calls or []

        ev = {"iter": it, "think": think, "calls": []}

        # 追加 assistant 消息（含 tool_calls）
        assistant_msg = {"role": "assistant", "content": msg.content or ""}
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ]
        messages.append(assistant_msg)

        if not tool_calls:
            final_text = think
            trace.append(ev)
            print(f"[iter {it}] 无工具调用，判定为最终答案", file=sys.stderr)
            break

        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            fn = TOOL_IMPL.get(name)
            if fn is None:
                result = {"error": f"未知工具{name}"}
            else:
                try:
                    result = fn(**args)
                except Exception as exc:
                    result = {"error": f"工具执行异常:{exc}"}
            summary = _summarize_result(name, result)
            ev["calls"].append({"name": name, "args": args, "summary": summary})
            print(f"[iter {it}] 调用 {name}({args}) -> {summary}", file=sys.stderr)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
        trace.append(ev)

    return final_text, trace, usage_total


# ---------------------------------------------------------------------------
# 6. 解析模型最终 JSON
# ---------------------------------------------------------------------------
def parse_final_picks(text):
    if not text:
        return None
    import re

    m = re.search(r"```json\s*(.*?)```", text, re.S)
    blob = m.group(1) if m else None
    if blob is None:
        # 尝试直接找列表
        m2 = re.search(r"(\[\s*\{.*\}\s*\])", text, re.S)
        blob = m2.group(1) if m2 else None
    if blob is None:
        return None
    try:
        data = json.loads(blob)
        if isinstance(data, dict):
            data = [data]
        return data
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 7. 价位程序回填 + 单调性夹逼 + 硬 veto
# ---------------------------------------------------------------------------
def backfill_prices(form, met):
    """按入场方式映射回填 挂单/止损/红线，再单调性夹逼。数据不足返回 None。"""
    price = met.get("现价")
    ma5 = met.get("MA5")
    ma20 = met.get("MA20")
    lo20 = met.get("近20日最低lo20")

    need = {
        "回踩MA5": [price, ma5, ma20],
        "回踩MA20": [price, ma20, lo20],
        "回踩前低": [price, lo20],
        "突破确认": [price, ma20],
        "缩量企稳": [price, ma20],
    }
    if form not in need or any(v is None for v in need[form]):
        return {"挂单价": None, "止损价": None, "不追高上限红线": None, "备注": "数据不足·人工确认"}

    if form == "回踩MA5":
        entry, stop, red = ma5, ma20, price
    elif form == "回踩MA20":
        entry, stop, red = ma20, lo20, price
    elif form == "回踩前低":
        entry, stop, red = lo20, lo20 * 0.98, price
    elif form == "突破确认":
        entry, stop, red = price, ma20, price * 1.02
    else:  # 缩量企稳
        entry, stop, red = price, ma20, price * 1.02

    # 单调性夹逼
    if form in ("回踩MA5", "回踩MA20", "回踩前低"):
        entry = min(entry, price)  # 回踩类挂单不高于现价
    if stop >= entry:
        stop = entry * 0.98
    red = max(red, entry)

    return {
        "挂单价": _round(entry),
        "止损价": _round(stop),
        "不追高上限红线": _round(red),
        "备注": "",
    }


def hard_veto(met):
    """涨停不可买 / pos60>=0.95贴高 / 获利盘>=0.95 → 剔除。返回(是否veto, 原因)。"""
    reasons = []
    if met.get("涨停不可买") is True:
        reasons.append("涨停不可买")
    pos60 = met.get("pos60")
    dist60 = met.get("距60高%")
    if pos60 is not None and pos60 >= 0.95 and (dist60 is not None and dist60 <= 2):
        reasons.append(f"pos60={pos60}贴60日高(距高{dist60}%)极高位抛压")
    prof = met.get("获利盘")
    if prof is not None and prof >= 0.95:
        reasons.append(f"获利盘{prof}>=0.95高抛压")
    return (len(reasons) > 0, "；".join(reasons))


# ---------------------------------------------------------------------------
# 8. 主流程
# ---------------------------------------------------------------------------
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    base = os.environ.get("LLM_BASE_URL")
    key = os.environ.get("LLM_API_KEY")
    mechanism = "function-calling"
    final_text, trace, usage = None, [], {"prompt": 0, "completion": 0}

    if not base or not key:
        print("[warn] 无 LLM 环境变量，降级规则版", file=sys.stderr)
        mechanism = "降级-规则版(无网关env)"
    else:
        from openai import OpenAI

        client = OpenAI(base_url=base, api_key=key)
        # function-calling，重试2次
        last_exc = None
        for attempt in range(3):
            try:
                final_text, trace, usage = run_agent(client)
                if final_text is not None or trace:
                    break
            except Exception as exc:
                last_exc = exc
                print(f"[warn] function-calling 第{attempt+1}次失败:{exc}", file=sys.stderr)
                time.sleep(2)
        else:
            print(f"[warn] function-calling 三次失败:{last_exc}", file=sys.stderr)
            mechanism = "降级-规则版(function-calling失败)"

    picks_raw = parse_final_picks(final_text) if final_text else None

    # 降级规则版：LLM 不可用或没解析出结果
    if not picks_raw:
        if mechanism.startswith("降级"):
            picks_raw = rule_based_picks()
        else:
            mechanism += "+最终JSON解析失败,退规则版补选"
            picks_raw = rule_based_picks()

    # 价位回填 + veto + 信号
    results = []
    for p in picks_raw:
        code = str(p.get("code", "")).strip()
        if not code:
            continue
        met = compute_metrics(code)
        sec = compute_sector_context(code)
        form = p.get("入场方式") or "缩量企稳"
        prices = backfill_prices(form, met)
        vetoed, veto_reason = hard_veto(met)

        grade = p.get("档", "观察")
        score = p.get("建议分", 5)
        if vetoed:
            grade = "剔除"
            score = min(float(score) if isinstance(score, (int, float)) else 2, 2)

        signals = {
            "量比": met.get("量比"),
            "距60高%": met.get("距60高%"),
            "距20高%": met.get("距20高%"),
            "获利盘": met.get("获利盘"),
            "pos60": met.get("pos60"),
            "均线多头": met.get("均线多头"),
            "涨停不可买": met.get("涨停不可买"),
            "ATR%": met.get("ATR%"),
        }
        results.append(
            {
                "code": code,
                "name": met.get("name"),
                "档": grade,
                "建议分": score,
                "入场方式": form,
                "挂单价": prices["挂单价"],
                "止损价": prices["止损价"],
                "不追高上限红线": prices["不追高上限红线"],
                "价位备注": prices["备注"],
                "理由": p.get("理由", ""),
                "风险": p.get("风险", ""),
                "卖出计划": p.get("卖出计划", ""),
                "veto": vetoed,
                "veto原因": veto_reason,
                "信号": signals,
                "板块": sec.get("板块"),
                "角色": sec.get("角色"),
                "现价": met.get("现价"),
                "MA5": met.get("MA5"),
                "MA20": met.get("MA20"),
            }
        )

    # 写 3 个产物
    write_outputs(results, trace, usage, mechanism, final_text)

    # 控制台回报
    n_calls = sum(len(e["calls"]) for e in trace)
    print("\n===== 回报 =====")
    print(f"机制: {mechanism}")
    print(f"工具调用总次数: {n_calls}  轮数: {len(trace)}")
    print(f"token: prompt={usage['prompt']} completion={usage['completion']} 总={usage['prompt']+usage['completion']}")
    print("最终选票:")
    for r in results:
        print(
            f"  {r['code']} {r['name']} | {r['档']} 分{r['建议分']} | "
            f"{r['入场方式']} 挂单{r['挂单价']} 止损{r['止损价']} 红线{r['不追高上限红线']}"
            + (f" | VETO:{r['veto原因']}" if r["veto"] else "")
        )
    return results, trace, usage, mechanism


def rule_based_picks():
    """LLM 不可用时的规则版兜底：数据面综合分高 + 消息面看多 + 未 veto，选前8。"""
    scored = []
    for t in LEDGER:
        code = t["code"]
        met = compute_metrics(code)
        vetoed, _ = hard_veto(met)
        if vetoed:
            continue
        base = t.get("数据面综合分", 0) or 0
        news = t.get("消息面分", 0) or 0
        bull = 0.05 if t.get("消息面方向") == "看多" else 0
        ma_bonus = 0.05 if met.get("均线多头") else 0
        s = base + news * 0.3 + bull + ma_bonus
        # 入场方式规则：均线多头且贴MA5→回踩MA5；否则回踩MA20；量比高→突破确认
        if met.get("均线多头"):
            form = "回踩MA5"
        elif met.get("量比") and met["量比"] > 1.5:
            form = "突破确认"
        else:
            form = "回踩MA20"
        grade = "观察"
        scored.append(
            (
                s,
                {
                    "code": code,
                    "档": grade,
                    "建议分": round(min(s * 8, 7.5), 1),
                    "入场方式": form,
                    "理由": f"[规则版]数据面{base}+消息面{t.get('消息面方向')}{news};{t.get('理由','')}",
                    "风险": "规则版无LLM研判,仅按分数与均线形态",
                    "卖出计划": "明早午盘前择机卖出,破止损即走",
                },
            )
        )
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:8]]


# ---------------------------------------------------------------------------
# 9. 产物写出
# ---------------------------------------------------------------------------
def write_outputs(results, trace, usage, mechanism, final_text):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_calls = sum(len(e["calls"]) for e in trace)

    # 9.1 JSON
    j = {
        "date": TODAY,
        "slot": "afternoon_intraday",
        "生成时间": ts,
        "机制": mechanism,
        "模型": MODEL_ID,
        "工具调用总次数": n_calls,
        "token用量": {**usage, "总": usage["prompt"] + usage["completion"]},
        "免责": "研究模拟,非投资建议;不真交易",
        "选股": results,
    }
    with open(os.path.join(OUT_DIR, f"deepseek_agent_选股_{TODAY}.json"), "w", encoding="utf-8") as fh:
        json.dump(j, fh, ensure_ascii=False, indent=2)

    # 9.2 trace markdown
    lines = []
    lines.append(f"# DeepSeek-Agent 化选股 过程留痕 · {TODAY}")
    lines.append("")
    lines.append(f"- 生成时间：{ts}")
    lines.append(f"- 机制：**{mechanism}**（对照实验：Agent 版 vs 填表版）")
    lines.append(f"- 模型：`{MODEL_ID}`")
    lines.append(f"- 交互轮数：{len(trace)}　工具调用总次数：**{n_calls}**")
    lines.append(f"- token：prompt={usage['prompt']}　completion={usage['completion']}　总={usage['prompt']+usage['completion']}")
    lines.append("- 研究模拟，非投资建议。")
    lines.append("")
    # 工具使用统计
    from collections import Counter

    cnt = Counter()
    codes_studied = set()
    for e in trace:
        for c in e["calls"]:
            cnt[c["name"]] += 1
            if "code" in c["args"]:
                codes_studied.add(str(c["args"]["code"]))
    lines.append("## 工具调用统计（模型自主决定）")
    lines.append("")
    lines.append("| 工具 | 调用次数 |")
    lines.append("|---|---|")
    for name, n in cnt.most_common():
        lines.append(f"| `{name}` | {n} |")
    lines.append("")
    lines.append(f"- 模型自主研究了 **{len(codes_studied)}** 只个股：{', '.join(sorted(codes_studied))}")
    lines.append("")
    lines.append("## 与「填表版（单次固定 payload）」的对比")
    lines.append("")
    lines.append("| 维度 | 填表版 | 本次 Agent 版 |")
    lines.append("|---|---|---|")
    lines.append("| 取数方式 | 一次性把固定字段塞进 prompt | 模型自主决定调哪个工具/调几次/先看谁 |")
    lines.append(f"| 取数轮次 | 1 次 | {len(trace)} 轮、{n_calls} 次工具调用 |")
    lines.append(f"| 研究广度 | 全池同一套字段 | 主动挑 {len(codes_studied)} 只逐个下钻(metrics/kline/盘中/板块) |")
    lines.append("| 决策链 | 一步到位 | 多轮推理,看完再决定下一步看谁 |")
    lines.append("")
    lines.append("## 逐轮留痕")
    lines.append("")
    for e in trace:
        lines.append(f"### 第 {e['iter']} 轮")
        if e.get("think"):
            lines.append("")
            lines.append("**模型思考：**")
            lines.append("")
            lines.append("> " + e["think"].replace("\n", "\n> "))
        if e["calls"]:
            lines.append("")
            lines.append("**工具调用：**")
            lines.append("")
            for c in e["calls"]:
                lines.append(f"- `{c['name']}({json.dumps(c['args'], ensure_ascii=False)})` → {c['summary']}")
        else:
            lines.append("")
            lines.append("_（无工具调用，此轮为最终答案）_")
        lines.append("")
    lines.append("## 模型最终输出原文")
    lines.append("")
    lines.append("```")
    lines.append((final_text or "(无)")[:8000])
    lines.append("```")
    with open(os.path.join(OUT_DIR, f"deepseek_agent_trace_{TODAY}.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    # 9.3 操作卡
    cards = []
    cards.append(f"# DeepSeek-Agent 选股 操作卡 · {TODAY} 下午日内")
    cards.append("")
    cards.append(f"> 机制：{mechanism}｜研究模拟非投资建议｜明早午盘前卖出")
    cards.append("")
    order = {"推荐": 0, "观察": 1, "剔除": 2}
    for r in sorted(results, key=lambda x: (order.get(x["档"], 9), -float(x.get("建议分", 0) or 0))):
        s = r["信号"]
        cards.append(f"## {r['name']} {r['code']} ｜ {r['档']} 建议分 {r['建议分']}")
        cards.append("")
        if r["veto"]:
            cards.append(f"- ⛔ 硬纪律剔除：{r['veto原因']}")
        buy = r["挂单价"]
        red = r["不追高上限红线"]
        if buy is not None and red is not None:
            cards.append(f"- 买点：**{buy} – {red} 元** 挂限价买（{r['入场方式']}，不追高）")
        else:
            cards.append(f"- 买点：数据不足·人工确认（{r['入场方式']}）")
        if r["止损价"] is not None:
            cards.append(f"- 止损：跌破 **{r['止损价']} 元** 走")
        if red is not None:
            cards.append(f"- 红线：高于 **{red} 元** = 追高别买")
        cards.append(f"- 卖出：明早午盘前卖出。{r['卖出计划']}")
        cards.append(
            f"- 信号：量比{s['量比']}｜距60高{s['距60高%']}%｜pos60={s['pos60']}"
            f"｜获利盘{s['获利盘']}(pos60代理)｜均线多头={s['均线多头']}"
        )
        cards.append(f"- 理由：{r['理由']}")
        if r["风险"]:
            cards.append(f"- 风险：{r['风险']}")
        cards.append("")
    with open(os.path.join(OUT_DIR, f"deepseek_agent_操作卡_{TODAY}.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(cards))

    print(f"[out] 已写 3 产物到 {OUT_DIR}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
