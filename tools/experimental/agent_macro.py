#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外围宏观影响分析 Agent（US / 日本 / 香港 宏观金融新闻 → 结构化影响报告）。

把 DeepSeek / 千问3.8-max 当**真 Agent**：给它联网工具（web_search / web_fetch）
+ 一个交卷工具（submit_report），让模型**自主**决定搜什么、抓哪条、怎么归类，
最后按固定 schema 产出**结构化机读报告**。

评级**不产 raw 数字**：模型只能在【五档文字定义】里选一档（强利多/利多/中性/
利空/强利空），与 tools/analysis/selection_synth.py 的「建议分档位定义先定档」同一原则。

复用 tools/experimental/agent_select_{ds,qwen}.py 的 function-calling 脚手架思路：
  - OpenAI 兼容 client（走 内部/公司网关，不烧 Bedrock）
  - TOOLS_SCHEMA function-calling loop，失败重试
  - trace / usage / mechanism 全程留痕

用法（真跑走 zsh -ic 才有网关 env）：
    python tools/experimental/agent_macro.py --provider ds     # deepseek-v4-pro
    python tools/experimental/agent_macro.py --provider qwen   # qwen3.8-max
可选：--max-iters N  --out-dir PATH  --dry-run(只打印prompt不调LLM)

⚠️ 研究模拟、非投资建议。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime

TODAY = datetime.now().strftime("%Y-%m-%d")

# ============================================================================
# 1. 收集范围 / 评级档定义 / 结构化 schema（文字优先，禁裸分）
# ============================================================================

# 收集范围清单（US / 日本 / 香港 + 跨境传导）——写进 system prompt，指导 agent 搜什么
SCOPE = {
    "US": ["美联储FOMC利率决议/点阵图/官员表态", "美债10Y/2Y收益率", "美元指数DXY",
           "美国CPI/PCE/非农就业", "对华关税/出口管制/实体清单"],
    "JP": ["日本央行BOJ利率决议/YCC", "日元USDJPY汇率/套息交易carry trade", "日经225"],
    "HK": ["香港金管局HKMA/联系汇率", "港股恒生/恒生科技", "南向资金/港股通", "IPO与流动性"],
    "CROSS": ["北向资金流向", "人民币在岸/离岸汇率", "中美利差", "全球风险偏好VIX"],
}

# 【五档评级定义】——事件对 A股 的影响方向与力度；模型据此**先定档**，禁止裸给数字
RATING_TIERS = """【影响评级五档定义（对 A股 大盘/风险偏好的方向与力度·你必须据此先定档·绝不许输出数字分）】
· 强利多：重大宽松/流动性显著改善/明确利好A股主线（如超预期降息、重大稳增长、外资大幅回流），方向明确且力度大，历史类比普遍上涨。
· 利多：边际偏正面，利好部分板块或小幅改善风险偏好，但力度有限或有对冲项。
· 中性：方向不明或多空基本对冲，对A股整体无显著净影响（可能仅局部结构性影响）。
· 利空：边际偏负面，压制估值/资金面/风险偏好，但非系统性（可结构性规避）。
· 强利空：重大紧缩/流动性冲击/系统性风险（如超预期加息+鹰派、套息逆转、关税急升），方向明确力度大，普遍下跌压力。"""

# 【证据充分度档】——同样档位化，不产数字
CONFIDENCE_TIERS = "【证据充分度】高（多源一致、有一手数据/官方口径）/中（有来源但单一或口径有差异）/低（传闻或推断为主）。"

# 结构化 schema（机读）——影响说明=自由文字；评级=五档之一
REPORT_SCHEMA = {
    "as_of": "YYYY-MM-DD（报告日）",
    "region_scope": "list，实际覆盖到的地区，如 [US, JP, HK, CROSS]",
    "events": ("list，每个宏观事件一个 dict：{"
               "id: 短id, date: 事件日期, region: US|JP|HK|EU|CN|GLOBAL, "
               "category: 货币政策|利率债市|汇率|贸易关税|资金流|地缘|其他, "
               "headline: 一句话标题, "
               "summary_free: 影响说明【自由文字·不强求量化】, "
               "sources: list[{title,url,date}]【每条事件≥1个来源·禁编造URL】, "
               "transmission_free: 传导到A股的路径【自由文字】, "
               "affected_sectors: list[{sector, direction: 利多|利空|中性, note}], "
               "rating: 五档之一【强利多|利多|中性|利空|强利空·据档定义先定档·禁数字】, "
               "rating_reason: 为何给这一档（点明判据）, "
               "confidence: 高|中|低, "
               "half_life_days: 影响半衰期天数(int·不确定可null)}"),
    "aggregate": ("dict：{net_direction: 五档之一（全局净方向）, "
                  "regime_note: 自由文字·整体外围环境定性, "
                  "sector_watchlist: list[{sector, stance: 超配|标配|低配|回避, reason}]}"),
}


