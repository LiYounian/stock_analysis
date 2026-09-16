"""目标板块自动建议(P1.3)——从当日板块面板捞"种子外的新热点" + 标"种子里该暂避的"。

判据(需求文档 §3.1,预注册·可复核):
  · 建议新增 = 非种子板块 且 (成交占比 top / 均涨幅居前 / 涨停多 / 冷热∈{过热,正常活跃})。
  · 建议暂避 = 种子板块 且 冷热=过冷 且 成交占比低 且 均涨幅落后。
新闻/政策命中维度留 P2(独立新闻库就绪后接入),P1 只用可离线的成交+价量+广度。
名单是**建议**,最终由人工确认(需求文档 §3.3)。
"""
from __future__ import annotations

from typing import Optional

# 预注册阈值(写死)
NEW_AMT_SHARE = 0.03        # 成交占比 ≥3% 才够"主流热点"量级
NEW_MIN_LIMIT = 2          # 涨停数 ≥2
AVOID_AMT_SHARE = 0.01     # 成交占比 <1% = 边缘板块


def suggest_sectors(panel: list[dict], *, seed: Optional[list[str]] = None) -> dict:
    """从板块面板(regime_panel.build_sector_regime 的输出)产出建议。"""
    from tools.analysis.sector_forecast.roles import SEED_SW
    seed = set(seed or SEED_SW)

    新增, 暂避 = [], []
    for r in panel:
        sw = r["板块"]
        share = r.get("板块成交占比") or 0
        avg = r.get("板块均涨幅")
        lim = r.get("涨停数") or 0
        label = r.get("冷热标签")

        if sw not in seed:
            # 种子外:量能够 + (涨停多 或 强动量领涨) + 非过冷
            hot = label in ("过热", "正常活跃")
            strong = lim >= NEW_MIN_LIMIT or (r.get("动量_截面档") == "热")
            if share >= NEW_AMT_SHARE and strong and label != "过冷":
                新增.append({
                    "板块": sw, "冷热": label, "成交占比": round(share, 4),
                    "涨停数": lim, "均涨幅": avg,
                    "理由": f"成交占比{share:.1%}、涨停{lim}"
                            + ("、强动量领涨" if r.get("动量_截面档") == "热" else "")
                            + f"、{label}",
                })
        else:
            if label == "过冷" and share < AVOID_AMT_SHARE:
                暂避.append({
                    "板块": sw, "冷热": label, "成交占比": round(share, 4),
                    "均涨幅": avg,
                    "理由": f"种子板块转过冷、成交占比仅{share:.1%}、均涨幅{avg}",
                })

    新增.sort(key=lambda x: -x["成交占比"])
    暂避.sort(key=lambda x: x["成交占比"])
    return {
        "建议新增板块": 新增, "建议暂避板块": 暂避,
        "口径": "成交占比+涨停+动量(P1);新闻/政策命中留P2",
        "说明": "自动建议,需人工确认后并入监控主表(需求文档§3.3)",
    }
