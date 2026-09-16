"""每日板块环境面板(产出 A)——拥挤/动量(温度计)+ 广度/成交(全A聚合)→ 冷热标签。

复用(不重造):
  · 拥挤度 + 动量档 = `industry_temp.thermometer.get_industry_thermometer`(因果 PIT native 口径)。
  · 广度 + 成交 = `sector_forecast.universe`(全A单次加载 → 按申万一级聚合)。
  · 冷热标签 = `sector_forecast.labels.classify_regime`(预注册规则合成)。

产出:data/analysis/<date>/sector_regime.json —— 每个申万一级一条,给下游(角色表/两步框架/选股)。
诚实边界:资金维度 P1 只有**板块成交额/占比**(个股 fundflow 陈旧不进);新闻催化留 P2。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from tools.analysis.industry_temp import thermometer as TH
from tools.analysis.sector_forecast import labels as L
from tools.analysis.sector_forecast import universe as U

logger = logging.getLogger("sector_forecast.regime_panel")

PANEL_VERSION = "v1-2026-09-16"


def build_sector_regime(date: str, *, frame=None, therm=None) -> list[dict]:
    """合成 date 当日全板块环境面板(list,每板块一条)。

    frame 可传入复用(省一次全A加载);therm 可传入预算的温度计信号 {sw: {...}}
    (批量回测时一次构建整窗口面板后按日切片,免逐日重跑温度计 420 日面板)。
    """
    if therm is None:
        therm = TH.get_industry_thermometer(date)      # {sw: {拥挤/动量/...}}
    if frame is None:
        frame = U.load_sector_frame(date)
    bd = U.sector_breadth(frame).set_index("sw")

    all_sw = sorted(set(therm) | set(bd.index))
    out = []
    for sw in all_sw:
        t = therm.get(sw, {})
        b = bd.loc[sw].to_dict() if sw in bd.index else {}
        crowd_p = t.get("拥挤分位")
        mom_cs = t.get("动量_截面分位")
        mom_ts = t.get("动量_时序分位")
        adv_ratio = b.get("上涨家数占比")
        lab = L.classify_regime(拥挤分位=crowd_p, 动量_截面分位=mom_cs,
                                动量_时序分位=mom_ts, 上涨家数占比=adv_ratio)
        out.append({
            "板块": sw,
            "冷热标签": lab["标签"],
            "标签依据": lab["依据"],
            "标签置信": lab["置信"],
            "拐点方向": lab.get("拐点方向"),
            # —— 拥挤/动量(温度计,因果 PIT)——
            "拥挤分位": crowd_p, "拥挤档": t.get("拥挤"),
            "动量_时序分位": mom_ts, "动量_时序档": t.get("动量_时序档"),
            "动量_截面分位": mom_cs, "动量_截面档": t.get("动量_截面档"),
            # —— 广度/成交(全A当日聚合)——
            "上涨家数占比": adv_ratio, "涨停数": b.get("涨停数"),
            "板块成交额": b.get("板块成交额"), "板块成交占比": b.get("板块成交占比"),
            "板块均涨幅": b.get("板块均涨幅"), "成分数": b.get("n") or t.get("n_members"),
            "asof": t.get("asof") or date,
        })
    # 过热/拐点在前(风险优先),再按成交占比降序
    order = {"过热": 0, "拐点": 1, "正常活跃": 2, "过冷": 3, "数据不足": 4}
    out.sort(key=lambda r: (order.get(r["冷热标签"], 9),
                            -(r["板块成交占比"] or 0)))
    return out


def write_sector_regime(date: str, *, out_root: Optional[str] = None, frame=None,
                        panel: Optional[list[dict]] = None) -> Path:
    """落库 data/analysis/<date>/sector_regime.json(原子写)。panel 可传入复用,避免重复构建。"""
    from tools.config import settings
    root = Path(out_root) if out_root else settings.PROJECT_ROOT / "data" / "analysis" / date
    root.mkdir(parents=True, exist_ok=True)
    if panel is None:
        panel = build_sector_regime(date, frame=frame)
    from tools.analysis.sector_forecast import suggest as SG
    建议 = SG.suggest_sectors(panel)
    payload = {
        "date": date, "version": PANEL_VERSION,
        "口径": "申万一级 · 因果PIT · 规则合成标签",
        "诚实边界": ["概念级成分缺(仅申万一级)", "资金维度仅板块成交额(个股fundflow陈旧未纳入)",
                    "新闻催化维度留P2", "涨停由pct+板块限价派生(breadth.is_limit_hit)"],
        "标签阈值版本": L.LABELS_VERSION,
        "n_板块": len(panel),
        "目标板块建议": 建议,
        "板块": panel,
        "免责": "测试环境研究模拟,非投资建议。",
    }
    out = root / "sector_regime.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(out)
    logger.info("落盘 %s:%d 板块(过热%d/拐点%d/过冷%d)", out, len(panel),
                sum(1 for r in panel if r["冷热标签"] == "过热"),
                sum(1 for r in panel if r["冷热标签"] == "拐点"),
                sum(1 for r in panel if r["冷热标签"] == "过冷"))
    return out
