"""大盘→板块两步框架 · 第二步 + 合成:输出"重点选股板块池"(产出 E,`sector_focus.json`)。

**关键设计(防踏空)**:重点板块**不用冷热标签去挑**——冷热"过冷"= 中期降温(如电子:拥挤时序
低+20日弱),但当天可能正是催化+资金+涨停+补涨所在。若用"过冷"降权就会**重演踏空**。故重点池
按 **新闻催化 + 资金在哪(成交占比)+ 当日涨停/广度 + 截面动量** 合成;冷热只作**风险上下文**
(过热/下行拐点小幅降权)。

双下游契约(统筹对齐):`sector_focus.json` 同时供 ① 午盘全A重筛入参位 ② 晚间选股读取——两下游
读**同一份**当日 focus。每条重点板块带 `角色表` 指针(→ data/sector_roster/<板块>.json 的先锋/
中军/补涨/龙头),下游据此在占优板块内定向取票。

铁律:**forward-shadow · non-gating · 纯记录**——本层不进任何生产选股决策(P3 gated 才接);
防未来:只用 ≤date 已披露 sentiment_policy + ≤date 面板。
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sector_forecast.focus")

FOCUS_VERSION = "v1-2026-09-16"
_DIR_SIGN = {"利好": 1, "利空": -1, "中性": 0}

# 合成权重(预注册·写死)。**催化提到最高 0.40**(用户洞察:选股激进/规避的真锚是
# 消息面催化,不是纯量价动量;有真催化的强势票该进候选,无催化的纯动量才防 gap-fade)。
W_CATALYST, W_MONEY, W_STRENGTH, W_MOM, W_RISK = 0.40, 0.20, 0.20, 0.12, 0.08
# 重点池门槛:催化>0 或 涨停≥此数(有资金/有催化才算"值得重点选股")
FOCUS_MIN_LIMIT = 2


def news_catalyst_by_sector(date: str) -> dict[str, dict]:
    """从当日 sentiment_policy 按 industries rollup 到申万一级 → {sw: {净催化, n条, 利好, 利空}}。

    industries 里的概念名(AI算力/半导体…)经 industry_map.to_sw 归申万一级(单一真源)。
    """
    from tools.analysis.sector_forecast.market_step import resolve_analysis_file
    from tools.analysis import industry_map
    p = resolve_analysis_file(date, "sentiment_policy.json")
    if not p:
        return {}
    try:
        msgs = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    agg: dict[str, dict] = defaultdict(lambda: {"净催化": 0.0, "n条": 0, "利好": 0, "利空": 0})
    for m in msgs:
        sign = _DIR_SIGN.get(m.get("影响方向"), 0)
        strength = float(m.get("影响强度") or 0)
        inds = m.get("industries") or m.get("受影响行业") or []
        seen = set()
        for raw in inds:
            sw = industry_map.to_sw(raw)
            if not sw or sw in seen:
                continue
            seen.add(sw)
            a = agg[sw]
            a["净催化"] += sign * strength
            a["n条"] += 1
            if sign > 0:
                a["利好"] += 1
            elif sign < 0:
                a["利空"] += 1
    return dict(agg)


def _rank_map(pairs: list[tuple[str, float]]) -> dict[str, float]:
    """[(key, val)] → {key: 分位 rank 0..1}(缺/并列取平均秩)。"""
    import pandas as pd
    if not pairs:
        return {}
    s = pd.Series({k: v for k, v in pairs}, dtype=float)
    r = s.rank(pct=True, method="average")
    return {k: float(r[k]) for k in s.index}


def build_focus(date: str, *, panel: Optional[list[dict]] = None,
                macro: Optional[dict] = None) -> dict:
    """两步合成 → 重点/规避板块池。panel/macro 可传入复用(省全A加载/akshare调用)。

    macro = news_store.build_news_store(date)['宏观研判'](含 宏观净方向/宏观情景),
    供第一步大盘档融合 + 顶层 宏观情景 字段(选股预案查表用)。
    """
    from tools.analysis.sector_forecast import regime_panel as RP
    from tools.analysis.sector_forecast import market_step as MS

    if panel is None:
        panel = RP.build_sector_regime(date)
    appetite = MS.market_risk_appetite(date, macro=macro)
    news = news_catalyst_by_sector(date)

    # 各维 rank
    cat_rank = _rank_map([(r["板块"], news.get(r["板块"], {}).get("净催化", 0.0)) for r in panel])
    money_rank = _rank_map([(r["板块"], r.get("板块成交占比") or 0) for r in panel])
    lim_rank = _rank_map([(r["板块"], r.get("涨停数") or 0) for r in panel])

    重点, 规避 = [], []
    for r in panel:
        sw = r["板块"]
        nc = news.get(sw, {})
        catalyst = nc.get("净催化", 0.0)
        strength = 0.5 * lim_rank.get(sw, 0) + 0.5 * (r.get("上涨家数占比") or 0)
        mom = r.get("动量_截面分位") or 0
        # 风险上下文:过热/下行拐点 → 降分(不排除,只降)
        risk_pen = 0.0
        if r["冷热标签"] == "过热":
            risk_pen = 1.0
        elif r["冷热标签"] == "拐点" and "下行" in (r.get("拐点方向") or ""):
            risk_pen = 0.6
        score = (W_CATALYST * cat_rank.get(sw, 0) + W_MONEY * money_rank.get(sw, 0)
                 + W_STRENGTH * strength + W_MOM * mom + W_RISK * (1 - risk_pen))

        row = {
            "板块": sw, "focus_score": round(score, 4),
            "冷热": r["冷热标签"], "新闻净催化": round(catalyst, 1),
            "利好条": nc.get("利好", 0), "利空条": nc.get("利空", 0),
            "成交占比": r.get("板块成交占比"), "涨停数": r.get("涨停数"),
            "动量_截面档": r.get("动量_截面档"), "拥挤档": r.get("拥挤档"),
            "角色表": f"data/sector_roster/{sw}.json",
        }
        # 规避:新闻净利空 且 (拥挤A 或 过热)——坏消息叠拥挤/过热,回撤空间大
        if catalyst < 0 and (r.get("拥挤档") == "A" or r["冷热标签"] == "过热"):
            规避.append({**row, "规避理由": f"新闻净利空{catalyst:.1f}+"
                        + ("拥挤A" if r.get("拥挤档") == "A" else "过热")})
            continue
        # 重点:有催化(>0)或 有涨停(≥门槛)—— 有资金/有催化才值得定向选股
        if catalyst > 0 or (r.get("涨停数") or 0) >= FOCUS_MIN_LIMIT:
            row["理由"] = (f"新闻净催化{catalyst:.1f}(利好{nc.get('利好',0)}/利空{nc.get('利空',0)})、"
                          f"成交占比{_pct(r.get('板块成交占比'))}、涨停{r.get('涨停数')}、"
                          f"截面动量{r.get('动量_截面档')}"
                          + ("、⚠面板过冷但当日活跃" if r["冷热标签"] == "过冷" else ""))
            重点.append(row)

    重点.sort(key=lambda x: -x["focus_score"])
    规避.sort(key=lambda x: x["新闻净催化"])
    return {
        "date": date, "version": FOCUS_VERSION,
        "风险偏好": appetite,
        "宏观情景": (macro or {}).get("宏观情景", "中性"),      # 选股预案查表用离散标签
        "宏观净方向": (macro or {}).get("宏观净方向", "中性"),
        "重点板块池": 重点, "规避板块池": 规避,
        "口径": "新闻催化(0.40最高权重)+资金(成交占比)+当日涨停广度+截面动量;冷热仅作风险上下文"
                "(不据它挑板块);催化优先——有真催化的强势票不因超买规避,无催化纯动量才防gap-fade",
        "下游": "双喂 午盘全A重筛入参位 + 晚间选股(读同一份);角色表指针→定向取先锋/中军/补涨",
        "诚实边界": ["forward-shadow·non-gating·不进生产选股(P3 gated才接)",
                    "概念级成分缺(申万一级)", "资金维度仅板块成交额(个股fundflow陈旧未纳入)"],
        "免责": "测试环境研究模拟,非投资建议。",
    }


def write_focus(date: str, *, panel=None, macro=None, out_root: Optional[str] = None) -> Path:
    from tools.config import settings
    root = Path(out_root) if out_root else settings.PROJECT_ROOT / "data" / "analysis" / date
    root.mkdir(parents=True, exist_ok=True)
    payload = build_focus(date, panel=panel, macro=macro)
    out = root / "sector_focus.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(out)
    logger.info("落盘 %s:重点%d/规避%d 板块,大盘=%s", out,
                len(payload["重点板块池"]), len(payload["规避板块池"]),
                payload["风险偏好"]["风险偏好"])
    return out


def _pct(v):
    return f"{v:.1%}" if isinstance(v, (int, float)) else "?"