# ============================================================================
# 2. 联网工具后端（curl_cffi 伪装 chrome；与项目 collectors 同一取数风格）
# ============================================================================

def _creq():
    from curl_cffi import requests as creq
    return creq


def tool_web_search(query: str, region_hint: str = "", **_) -> dict:
    """百度资讯(tn=news) 搜索，返回标题+链接+摘要列表。宏观新闻覆盖好、免登录。"""
    creq = _creq()
    q = query if not region_hint else f"{region_hint} {query}"
    try:
        r = creq.get("https://www.baidu.com/s",
                     params={"wd": q, "tn": "news", "rtt": "1", "rn": "12"},
                     impersonate="chrome110", timeout=25)
        html = r.text
    except Exception as e:
        return {"query": q, "error": f"{type(e).__name__}: {str(e)[:120]}", "results": []}
    results = []
    # 资讯卡片：标题在 <h3> 里的 <a>；链接是百度跳转 url（够用，可再 fetch）
    for m in re.finditer(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        url = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if title:
            results.append({"title": title, "url": url})
        if len(results) >= 12:
            break
    return {"query": q, "count": len(results), "results": results}


def tool_web_fetch(url: str, max_chars: int = 2500, **_) -> dict:
    """抓取网页正文（去标签、压空白、截断）。给 agent 读全文核实细节。"""
    creq = _creq()
    try:
        r = creq.get(url, impersonate="chrome110", timeout=25)
        html = r.text
    except Exception as e:
        return {"url": url, "error": f"{type(e).__name__}: {str(e)[:120]}", "text": ""}
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return {"url": url, "text": text[:max_chars], "truncated": len(text) > max_chars}


TOOL_IMPL = {
    "web_search": tool_web_search,
    "web_fetch": tool_web_fetch,
}

TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "搜索宏观金融新闻（百度资讯）。返回标题+链接。用于发现 US/日本/香港 最新宏观事件。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索词，如 '美联储 9月 加息 点阵图'"},
            "region_hint": {"type": "string", "description": "可选地区提示，如 '日本央行' '香港金管局'"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "抓取某条新闻网页正文，核实数字/日期/措辞。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "submit_report",
        "description": "提交最终结构化宏观影响报告（JSON）。调用它即结束。",
        "parameters": {"type": "object", "properties": {
            "report": {"type": "object", "description": "严格符合 REPORT_SCHEMA 的对象"}},
            "required": ["report"]}}},
]


# ============================================================================
# 3. system prompt（收集范围 + 评级档 + schema + 硬约束）
# ============================================================================

def build_system_prompt() -> str:
    scope_lines = "\n".join(
        f"  · {k}: {', '.join(v)}" for k, v in SCOPE.items())
    schema_str = json.dumps(REPORT_SCHEMA, ensure_ascii=False, indent=1)
    return f"""你是**外围宏观影响分析 Agent**。今天是 {TODAY}。目标：自主联网收集
【美国 / 日本 / 香港】及跨境的宏观金融新闻，分析其对 **A股** 大盘与板块的影响，
产出**结构化机读报告**。

【必须覆盖的收集范围】
{scope_lines}

【工作流程】
1. 用 web_search 多轮搜索上述范围的**最新**事件（{TODAY} 前后），一个方向一次搜。
2. 对关键事件用 web_fetch 抓正文，核实**幅度/日期/官方措辞**，别只看标题。
3. 归纳成事件列表，每条判断对 A股 的传导与受影响板块。
4. 逐条按【五档定义先定档】给 rating；再给全局净方向与板块 watchlist。
5. 调用 submit_report 交卷（JSON）。

{RATING_TIERS}

{CONFIDENCE_TIERS}

【输出 schema（submit_report 的 report 字段必须符合）】
{schema_str}

【硬约束】
① 评级只能选五档之一，**先按档定义定档再写理由**，**绝不输出任何数字打分**。
② 影响说明(summary_free/transmission_free)可自由文字、不强求量化，但每条事件**至少1个真实来源**(web_search/web_fetch 得到的 url)，**禁止编造 URL 或数字**。
③ 只用你实际搜到/抓到的信息；查不到确切数字就给区间或写“最近可得+时点”，**绝不臆造未来数据**。
④ 你没有多轮追问机会，**绝不反问**；信息不全时自己再搜或在报告里标注假设。
⑤ 研究模拟、非投资建议；只做市场/板块层面分析，不做个人化投资建议。
⑥ 尽量在 {MAX_ITERS_DEFAULT} 轮工具调用内完成；覆盖到 US+日本+香港至少各1条事件即可交卷。"""


