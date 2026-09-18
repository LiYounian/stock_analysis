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


def _txt(v) -> str:
    """A6/D4 铁律：任何值转成人读文本，禁止裸 dict/json 进 prompt。

    dict → `k=v k=v`（无花括号）；list/tuple → 顿号连接（空→"无"）；标量 → str。递归。
    """
    if v is None:
        return "无"
    if isinstance(v, dict):
        return " ".join(f"{k}={_txt(x)}" for k, x in v.items()) or "无"
    if isinstance(v, (list, tuple)):
        return "、".join(_txt(x) for x in v) if v else "无"
    return str(v)


def _scalar(v, key: Optional[str] = None):
    """从可能是 dict 的字段里取标量：优先取同名内层键，否则整体文字化。"""
    if isinstance(v, dict):
        if key and key in v and not isinstance(v[key], (dict, list)):
            return v[key]
        return _txt(v)
    return v


_排除冷热 = {"过热", "拐点"}  # A6：★ 排除 过热A / 拐点A


def market_overview(as_of: str, root: Optional[str] = None, n_focus: int = 5) -> dict:
    """市场定调 + 全板块概览（全分析·重点 n_focus·浓缩不遗漏）。

    A6：★重点板块改按 focus_score 降序挑，且排除"过热+拥挤A / 拐点+拥挤A"
    （按 regime 冷热标签/拥挤档判，与全板块渲染显示的标签一致）。
    """
    focus = _load(root, as_of, "sector_focus.json") or {}
    regime = _load(root, as_of, "sector_regime.json") or {}
    boards = regime.get("板块") or []
    # 全板块按截面动量分位降序（强主线在前）
    def mom(b):
        return b.get("动量_截面分位") or 0.0
    boards_sorted = sorted(boards, key=mom, reverse=True)
    # regime 标签映射（板块→冷热标签/拥挤档），★排除口径以它为准
    reg_label = {b.get("板块"): (b.get("冷热标签"), b.get("拥挤档")) for b in boards}
    avoid = {(x.get("板块") if isinstance(x, dict) else x)
             for x in (focus.get("规避板块池") or [])}

    def _过热拥挤(name: str) -> bool:
        冷热, 拥挤 = reg_label.get(name, (None, None))
        return 拥挤 == "A" and 冷热 in _排除冷热

    # A6：★ 从重点板块池按 focus_score 降序，排除 规避 与 过热A/拐点A
    key_entries = [x for x in (focus.get("重点板块池") or []) if isinstance(x, dict)]
    key_entries.sort(key=lambda x: (x.get("focus_score") or 0.0), reverse=True)
    重点 = [x.get("板块") for x in key_entries
            if x.get("板块") not in avoid and not _过热拥挤(x.get("板块"))][:n_focus]
    if not 重点:  # 兜底：无 focus 池时退回动量截面（同样排除 过热A/拐点A 与规避）
        重点 = [b.get("板块") for b in boards_sorted
                if b.get("板块") not in avoid and not _过热拥挤(b.get("板块"))][:n_focus]

    return {
        "as_of": as_of,
        # A6 文字化：风险偏好在 focus.json 里是 dict，取内层标量避免裸 dict 进 prompt
        "风险偏好": _scalar(focus.get("风险偏好"), "风险偏好"),
        "广度档": _scalar(focus.get("风险偏好"), "广度档") if isinstance(focus.get("风险偏好"), dict) else None,
        "宏观情景": _scalar(focus.get("宏观情景"), "宏观情景"),
        "宏观净方向": _scalar(focus.get("宏观净方向"), "宏观净方向"),
        "消息驱动": focus.get("消息驱动"),  # 原始保留（render 不直接印，需要时经 _txt）
        "规避板块池": [(x.get("板块") if isinstance(x, dict) else x)
                     for x in (focus.get("规避板块池") or [])],
        "全板块": boards_sorted,
        "重点板块": 重点,
        "重点口径": "focus_score↓·排除规避与过热A/拐点A",
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
                  "experience_rules", "fake_good_news", "financial_redflag",
                  "unlock_risk", "insider_reduction", "entry_price"]
    tools = {t: registry.get(t) for t in tool_names}

    top = C.select_top(skel["排序"], top_n)  # A10：同分不硬切，整桶纳入
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


