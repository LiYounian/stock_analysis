"""D2-2 · 金字塔选股 决策包组装（骨架 + 全浓缩块 + 全板块概览）。

产出"决策包"文本：喂给 LLM 合成层（Claude Code / DeepSeek / 千问）做受限调整，
或统筹直接读它产出金字塔选股。不含 LLM 调用，纯组装。

板块口径（用户 09-18 定）：**全部板块都分析，重点标 3~5 个，浓缩不遗漏**——
全 29 板块每行一个（冷热/拥挤/均涨/涨停/上涨占比），另深挖重点 3~5（含消息驱动）。
"""
from __future__ import annotations

from typing import Optional
import json
import os


def _load(root: Optional[str], as_of: str, fname: str):
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    p = os.path.join(root or base, "data", "analysis", as_of, fname)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def market_overview(as_of: str, root: Optional[str] = None, n_focus: int = 5) -> dict:
    """市场定调 + 全板块概览（全分析·重点 n_focus·浓缩不遗漏）。"""
    focus = _load(root, as_of, "sector_focus.json") or {}
    regime = _load(root, as_of, "sector_regime.json") or {}
    boards = regime.get("板块") or []
    # 全板块按截面动量分位降序（强主线在前）
    def mom(b):
        return b.get("动量_截面分位") or 0.0
    boards_sorted = sorted(boards, key=mom, reverse=True)
    avoid = set(focus.get("规避板块池") or [])
    key_pool = {(x.get("板块") if isinstance(x, dict) else x) for x in (focus.get("重点板块池") or [])}
    # 重点 n_focus：在重点池内、动量靠前、优先非过热（拥挤A 且 过热的降序靠后）
    ranked_key = [b for b in boards_sorted if b.get("板块") in key_pool and b.get("板块") not in avoid]
    重点 = ranked_key[:n_focus] if ranked_key else boards_sorted[:n_focus]
    return {
        "as_of": as_of,
        "风险偏好": focus.get("风险偏好"),
        "宏观情景": focus.get("宏观情景"),
        "宏观净方向": focus.get("宏观净方向"),
        "消息驱动": focus.get("消息驱动"),
        "规避板块池": focus.get("规避板块池"),
        "全板块": boards_sorted,
        "重点板块": [b.get("板块") for b in 重点],
    }


def render_boards(ov: dict) -> str:
    """全板块概览渲染：每行一个板块，浓缩不遗漏。"""
    lines = []
    key = set(ov.get("重点板块") or [])
    for b in ov.get("全板块") or []:
        name = b.get("板块")
        mark = "★" if name in key else "·"
        lines.append(
            f"{mark}{name}: {b.get('冷热标签')}/拥挤{b.get('拥挤档')} "
            f"均涨{b.get('板块均涨幅')}% 涨停{int(b.get('涨停数') or 0)} "
            f"上涨占比{round((b.get('上涨家数占比') or 0)*100)}% "
            f"动量截面{round((b.get('动量_截面分位') or 0),2)}"
        )
    return "\n".join(lines)


def build_package(as_of: str, root: Optional[str] = None, top_n: int = 15,
                  scan_kline: bool = True, n_focus: int = 5) -> dict:
    """决策包：市场定调 + 全板块概览 + 骨架 top_n（每票全部工具浓缩块）。"""
    from tools.pyramid import d2_compose as C
    from tools.pyramid import registry
    import tools.pyramid.tools  # noqa: F401

    skel = C.build_skeleton(as_of, root=root, scan_kline=scan_kline)
    ov = market_overview(as_of, root=root, n_focus=n_focus)

    # shared_pool 不逐票重扫（每次 scan_kline 要 14s）；用骨架已存的来源标签直接渲染。
    tool_names = ["price_volume", "gate", "sector_context",
                  "experience_rules", "fake_good_news", "entry_price"]
    tools = {t: registry.get(t) for t in tool_names}

    top = skel["排序"][:top_n]
    cards = []
    for row in top:
        labels = row.来源标签 or []
        blocks = [
            f"【shared_pool·①塔基】as_of={as_of}\n"
            f"命中来源: {'/'.join(labels) if labels else '无'}（{len(labels)}/4来源·≥2=交叉共识）"
        ]
        for t in tool_names:
            try:
                blocks.append(tools[t].run(as_of, row.code, root=root).to_prompt())
            except Exception as e:
                blocks.append(f"【{t}】ERR {e}")
        cards.append({
            "code": row.code,
            "骨架分": row.骨架分,
            "子分": row.子分,
            "来源标签": row.来源标签,
            "浓缩块": "\n".join(blocks),
        })
    return {
        "as_of": as_of,
        "市场定调": ov,
        "骨架": skel,
        "候选卡片": cards,
        "top_n": top_n,
    }


def render_package(pkg: dict) -> str:
    """决策包 → 文本（供 LLM prompt 或统筹阅读）。"""
    ov = pkg["市场定调"]
    out = []
    out.append(f"# 金字塔决策包 · as_of={pkg['as_of']}")
    out.append("\n## 市场定调")
    out.append(f"风险偏好={ov.get('风险偏好')} 宏观情景={ov.get('宏观情景')} 宏观净方向={ov.get('宏观净方向')}")
    out.append(f"重点板块(★)={'/'.join(ov.get('重点板块') or [])}　规避板块={ov.get('规避板块池')}")
    out.append("\n## 全板块概览(全分析·★重点·浓缩不遗漏)")
    out.append(render_boards(ov))
    skel = pkg["骨架"]
    out.append(f"\n## 骨架排序(池{skel['池规模']}·计分{skel['计分票数']}·否决{len(skel['排雷否决'])}·权重{skel['权重']})")
    out.append(f"\n## 候选 top{pkg['top_n']}(每票骨架分+子分+全工具浓缩块)")
    for c in pkg["候选卡片"]:
        out.append(f"\n### {c['code']} 骨架分={c['骨架分']} 子分={c['子分']} 来源={'/'.join(c['来源标签'])}")
        out.append(c["浓缩块"])
    return "\n".join(out)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--as-of", required=True)
    p.add_argument("--data-root", default=None)
    p.add_argument("--top-n", type=int, default=15)
    p.add_argument("--out", default=None)
    a = p.parse_args()
    pkg = build_package(a.as_of, root=a.data_root, top_n=a.top_n)
    txt = render_package(pkg)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(txt)
        print(f"决策包写入 {a.out}（{len(txt)} 字）")
    else:
        print(txt)