USER_KICKOFF = "请开始：先搜美联储最新利率决议，再覆盖日本央行、香港市场与北向/人民币，最后 submit_report 交卷。"

MAX_ITERS_DEFAULT = 16


# ============================================================================
# 4. Agent loop（function-calling）
# ============================================================================

def _summarize(name, result):
    if name == "web_search":
        return f"[web_search] q={result.get('query')!r} -> {result.get('count', 0)} 条"
    if name == "web_fetch":
        t = result.get("text", "")
        return f"[web_fetch] {result.get('url','')[:50]} -> {len(t)} 字"
    return f"[{name}]"


def _trim_history(messages, keep_tool_full=6):
    """控制 prompt 膨胀:只保留最近 keep_tool_full 条 tool 结果全文,更早的压成短桩。
    (web_fetch/web_search 正文用完即可丢——避免累积重发把 prompt 撑到几十万 token。)"""
    tool_idxs = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in tool_idxs[:-keep_tool_full] if len(tool_idxs) > keep_tool_full else []:
        if not messages[i].get("_stubbed"):
            messages[i]["content"] = "（早期工具结果已用于分析，正文省略以控制上下文）"
            messages[i]["_stubbed"] = True


def run_agent(client, model, max_iters, trace, timeout=200):
    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": USER_KICKOFF},
    ]
    usage = {"prompt": 0, "completion": 0}
    final_report = None
    n_tool_calls = 0
    nudged = False

    for it in range(1, max_iters + 1):
        remaining = max_iters - it
        # 收敛压力:过半还没交卷 → 逼它尽快 submit_report;最后一轮强制只能 submit。
        if remaining <= 0:
            tool_choice = {"type": "function", "function": {"name": "submit_report"}}
        else:
            tool_choice = "auto"
        if not nudged and it >= max(3, max_iters // 2) and final_report is None:
            messages.append({"role": "user",
                             "content": f"已收集不少信息（还剩约 {remaining} 轮预算）。US/日本/香港"
                                        "至少各1条即可，请在**下一轮直接调用 submit_report 交卷**，"
                                        "不要再无止境搜索。"})
            nudged = True
        _trim_history(messages)
        send_msgs = [{k: v for k, v in m.items() if k != "_stubbed"} for m in messages]
        resp = client.chat.completions.create(
            model=model, messages=send_msgs, tools=TOOLS_SCHEMA,
            tool_choice=tool_choice, temperature=0.3, timeout=timeout)
        if resp.usage:
            usage["prompt"] += resp.usage.prompt_tokens or 0
            usage["completion"] += resp.usage.completion_tokens or 0
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []

        assistant_msg = {"role": "assistant", "content": msg.content or ""}
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls]
        messages.append(assistant_msg)

        if not tool_calls:
            # 没调工具：可能直接给了文字结论——记下来，提醒它必须 submit_report
            trace.append({"iter": it, "kind": "text", "content": (msg.content or "")[:400]})
            messages.append({"role": "user",
                             "content": "请用 submit_report 工具提交结构化 JSON 报告（不要只输出文字）。"})
            continue

        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            n_tool_calls += 1

            if name == "submit_report":
                final_report = args.get("report")
                trace.append({"iter": it, "kind": "submit", "ok": final_report is not None})
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps({"received": True}, ensure_ascii=False)})
                if final_report is not None:
                    return final_report, usage, n_tool_calls
                continue

            impl = TOOL_IMPL.get(name)
            if impl is None:
                result = {"error": f"unknown tool {name}"}
            else:
                try:
                    result = impl(**args)
                except Exception as e:
                    result = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
            trace.append({"iter": it, "kind": "tool", "name": name,
                          "args": args, "summary": _summarize(name, result)})
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, ensure_ascii=False)[:3500]})

    return final_report, usage, n_tool_calls


