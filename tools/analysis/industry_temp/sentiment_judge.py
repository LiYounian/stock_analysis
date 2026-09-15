"""P-B:LLM 行业情绪判官(Marks 表15-1 情绪面4维)——**live 信号 · 非历史 validated**。

⚠️ feasibility 现实(诚实):全A PIT 行业文本仅从 2026-08-06 起(~6周),远低于 120 天 walk-forward
底线 → **无法历史 validate**。本模块只做 **going-forward live 信号 + 小样本人工核验 pilot**;
输出接 forward-shadow(纯记录、不 gate 任何决策),攒够 ≥120 独立样本才谈 validate。

防偷看6条(硬):①只喂**截止 date(≤T)**的文本;②**固定提示词**(本模块常量,不随结果调);
③小样本**人工核验**(run_shadow 落文本+判定供审);④**只输出 A/B**(中性=弃权,**不打分**);
⑤真 LLM 走 **zsh -ic 网关**(tools/llm/client);⑥分支不合 main。

4维→A/B(A=过热侧, B=过冷侧, 中性/无文本=None 弃权):
  投资者情绪 乐观A/悲观B ｜ 经济展望 正面A/负面B ｜ 流行风格 激进A/审慎B ｜ 景气 生机A/停滞B
"""
from __future__ import annotations

import glob
import json
import logging
import os
from pathlib import Path
from typing import Optional

from tools.analysis import industry_map

logger = logging.getLogger("industry_temp.sentiment_judge")

_MAIN = Path("/Users/yqg/Documents/projects/stock_analysis")
WINDOW_DAYS = 7                      # 情绪回看窗(交易日近似:自然日)

DIMS = ("投资者情绪", "经济展望", "流行风格", "景气")

# —— 固定提示词(防偷看②:不随结果调;改版=显式版本号)——
PROMPT_VERSION = "v1-2026-09-15"
SENTIMENT_SCHEMA = {
    "投资者情绪": "乐观 | 悲观 | 中性",
    "经济展望": "正面 | 负面 | 中性",
    "流行风格": "激进 | 审慎 | 中性",
    "景气": "生机 | 停滞 | 中性",
    "依据": "一句话依据(≤30字)",
}
SENTIMENT_INSTRUCTION = (
    "你是行业市场情绪分析员。**只依据下面给定的、截至评估日的新闻/政策文本**,判断该行业"
    "当前的市场**情绪面**(不是基本面数值)。四个维度各**二选一**;文本不足以判断就填『中性』,"
    "**禁止编造、禁止用你已知的评估日之后的信息**。**不要打分**,只做选择。"
)

# —— A/B 映射(A=过热侧, B=过冷侧)——
_AB = {
    "乐观": "A", "悲观": "B", "正面": "A", "负面": "B",
    "激进": "A", "审慎": "B", "生机": "A", "停滞": "B",
    "中性": None, "": None,
}


def to_ab(label: Optional[str]) -> Optional[str]:
    return _AB.get((label or "").strip(), None)


# ————————————————————— PIT 文本采集(只 ≤date) —————————————————————
def gather_pit_policy_text(industry: str, date: str, *, window_days: int = WINDOW_DAYS,
                           data_root: Optional[str] = None) -> list[str]:
    """截至 date(含)、近 window_days 自然日内,tag 能映射到该申万一级行业的 policy 文本。

    防未来:只读 date_dir ≤ date 的 policy_<date>.json;只取 item['date'] ≤ date 的条目。
    """
    from datetime import datetime, timedelta
    root = Path(data_root) if data_root else _MAIN / "data"
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=window_days)).strftime("%Y-%m-%d")
    texts = []
    for dd in sorted(glob.glob(str(root / "raw" / "*" / "policy" / "policy_*.json"))):
        day = os.path.basename(dd)[len("policy_"):-len(".json")]
        if not (start <= day <= date):        # 只 ≤date 的落盘日
            continue
        try:
            items = json.load(open(dd, encoding="utf-8"))
        except Exception:
            continue
        for it in (items if isinstance(items, list) else []):
            if str(it.get("date", ""))[:10] > date:      # 防未来:条目自身日期 ≤date
                continue
            tags = it.get("industries") or []
            if any(industry_map.to_sw(t) == industry for t in tags):
                title = (it.get("title") or "").strip()
                summ = (it.get("summary") or "").strip()
                if title or summ:
                    texts.append(f"【{it.get('date')}】{title}。{summ}")
    return texts