def _sub_txt(subs) -> str:
    """子分 dict → 人读文本（A6：禁止裸 dict 进 prompt）。"""
    if not isinstance(subs, dict):
        return _txt(subs)
    return " ".join(f"{k} {v}" for k, v in subs.items())


def render_package(pkg: dict) -> str:
    """决策包 → 文本（供 LLM prompt 或统筹阅读）。A6：全程文字化，无裸 dict。"""
    ov = pkg.get("市场定调") or {}
    out = []
    out.append(f"# 金字塔决策包 · as_of={pkg.get('as_of')}")
    out.append("\n## 市场定调")
    广度 = ov.get("广度档")
    out.append(
        f"风险偏好={_txt(ov.get('风险偏好'))}"
        + (f"（广度档={_txt(广度)}）" if 广度 else "")
        + f" 宏观情景={_txt(ov.get('宏观情景'))} 宏观净方向={_txt(ov.get('宏观净方向'))}"
    )
    out.append(
        f"重点板块(★)={_txt(ov.get('重点板块'))}　规避板块={_txt(ov.get('规避板块池'))}"
        + (f"　[{ov.get('重点口径')}]" if ov.get("重点口径") else "")
    )
    out.append("\n## 全板块概览(全分析·★重点·浓缩不遗漏)")
    out.append(render_boards(ov))
    skel = pkg.get("骨架") or {}
    # A11：口径三分拆·自洽（参与 = 计分 + 数据缺；计分 = 排序 + 否决）
    池 = skel.get("池规模")
    参与 = skel.get("参与票数", 池)
    计分 = skel.get("计分票数", len(skel.get("排序") or []) + len(skel.get("排雷否决") or []))
    排序票 = skel.get("排序票数", len(skel.get("排序") or []))
    否决票 = skel.get("否决票数", len(skel.get("排雷否决") or []))
    缺 = skel.get("数据缺失票数", max(参与 - 计分, 0) if isinstance(参与, int) else 0)
    out.append(
        f"\n## 骨架排序(池{池}·参与{参与}=计分{计分}+数据缺{缺}；"
        f"计分{计分}=排序{排序票}+否决{否决票}·权重 {_txt(skel.get('权重'))})"
    )
    if skel.get("排序键"):
        out.append(f"排序键：{skel['排序键']}（同分不跨界硬切）")
    cards = pkg.get("候选卡片") or []
    out.append(f"\n## 候选 top{pkg.get('top_n')}(实入{len(cards)}张·每票骨架分+子分+全工具浓缩块)")
    for c in cards:
        out.append(
            f"\n### {c.get('code')} 骨架分={c.get('骨架分')} "
            f"子分[{_sub_txt(c.get('子分'))}] 来源={_txt(c.get('来源标签'))}"
        )
        out.append(c.get("浓缩块") or "")
    return "\n".join(out)


def save_package(pkg: dict, txt: Optional[str] = None, root: Optional[str] = None,
                 label: Optional[str] = None) -> str:
    """A12：决策包 md 落盘到 data/analysis/<as_of>/金字塔决策包_<label>.md，返回路径。"""
    if txt is None:
        txt = render_package(pkg)
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    as_of = pkg.get("as_of") or "unknown"
    d = os.path.join(root or base, "data", "analysis", as_of)
    os.makedirs(d, exist_ok=True)
    lab = label or f"top{pkg.get('top_n')}"
    p = os.path.join(d, f"金字塔决策包_{lab}.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(txt)
    return p


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--as-of", required=True)
    p.add_argument("--data-root", default=None)
    p.add_argument("--top-n", type=int, default=15)
    p.add_argument("--out", default=None, help="显式输出路径（不给则默认落 data/analysis/<as_of>/）")
    p.add_argument("--no-save", action="store_true", help="不落盘（仅打印）")
    a = p.parse_args()
    pkg = build_package(a.as_of, root=a.data_root, top_n=a.top_n)
    txt = render_package(pkg)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(txt)
        print(f"决策包写入 {a.out}（{len(txt)} 字）")
    elif a.no_save:
        print(txt)
    else:
        # A12：默认落盘过程文件（决策包 md）
        path = save_package(pkg, txt, root=a.data_root)
        print(f"决策包落盘 {path}（{len(txt)} 字）")
