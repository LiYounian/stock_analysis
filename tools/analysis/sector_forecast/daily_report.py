"""每日板块定向分析文档(产出 C)——§6 模板渲染 + 结构化落盘。

输入(全部已由前序模块产出,本模块只组装):板块面板(regime)+ 两步 focus + 角色主表(roster)
+ 当日 sentiment_policy(全局/板块新闻主线)。输出:
  data/analysis/<date>/sector_daily.md    人可读日报
  data/analysis/<date>/sector_daily.json  结构化(供审计/回填)

诚实边界照旧标注;新闻主线 P2 先用 sentiment_policy(选股链副产),独立新闻库就绪后替换为 D。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sector_forecast.daily_report")

REPORT_VERSION = "v1-2026-09-16"


def _load_roster(sw: str) -> Optional[dict]:
    from tools.config import settings
    from tools.backtest.iet_probe.data import _MAIN
    for base in (settings.PROJECT_ROOT, _MAIN):
        p = Path(base) / "data" / "sector_roster" / f"{sw}.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def _news_headlines(date: str, top: int = 5) -> list[dict]:
    """当日 sentiment_policy 里强度最高的几条(全局主线用)。"""
    from tools.analysis.sector_forecast.market_step import resolve_analysis_file
    p = resolve_analysis_file(date, "sentiment_policy.json")
    if not p:
        return []
    try:
        msgs = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    msgs = sorted(msgs, key=lambda m: -(m.get("影响强度") or 0))
    out = []
    for m in msgs[:top]:
        out.append({"title": m.get("title", "")[:50], "方向": m.get("影响方向"),
                    "强度": m.get("影响强度"), "行业": (m.get("industries") or [])[:3],
                    "region": m.get("region")})
    return out


def build_report(date: str, *, panel=None, focus=None) -> dict:
    """组装结构化日报(dict)。panel/focus 可传入复用。"""
    from tools.analysis.sector_forecast import regime_panel as RP
    from tools.analysis.sector_forecast import focus as F
    if panel is None:
        panel = RP.build_sector_regime(date)
    if focus is None:
        focus = F.build_focus(date, panel=panel)

    headlines = _news_headlines(date)
    重点 = focus["重点板块池"]
    分板块 = []
    预警 = []
    for r in 重点:
        sw = r["板块"]
        roster = _load_roster(sw)
        roles_brief = {}
        if roster:
            for role, lst in roster.get("roles", {}).items():
                roles_brief[role] = [
                    {"code": it["code"], "name": it.get("name", ""), "选级": it["选级"]}
                    for it in lst[:2]]
                # 龙头/补涨里当日涨停 → 进预警
                for it in lst[:3]:
                    if it.get("今日涨停"):
                        预警.append({"板块": sw, "code": it["code"], "name": it.get("name", ""),
                                    "角色": role, "事件": "今日涨停", "多空": "多",
                                    "建议": "重点观察"})
        分板块.append({
            "板块": sw, "冷热": r["冷热"], "focus_score": r["focus_score"],
            "当日价量": {"成交占比": r.get("成交占比"), "涨停数": r.get("涨停数"),
                       "动量_截面档": r.get("动量_截面档")},
            "新闻催化": {"净催化": r.get("新闻净催化"), "利好": r.get("利好条"), "利空": r.get("利空条")},
            "角色名单": roles_brief,
            "结论": r.get("理由", ""),
        })

    return {
        "date": date, "version": REPORT_VERSION,
        "全局摘要": {
            "市场风险偏好": focus["风险偏好"]["风险偏好"],
            "大盘依据": focus["风险偏好"].get("依据"),
            "新闻主线": headlines,
        },
        "覆盖板块数": len(分板块),
        "分板块": 分板块,
        "个股预警清单": 预警,
        "名单变更建议": {
            "建议新增板块": next((p.get("目标板块建议", {}).get("建议新增板块", [])
                              for p in [_panel_meta(date)]), []),
        },
        "诚实边界": focus["诚实边界"] + ["新闻主线暂用sentiment_policy(选股副产),独立新闻库(D)就绪后替换"],
        "免责": "测试环境研究模拟,非投资建议。",
    }


def _panel_meta(date: str) -> dict:
    """读已落盘 sector_regime 的目标板块建议(若有)。"""
    from tools.analysis.sector_forecast.market_step import resolve_analysis_file
    p = resolve_analysis_file(date, "sector_regime.json")
    if p:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def render_markdown(report: dict) -> str:
    d = report["date"]
    g = report["全局摘要"]
    L = [f"# 板块定向分析 · {d}", "",
         "> ⚠️ 测试环境研究模拟,非投资建议。forward-shadow 留痕,不进生产选股决策。", "",
         "## 全局摘要",
         f"- **市场风险偏好**:{g['市场风险偏好']}({g['大盘依据']})",
         "- **新闻主线**(当日强度 top):"]
    for h in g["新闻主线"]:
        L.append(f"  - [{h['方向']}·强度{h['强度']}·{h.get('region','')}] {h['title']}（{'/'.join(h['行业'])}）")
    L += ["", f"## 重点板块（{report['覆盖板块数']} 个，按 focus_score 降序）", ""]
    for s in report["分板块"]:
        pv = s["当日价量"]; nc = s["新闻催化"]
        L.append(f"### {s['板块']}　[{s['冷热']}]　focus={s['focus_score']}")
        L.append(f"- 当日：成交占比 {_pct(pv['成交占比'])}、涨停 {pv['涨停数']}、截面动量 {pv['动量_截面档']}")
        L.append(f"- 催化：净 {nc['净催化']}（利好{nc['利好']}/利空{nc['利空']}）")
        for role, lst in s["角色名单"].items():
            if lst:
                names = "、".join(f"{it['name'] or it['code']}({it['选级']})" for it in lst)
                L.append(f"- {role}：{names}")
        L.append(f"- **结论**：{s['结论']}")
        L.append("")
    if report["个股预警清单"]:
        L += ["## 个股预警清单", "", "| 板块 | 代码 | 名称 | 角色 | 事件 | 多空 | 建议 |",
              "|---|---|---|---|---|---|---|"]
        for w in report["个股预警清单"]:
            L.append(f"| {w['板块']} | {w['code']} | {w['name']} | {w['角色']} | {w['事件']} | {w['多空']} | {w['建议']} |")
        L.append("")
    nz = report["名单变更建议"]["建议新增板块"]
    if nz:
        L += ["## 名单变更建议（自动·需人工确认）"]
        for x in nz:
            L.append(f"- 建议新增 **{x['板块']}**：{x.get('理由','')}")
        L.append("")
    L += ["## 诚实边界"] + [f"- {b}" for b in report["诚实边界"]]
    return "\n".join(L)


def write_report(date: str, *, panel=None, focus=None, out_root: Optional[str] = None) -> tuple[Path, Path]:
    from tools.config import settings
    root = Path(out_root) if out_root else settings.PROJECT_ROOT / "data" / "analysis" / date
    root.mkdir(parents=True, exist_ok=True)
    report = build_report(date, panel=panel, focus=focus)
    jp = root / "sector_daily.json"
    jp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    mp = root / "sector_daily.md"
    mp.write_text(render_markdown(report), encoding="utf-8")
    logger.info("落盘 %s + %s(重点%d板块/预警%d)", mp, jp,
                report["覆盖板块数"], len(report["个股预警清单"]))
    return mp, jp


def _pct(v):
    return f"{v:.1%}" if isinstance(v, (int, float)) else "?"
