#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D2-3 · 金字塔选股 三方产线之 DeepSeek / 千问 版(LLM 受限调整合成层)。

与 experimental/agent_select_*(function-calling 自主取数)不同:本脚本走 D2 设计的
**打分骨架 + 浓缩块 LLM 受限调整**机制——
  1. 程序先用 d2_package 组装"决策包"(市场定调 + 全板块概览 + 骨架 top-N·每票全工具浓缩块);
  2. 把决策包整体喂给 LLM,LLM **只做**受限调整:从池内 top-N 选 2~3 买 + 2~3 避、
     微调序位、否决并给理由;**绝不产任何价位/分数数字、不加池外票**;
  3. 价位一律用 entry_price 工具回填的数字,骨架分/子分用 d2_compose 的数字。

真调 LLM 必须走 zsh -ic(非交互 bash 无网关 env):
    zsh -ic 'cd <repo> && PYTHONPATH=. ~/.conda/envs/stock_analysis/bin/python \
        -m tools.experimental.d2_pyramid_llm_select \
        --provider deepseek_v4pro --as-of 2026-09-17 \
        --data-root <生产data父目录> --label DeepSeek \
        --out-md docs/每日分析/选股/2026-09-17_金字塔_DeepSeek.md'

研究模拟、非投资建议。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# 0. 中文名(从 config/code_name.json 查证,绝不猜)
# ---------------------------------------------------------------------------
def _repo_root() -> str:
    # tools/experimental/<this>.py → 上溯 2 层 = 仓库根
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_code_names() -> dict:
    p = os.path.join(_repo_root(), "config", "code_name.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 1. 决策包 + 每票结构化字段(骨架分/子分/entry_price/量价/板块)
# ---------------------------------------------------------------------------
def collect_package(as_of: str, root: Optional[str], top_n: int,
                    no_digest: bool = False) -> dict:
    """组装决策包 + 每票结构化字段(供回填,不靠解析文本)。

    no_digest(A/B 只读开关):渲染前清空每张 card 的新闻digest行,
    使决策包退回 pre-W3-A 无 digest 形态。**不改生产 d2_package**——
    render_package 的 out.extend(digest or []) 自然渲染空。
    """
    from tools.pyramid import d2_package as P
    from tools.pyramid import registry
    import tools.pyramid.tools  # noqa: F401

    pkg = P.build_package(as_of, root=root, top_n=top_n)
    if no_digest:
        for card in pkg.get("候选卡片") or []:
            card["新闻digest_lines"] = []
    pkg_text = P.render_package(pkg)

    facts = {}
    for card in pkg["候选卡片"]:
        code = card["code"]
        ep = registry.get("entry_price").run(as_of, code, root=root).fields
        pv = registry.get("price_volume").run(as_of, code, root=root).fields
        sc = registry.get("sector_context").run(as_of, code, root=root).fields
        facts[code] = {
            "骨架分": card["骨架分"],
            "子分": card["子分"],
            "来源标签": card["来源标签"],
            "entry_price": ep,
            "price_volume": pv,
            "sector_context": sc,
        }
    return {"pkg": pkg, "pkg_text": pkg_text, "facts": facts,
            "top_codes": [c["code"] for c in pkg["候选卡片"]]}


# ---------------------------------------------------------------------------
# 2. Prompt(§2 受限调整规则·描述性约束·不塞具体 case)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是金字塔选股流程的"浓缩块合成层"分析师。上游程序已完成两件事:
①独立全A召回池 → 五基石加权打分 → 得到"骨架排序"(数字全部由程序产出、可复现);
②对骨架 top-N 每票拼好了全部工具的浓缩块(量价/闸门/板块/经验/假利好/入场价位)。

你的职责是在骨架之上做**受限调整**,把骨架排序落成最终选股。严守边界:

能做:
- 在给定的骨架 top-N 池**之内**选出 2~3 只买入 + 2~3 只规避;
- 相对骨架序位做小幅升降(哑铃/降beta/回避高位与过热拥挤的思路);
- 否决某票并给理由(如浓缩块显示位置过高、板块过热拥挤、财报/假利好隐患、趋势未修复等);
- 为每只买入写"入场逻辑与纪律""退出条件",为每只规避写"验什么""触发纳入/放弃条件"。

不能做:
- 不得产出任何价位或分数数字(价位由 entry_price 程序回填、分数由打分程序产出);
- 不得把 top-N 池**之外**的票加进来(独立召回的边界);
- 每个买入/规避/否决的判断都必须**引用某工具浓缩块里的具体一行**作为证据,不做无证据主观加塞;
- 买入与规避名单不得重叠。

辩证核对(逐买入/规避都做·在池内选的前提下开深度):
- 对每个买入/规避,主动质疑决策包"支持它的那一行"是否被同票其它面的证据反驳
  (如量价强但新闻digest利空占多、板块热但个股位置过高、财报评级好但假利好/解禁有隐患),
  做"我的判断 vs 骨架序位"的出入核对,有分歧就写进 adjust_log,别默认服从骨架也别无据翻案;
- 消息面判断不能只看净情绪标量:结合本票消息面里的"新闻digest"辨真伪——看利好/利空条数结构
  与最强标题的来源,警惕高开走弱式假利好、利好兑现后回吐;来源存疑或样本极少时,
  明说依据不足、按不确定处理,不硬凑成利好;
- 区分 α(个股)与 β(大盘):个股逻辑走该票四面浓缩块;大盘方向/广度/情绪走"大盘定调"段的
  方向档与其"⚠️效力诚实标注",不得把大盘上行概率读成个股必涨、不得把"高概率"读成"能赚钱";
- 诚实不硬凑:池内够格的买入不足 2~3 只时,如实只给 N 只并说明其余为何不够格,
  不为凑满名额纳入证据不足的票。

诚实边界:本版宏观 regime 用板块净催化代理、财报排雷用假利好+经验代理(专用工具未建),
不得谎称已覆盖;经验库对多数票命中 0 条属正常,不得据此编造规则。"""


# ── A/B 只读对照:pre-W3-B 基线 SYSTEM_PROMPT(= 现状减去"辩证核对"段)──────────
# 仅供 --baseline-prompt 做效果验证 A/B 用;取自 git dec442f^(W3-B 增补辩证核对段之前),
# 一字不改地保留当时的闭卷骨架约束,不引用"新闻digest"。现有 SYSTEM_PROMPT 不受影响。
SYSTEM_PROMPT_BASELINE = """你是金字塔选股流程的"浓缩块合成层"分析师。上游程序已完成两件事:
①独立全A召回池 → 五基石加权打分 → 得到"骨架排序"(数字全部由程序产出、可复现);
②对骨架 top-N 每票拼好了全部工具的浓缩块(量价/闸门/板块/经验/假利好/入场价位)。

你的职责是在骨架之上做**受限调整**,把骨架排序落成最终选股。严守边界:

能做:
- 在给定的骨架 top-N 池**之内**选出 2~3 只买入 + 2~3 只规避;
- 相对骨架序位做小幅升降(哑铃/降beta/回避高位与过热拥挤的思路);
- 否决某票并给理由(如浓缩块显示位置过高、板块过热拥挤、财报/假利好隐患、趋势未修复等);
- 为每只买入写"入场逻辑与纪律""退出条件",为每只规避写"验什么""触发纳入/放弃条件"。

不能做:
- 不得产出任何价位或分数数字(价位由 entry_price 程序回填、分数由打分程序产出);
- 不得把 top-N 池**之外**的票加进来(独立召回的边界);
- 每个买入/规避/否决的判断都必须**引用某工具浓缩块里的具体一行**作为证据,不做无证据主观加塞;
- 买入与规避名单不得重叠。

诚实边界:本版宏观 regime 用板块净催化代理、财报排雷用假利好+经验代理(专用工具未建),
不得谎称已覆盖;经验库对多数票命中 0 条属正常,不得据此编造规则。"""


def build_user_prompt(pkg_text: str, top_codes: list) -> str:
    codes_line = "/".join(top_codes)
    return f"""下面是 as_of=2026-09-17 收盘口径的金字塔决策包(预测次日 2026-09-18)。
请据此做受限调整,最终给出 2~3 买入 + 2~3 规避。

可选票池(**只能从这里选,代码必须完全一致**):{codes_line}

==== 决策包开始 ====
{pkg_text}
==== 决策包结束 ====

完成后**只输出一个 ```json 代码块**(不要多余文字),格式:
{{
  "market_tone": "一段市场定调(广度/风格/板块冷热),可引用决策包里的数字,但不得自创新数字",
  "picks_buy": [
    {{"code":"六位代码","thesis":"入场逻辑(点明量价/板块/位置依据)","evidence":"引用的浓缩块关键行(原文片段)","discipline":"入场纪律(控仓/破位处理/不追高)","exit":"退出条件"}}
  ],
  "picks_avoid": [
    {{"code":"六位代码","bucket":"规避来源:板块/市场/国际局势/纪律 之一","reason":"规避理由(引用浓缩块)","evidence":"引用的浓缩块关键行","verify":"验什么:调模型/防踏空/验纪律","trigger":"触发纳入或彻底放弃的条件"}}
  ],
  "adjust_log": ["相对骨架序位做了哪几处调整及理由(可审计),如 骨架#X 因... 下调至规避 / 从低位维度提 X 为买入"]
}}

要求:picks_buy 2~3 只、picks_avoid 2~3 只、两名单不重叠、所有 code 均来自可选票池、全程不写价位/分数数字。"""


# ---------------------------------------------------------------------------
# 3. LLM 调用(直建 OpenAI client 以捕获 usage)
# ---------------------------------------------------------------------------
def call_llm(provider_id: str, system: str, user: str) -> tuple:
    """按注册表 provider 直连网关,返回 (text, usage, model_id)。"""
    from tools.config import model_registry as mr
    from openai import OpenAI

    spec = mr.load_registry().spec_for(provider_id)
    base = spec.resolve_base_url()
    key = spec.resolve_api_key()
    model = spec.model
    if not base or not key:
        raise RuntimeError(f"provider {provider_id} 网关 env 未配置(需 zsh -ic)")

    client = OpenAI(base_url=base, api_key=key, timeout=600.0, max_retries=2)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=0,
        max_tokens=4096,
        extra_body={"enable_thinking": False},
    )
    text = resp.choices[0].message.content or ""
    u = resp.usage
    usage = {"prompt": (u.prompt_tokens or 0) if u else 0,
             "completion": (u.completion_tokens or 0) if u else 0}
    return text, usage, model


def parse_llm_json(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    blob = m.group(1) if m else None
    if blob is None:
        s, e = text.find("{"), text.rfind("}")
        blob = text[s:e + 1] if (s != -1 and e != -1) else None
    if blob is None:
        return None
    try:
        return json.loads(blob)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 4. 校验 + 归一(码入池、买避不重叠、条数 2~3)
# ---------------------------------------------------------------------------
def sanitize(parsed: dict, top_codes: list) -> dict:
    top = set(top_codes)
    warns = []

    def clean(items, kind):
        out, seen = [], set()
        for it in items or []:
            code = str(it.get("code", "")).strip()
            if code not in top:
                warns.append(f"{kind} 剔除池外/无效码 {code!r}")
                continue
            if code in seen:
                continue
            seen.add(code)
            out.append({**it, "code": code})
        return out

    buy = clean(parsed.get("picks_buy"), "买入")
    avoid = clean(parsed.get("picks_avoid"), "规避")
    # 买避不重叠:重叠者保留在买入、从规避剔
    buy_codes = {b["code"] for b in buy}
    avoid2 = []
    for a in avoid:
        if a["code"] in buy_codes:
            warns.append(f"规避 {a['code']} 与买入重叠,已从规避剔除")
            continue
        avoid2.append(a)
    if not (2 <= len(buy) <= 3):
        warns.append(f"买入 {len(buy)} 只(期望 2~3)")
    if not (2 <= len(avoid2) <= 3):
        warns.append(f"规避 {len(avoid2)} 只(期望 2~3)")
    return {"buy": buy, "avoid": avoid2,
            "market_tone": parsed.get("market_tone", ""),
            "adjust_log": parsed.get("adjust_log", []), "warns": warns}


# ---------------------------------------------------------------------------
# 5. 渲染 markdown(照 ClaudeCode 版格式)
# ---------------------------------------------------------------------------
_SUBSCORE_KEYS = ["量价自证", "板块角色", "策略共识", "排雷", "宏观催化"]
_SUBSCORE_LABEL = {"量价自证": "量价", "板块角色": "板块角色", "策略共识": "策略共识",
                   "排雷": "排雷", "宏观催化": "宏观催化"}


def _fmt_subscores(sub: dict) -> str:
    return " + ".join(f"{_SUBSCORE_LABEL[k]}{round(sub.get(k, 0), 1)}"
                      for k in _SUBSCORE_KEYS)


def render_md(label: str, model_id: str, as_of: str, next_day: str,
              result: dict, facts: dict, names: dict,
              usage: dict, weights: dict, skel: dict, ov: dict) -> str:
    buy_codes = ",".join(b["code"] for b in result["buy"])
    avoid_codes = ",".join(a["code"] for a in result["avoid"])
    L = []
    L.append(f"<!-- PICKS_BUY: {buy_codes} -->")
    L.append(f"<!-- PICKS_AVOID: {avoid_codes} -->")
    L.append(f"# {as_of} 金字塔选股 · {label} 版（第一版真·金字塔）")
    L.append("")
    L.append(f"> as_of={as_of} 收盘 → 预测 {next_day} ｜ 性质：**金字塔流程产出"
             f"（独立全A召回 + 多基石打分骨架 + 浓缩块 LLM 合成）· 研究模拟 · 非投资建议**")
    L.append(f"> 流程：`shared_pool` 独立召回池 {skel.get('池规模')} 票 → `d2_compose` "
             f"五基石加权骨架分 + 排雷否决 → 骨架 top-N 浓缩块 → **{label}**"
             f"（`{model_id}`·LLM 合成层）受限调整。价位全程序 `entry_price` 回填、LLM 不产数字。")
    w = weights
    L.append(f"> 权重（临时·复盘调）：量价 {w.get('量价自证')} / 板块角色 {w.get('板块角色')} / "
             f"策略共识 {w.get('策略共识')} / 排雷 {w.get('排雷')} / 宏观催化 {w.get('宏观催化')}。")
    L.append("")

    # 市场定调
    L.append("## 市场定调")
    L.append(result.get("market_tone") or "（模型未产出市场定调）")
    L.append("")

    # 买入
    L.append(f"## 🟢 推荐买入（{len(result['buy'])} 只）")
    L.append("")
    for i, b in enumerate(result["buy"], 1):
        code = b["code"]
        f = facts.get(code, {})
        name = names.get(code, code)
        sub = f.get("子分", {})
        ep = f.get("entry_price", {})
        pv = f.get("price_volume", {})
        sc = f.get("sector_context", {})
        skl = f.get("骨架分")
        role = sc.get("角色") or "—"
        rank = sc.get("板块内排名")
        rank_s = f"·{rank}" if rank else ""
        L.append(f"### {i}. {name} {code} ｜ 骨架分 {skl}（{role}{rank_s}）")
        L.append(f"- **骨架分解**：{_fmt_subscores(sub)}"
                 f"｜pos60={pv.get('pos60')}（{pv.get('pos60档')}）·量比{pv.get('量比')}"
                 f"（{pv.get('量比档')}）·距60高{pv.get('距60高pct')}%。")
        entry = ep.get("挂单价")
        stop = ep.get("止损价")
        red = ep.get("不追高上限")
        L.append(f"- **买点**（entry_price 回填）：首入场/加仓回踩 **{entry}**（{ep.get('入场方式')}）"
                 f"／止损 **{stop}**（{ep.get('止损幅pct')}%）／红线不追高 **{red}**。")
        L.append(f"- **理由**：{b.get('thesis','')}")
        if b.get("evidence"):
            L.append(f"- **证据**（浓缩块）：{b['evidence']}")
        L.append(f"- **纪律**：{b.get('discipline','')}")
        if b.get("exit"):
            L.append(f"- **退出**：{b['exit']}")
        L.append("")

    # 规避
    L.append(f"## 🔴 建议规避（{len(result['avoid'])} 只 · 调模型/验纪律）")
    L.append("")
    L.append("| 票 | 骨架分 | 规避来源 | 规避理由 | 验什么 | 触发条件 |")
    L.append("|---|---|---|---|---|---|")
    for a in result["avoid"]:
        code = a["code"]
        name = names.get(code, code)
        skl = facts.get(code, {}).get("骨架分")
        L.append(f"| **{name} {code}** | {skl} | {a.get('bucket','')} | "
                 f"{a.get('reason','')} | {a.get('verify','')} | {a.get('trigger','')} |")
    L.append("")

    # 方法留痕
    L.append("## 方法留痕（可审计）")
    L.append(f"- 骨架排序表：池 {skel.get('池规模')} → 排雷否决 {len(skel.get('排雷否决', []))} → "
             f"计分 {skel.get('计分票数')}，取 top{len(facts)} 喂 LLM。")
    L.append("- LLM 受限调整：")
    for line in result.get("adjust_log", []):
        L.append(f"  - {line}")
    if result.get("warns"):
        L.append(f"- 归一告警：{'；'.join(result['warns'])}")
    L.append("- 诚实边界：宏观 regime(P3)、财报排雷(P2)专用工具未建，本版宏观走板块净催化代理、"
             "财报靠假利好+经验补；股票中文名取自 `config/code_name.json`（以代码为准）。")
    L.append("- 证据边界：LLM 全程未产价位/分数数字，价位来自 `entry_price`、分数来自 `d2_compose`。")
    L.append("")

    # 验证
    L.append("## 验证")
    L.append(f"{next_day} D+1 起 forward 记分（r_exit + α），与 ClaudeCode/另一模型 金字塔版三方对照 "
             "+ 四模式对照；≥10 交易日正向才固化为定时任务。")
    L.append("")
    L.append(f"> token 用量：prompt={usage['prompt']}　completion={usage['completion']}　"
             f"总={usage['prompt']+usage['completion']}（模型 `{model_id}`）。")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 6. 机读 json(供 D3)
# ---------------------------------------------------------------------------
def build_machine_json(label, model_id, as_of, next_day, result, facts, names, usage):
    def enrich(items, kind):
        out = []
        for it in items:
            code = it["code"]
            f = facts.get(code, {})
            row = {"code": code, "name": names.get(code, code),
                   "骨架分": f.get("骨架分"), "子分": f.get("子分"),
                   "来源标签": f.get("来源标签"), **it}
            if kind == "buy":
                row["价位"] = f.get("entry_price")
            out.append(row)
        return out
    return {
        "date": as_of, "next": next_day, "策略": "金字塔", "来源": label,
        "模型": model_id, "机制": "打分骨架+浓缩块LLM受限调整",
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "token用量": {**usage, "总": usage["prompt"] + usage["completion"]},
        "市场定调": result.get("market_tone"),
        "买入": enrich(result["buy"], "buy"),
        "规避": enrich(result["avoid"], "avoid"),
        "调整留痕": result.get("adjust_log"),
        "归一告警": result.get("warns"),
        "免责": "研究模拟,非投资建议;不真交易",
    }


# ---------------------------------------------------------------------------
# 7. 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True,
                    choices=["deepseek_v4pro", "qwen_max"])
    ap.add_argument("--label", required=True, help="产物标签,如 DeepSeek / 千问")
    ap.add_argument("--as-of", default="2026-09-17")
    ap.add_argument("--next-day", default="2026-09-18")
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--out-md", required=True)
    ap.add_argument("--out-json", default=None)
    ap.add_argument("--dump-raw", default=None, help="落 LLM 原始回复(排查用)")
    # ── A/B 只读对照开关(默认关=现状加厚 B臂;不改打分/召回/落盘逻辑)──
    ap.add_argument("--no-digest", action="store_true",
                    help="A/B:决策包不含新闻digest(退回 pre-W3-A 形态)")
    ap.add_argument("--baseline-prompt", action="store_true",
                    help="A/B:用 pre-W3-B 基线 SYSTEM_PROMPT(无辩证核对段)")
    a = ap.parse_args()

    print(f"[1/4] 组装决策包 as_of={a.as_of} top{a.top_n} "
          f"(no_digest={a.no_digest}) ...", file=sys.stderr)
    C = collect_package(a.as_of, a.data_root, a.top_n, no_digest=a.no_digest)
    pkg, facts, top_codes = C["pkg"], C["facts"], C["top_codes"]
    skel, ov = pkg["骨架"], pkg["市场定调"]
    weights = skel.get("权重", {})
    names = load_code_names()

    system = SYSTEM_PROMPT_BASELINE if a.baseline_prompt else SYSTEM_PROMPT
    print(f"[2/4] 调 {a.provider} 做受限调整 "
          f"(prompt={'baseline' if a.baseline_prompt else 'current'}) ...", file=sys.stderr)
    user = build_user_prompt(C["pkg_text"], top_codes)
    text, usage, model_id = call_llm(a.provider, system, user)
    if a.dump_raw:
        with open(a.dump_raw, "w", encoding="utf-8") as f:
            f.write(text)

    print("[3/4] 解析 + 归一 ...", file=sys.stderr)
    parsed = parse_llm_json(text)
    if not parsed:
        print("[ERR] LLM 未产出可解析 JSON,原文:\n" + text[:2000], file=sys.stderr)
        sys.exit(2)
    result = sanitize(parsed, top_codes)
    if result["warns"]:
        print("[warn] 归一告警:" + "；".join(result["warns"]), file=sys.stderr)

    print("[4/4] 渲染产物 ...", file=sys.stderr)
    md = render_md(a.label, model_id, a.as_of, a.next_day,
                   result, facts, names, usage, weights, skel, ov)
    os.makedirs(os.path.dirname(os.path.abspath(a.out_md)), exist_ok=True)
    with open(a.out_md, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"[out] md → {a.out_md}", file=sys.stderr)

    if a.out_json:
        j = build_machine_json(a.label, model_id, a.as_of, a.next_day,
                               result, facts, names, usage)
        os.makedirs(os.path.dirname(os.path.abspath(a.out_json)), exist_ok=True)
        with open(a.out_json, "w", encoding="utf-8") as f:
            json.dump(j, f, ensure_ascii=False, indent=2)
        print(f"[out] json → {a.out_json}", file=sys.stderr)

    # 控制台回报
    print("\n===== 回报 =====")
    print(f"来源: {a.label}  模型: {model_id}")
    print(f"买入: {[b['code'] for b in result['buy']]}")
    print(f"规避: {[x['code'] for x in result['avoid']]}")
    print(f"token: prompt={usage['prompt']} completion={usage['completion']} "
          f"总={usage['prompt']+usage['completion']}")


if __name__ == "__main__":
    main()