# ============================================================================
# 5. main / provider 选择 / 输出留痕
# ============================================================================

PROVIDERS = {
    "ds": {"base": "LLM_BASE_URL", "key": "LLM_API_KEY",
           "model_env": "LLM_MODEL", "model_default": "deepseek-v4-pro", "tag": "deepseek"},
    "qwen": {"base": "QWEN_BASE_URL", "key": "QWEN_API_KEY",
             "model_env": "QWEN_MODEL", "model_default": "qwen3.8-max", "tag": "qwen"},
}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=list(PROVIDERS), default="ds")
    ap.add_argument("--max-iters", type=int, default=MAX_ITERS_DEFAULT)
    ap.add_argument("--timeout", type=int, default=200, help="每次 LLM 调用超时秒数")
    ap.add_argument("--out-dir", default=os.path.join("data", "analysis", TODAY))
    ap.add_argument("--dry-run", action="store_true", help="只打印 system prompt，不调 LLM")
    args = ap.parse_args(argv)

    if args.dry_run:
        print(build_system_prompt())
        return 0

    pcfg = PROVIDERS[args.provider]
    base = os.environ.get(pcfg["base"])
    key = os.environ.get(pcfg["key"])
    model = os.environ.get(pcfg["model_env"]) or pcfg["model_default"]
    if not base or not key:
        print(f"[fatal] 缺网关 env（{pcfg['base']}/{pcfg['key']}）——请用 zsh -ic 跑。", file=sys.stderr)
        return 2

    from openai import OpenAI
    client = OpenAI(base_url=base, api_key=key)

    trace = []
    t0 = time.time()
    report, usage, n_calls, mechanism = None, {"prompt": 0, "completion": 0}, 0, "function-calling"
    last_exc = None
    for attempt in range(2):
        try:
            report, usage, n_calls = run_agent(client, model, args.max_iters, trace, timeout=args.timeout)
            break
        except Exception as exc:
            last_exc = exc
            traceback.print_exc()
            print(f"[warn] 第{attempt+1}次失败：{exc}", file=sys.stderr)
            time.sleep(3)
    else:
        mechanism = "function-calling-failed"

    elapsed = time.time() - t0
    ok = report is not None
    meta = {"provider": args.provider, "model": model,
            "mechanism": mechanism if ok else f"{mechanism}(no report:{last_exc})",
            "elapsed_sec": round(elapsed, 1), "tool_calls": n_calls,
            "usage": usage, "iters_trace": len(trace), "as_of": TODAY}
    out = {"report": report, "meta": meta}

    os.makedirs(args.out_dir, exist_ok=True)
    tag = pcfg["tag"]
    jpath = os.path.join(args.out_dir, f"{tag}_agent_宏观_{TODAY}.json")
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    tpath = os.path.join(args.out_dir, f"{tag}_agent_宏观_trace_{TODAY}.md")
    with open(tpath, "w", encoding="utf-8") as fh:
        fh.write(f"# {tag} 宏观 agent trace {TODAY}\n\n")
        fh.write(f"model={model} mechanism={meta['mechanism']} elapsed={elapsed:.1f}s "
                 f"tool_calls={n_calls} usage={usage}\n\n")
        for ev in trace:
            fh.write(f"- iter{ev.get('iter')} [{ev.get('kind')}] "
                     f"{ev.get('summary') or ev.get('content','') or ev.get('name','')}\n")

    print(f"[done] provider={args.provider} model={model} ok={ok} "
          f"elapsed={elapsed:.1f}s tool_calls={n_calls} "
          f"usage(prompt/completion)={usage['prompt']}/{usage['completion']} "
          f"events={len(report.get('events',[])) if ok else 0}")
    print(f"[out] {jpath}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