def _aggregate(dim_ab: dict) -> Optional[str]:
    """4维 A/B → 综合情绪:多数 A/B;平局或全弃权→None。"""
    vals = [v for v in dim_ab.values() if v in ("A", "B")]
    if not vals:
        return None
    a, b = vals.count("A"), vals.count("B")
    if a == b:
        return None
    return "A" if a > b else "B"


# ————————————————————— 判官 —————————————————————
def judge(industry: str, date: str, *, client=None, window_days: int = WINDOW_DAYS,
          data_root: Optional[str] = None, text: Optional[list[str]] = None) -> dict:
    """判某行业在 date 的情绪面 4 维 A/B + 综合。无文本→全弃权(不臆造)。

    防偷看:text 只来自 gather_pit_policy_text(≤date);client 缺省 get_client('extract')。
    """
    if text is None:
        text = gather_pit_policy_text(industry, date, window_days=window_days, data_root=data_root)
    if not text:
        return {"industry": industry, "date": date, "n_text": 0, "abstain": True,
                "维度": {d: None for d in DIMS}, "综合": None, "prompt_version": PROMPT_VERSION}
    if client is None:
        from tools.llm.client import get_client
        client = get_client("extract")
    corpus = "\n".join(f"- {t}" for t in text[:40])     # 控 token,取近端≤40条
    body = f"评估日:{date}\n行业:{industry}\n可用文本(均≤评估日):\n{corpus}"
    try:
        raw = client.extract(body, SENTIMENT_SCHEMA, instruction=SENTIMENT_INSTRUCTION)
    except Exception as e:
        return {"industry": industry, "date": date, "n_text": len(text), "abstain": True,
                "error": str(e)[:80], "维度": {d: None for d in DIMS}, "综合": None,
                "prompt_version": PROMPT_VERSION}
    dim_ab = {d: to_ab(raw.get(d)) for d in DIMS}
    return {
        "industry": industry, "date": date, "n_text": len(text), "abstain": False,
        "维度": dim_ab, "综合": _aggregate(dim_ab),
        "依据": (raw.get("依据") or "")[:40],
        "raw_labels": {d: raw.get(d) for d in DIMS},
        "prompt_version": PROMPT_VERSION,
    }


# ————————————————————— forward-shadow(纯记录·不 gate) —————————————————————
def _all_industries(data_root: Optional[str] = None) -> list[str]:
    from tools.collectors import code_industry
    snap = code_industry.load()
    return sorted({industry_map.to_sw(r) for r in snap.values()
                   if r and industry_map.to_sw(r)})


def run_shadow(date: str, *, out_dir: str, data_root: Optional[str] = None,
               industries: Optional[list[str]] = None, client=None) -> dict:
    """判某日全申万一级行业情绪,**纯记录**到 out_dir/<date>.json(不 gate 任何决策)。

    forward-shadow 纪律:攒够 ≥120 独立样本才谈 validate;此前只作观察信号。落盘含输入文本条数、
    综合、各维、依据、prompt_version,供人工核验与未来验证。
    """
    inds = industries or _all_industries(data_root)
    results = []
    for ind in inds:
        results.append(judge(ind, date, client=client, data_root=data_root))
    payload = {
        "date": date, "prompt_version": PROMPT_VERSION,
        "非validated": True, "forward_shadow": True,
        "n_industries": len(inds),
        "n_judged": sum(1 for r in results if not r.get("abstain")),
        "results": results,
        "免责": "live pilot·非历史validated(~6周PIT文本);policy文本利好偏斜+tag覆盖偏科技,"
                "综合偏A属已知偏差;只作观察,勿gate决策,≥120独立样本再谈validate。",
    }
    out = Path(out_dir) / f"{date}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
    return payload


def read_sentiment_shadow(date: str, shadow_dir: str) -> dict:
    """读某日 shadow 情绪 → {industry: 综合A/B/None}。缺文件→{}。供板块层 情绪 slot 选读。"""
    p = Path(shadow_dir) / f"{date}.json"
    if not p.exists():
        return {}
    try:
        payload = json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}
    return {r["industry"]: r.get("综合") for r in payload.get("results", [])}


def _cli() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="P-B 情绪判官 forward-shadow(纯记录·非validated)")
    ap.add_argument("--date", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--industries", default=None, help="逗号分隔;缺省=全申万一级")
    args = ap.parse_args()
    inds = args.industries.split(",") if args.industries else None
    r = run_shadow(args.date, out_dir=args.out_dir, data_root=args.data_root, industries=inds)
    print(f"judged {r['n_judged']}/{r['n_industries']} 行业 @ {args.date} → {args.out_dir}/{args.date}.json")


if __name__ == "__main__":
    _cli()
