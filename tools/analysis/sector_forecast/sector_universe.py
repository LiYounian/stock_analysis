"""S1 · 板块 universe 维护(周度)——程序化流水线第一阶段。

设计契约:docs/计划/2026-09-16_消息板块选股_程序化流水线_设计.md §1 S1。
解决"板块只 ~10 个"——把覆盖从种子 10 板块扩到**全申万一级**(28–31 个),并按量价
标注热门/活跃,产**周度过程文件** + **与上周 diff 留痕**。

输入:全A 行情 + 申万一级归属 + 广度/成交/涨停/换手(全部复用 universe.load_sector_frame)。
处理:按成交额占比 / 涨停家数 / 换手 / 动量共振排活跃度,规则化标注热门 flag。
输出:data/analysis/sector_universe.json(板块清单 + 热度标记 + 生成日 + 与上周 diff)。
      并归档 data/analysis/sector_universe_history/<date>.json(留痕,供后续 diff 基线)。
周期:每周一次(+ 成分调整时)。**纯量价、无 LLM。**

**防未来函数**:只读 ≤date 的 master kline 当日行(经 universe.load_sector_frame 保证)。
热门阈值**预注册写死**(HOT_* 常量),不对个例调——防偷看。
⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("sector_forecast.sector_universe")

UNIVERSE_VERSION = "v1-2026-09-16"

# —— 热门/活跃 阈值(预注册写死,不对个例调) ——
HOT_TOP_N = 8            # 活跃度综合排名前 N → 热门
HOT_LIMIT_MIN = 3        # 涨停家数 ≥ 此 → 直接判热门(涨停潮)
HOT_MEANPCT_MIN = 2.0    # 板块均涨幅(%) ≥ 此 → 直接判热门(普涨)
WARM_TOP_N = 16          # 前 N(未入热门)→ 温;其余 → 冷


def _board_turnover(frame: pd.DataFrame) -> dict[str, float]:
    """{sw: 板块换手率中位数(%)}。用中位数抗异常值(个别小盘高换手)。"""
    out: dict[str, float] = {}
    if frame.empty or "turnover" not in frame:
        return out
    for sw, g in frame.groupby("sw"):
        t = g["turnover"].dropna()
        if len(t):
            out[sw] = round(float(t.median()), 3)
    return out


def _thermometer(date: str) -> dict:
    """板块温度计(拥挤/动量),honest degrade:取不到 → 空 dict,不阻断 S1。"""
    try:
        from tools.analysis.industry_temp import thermometer as TH
        return TH.get_industry_thermometer(date) or {}
    except Exception as e:
        logger.warning("温度计取用失败(S1 降级为纯广度/成交口径):%s", e)
        return {}


def _rank_pct(s: pd.Series) -> pd.Series:
    return s.rank(pct=True, method="average")


def build_sector_universe(date: str, *, frame=None) -> dict:
    """构建 date 当日全申万一级板块 universe(含活跃度排名 + 热门标记)。

    frame 可传入复用(省一次全A加载,与面板/角色共用)。
    """
    from tools.analysis.sector_forecast import universe as U
    if frame is None:
        frame = U.load_sector_frame(date)
    bd = U.sector_breadth(frame)                       # 每 sw:n/上涨占比/涨停数/成交额/成交占比/均涨幅
    if bd.empty:
        return {"date": date, "version": UNIVERSE_VERSION, "板块清单": [], "n_板块": 0,
                "口径": "申万一级 · 纯量价 · 全A当日聚合", "免责": "测试环境研究模拟,非投资建议。"}
    turn = _board_turnover(frame)
    therm = _thermometer(date)

    bd = bd.copy()
    bd["板块换手中位"] = bd["sw"].map(turn)
    bd["动量_时序分位"] = bd["sw"].map(lambda s: (therm.get(s, {}) or {}).get("动量_时序分位"))
    bd["拥挤分位"] = bd["sw"].map(lambda s: (therm.get(s, {}) or {}).get("拥挤分位"))

    # 活跃度综合 = 成交占比 + 涨停数 + 换手 + 动量共振(缺失置 0,honest degrade,不惩罚缺失)
    r_amt = _rank_pct(bd["板块成交占比"].fillna(0))
    r_lim = _rank_pct(bd["涨停数"].fillna(0))
    r_turn = _rank_pct(bd["板块换手中位"].fillna(0))
    r_mom = _rank_pct(bd["动量_时序分位"].fillna(0)) if bd["动量_时序分位"].notna().any() else 0.0
    bd["活跃度分"] = (r_amt * 1.2 + r_lim + r_turn * 0.8 + r_mom * 0.5)
    bd = bd.sort_values("活跃度分", ascending=False).reset_index(drop=True)
    bd["活跃度排名"] = bd.index + 1

    # 排名档口径:绝对 N 与"约 28%/55% 板块数"取小——生产(~31板块)= 前8/16,
    # 小 universe 也不会把全部板块判热(防测试/异常日全热)。
    import math
    n_all = len(bd)
    hot_cut = min(HOT_TOP_N, max(1, math.ceil(n_all * 0.28)))
    warm_cut = min(WARM_TOP_N, max(hot_cut, math.ceil(n_all * 0.55)))

    def _tier(row) -> str:
        rank = int(row["活跃度排名"])
        hot = (rank <= hot_cut) or (int(row["涨停数"] or 0) >= HOT_LIMIT_MIN) \
            or ((row["板块均涨幅"] or -99) >= HOT_MEANPCT_MIN)
        if hot:
            return "热"
        return "温" if rank <= warm_cut else "冷"

    板块清单 = []
    for _, r in bd.iterrows():
        tier = _tier(r)
        板块清单.append({
            "板块": r["sw"], "活跃度排名": int(r["活跃度排名"]),
            "热度档": tier, "热门": tier == "热",
            "成分数": int(r["n"]),
            "板块成交额": _round(r["板块成交额"], 0),
            "板块成交占比": _round(r["板块成交占比"], 6),
            "涨停数": int(r["涨停数"] or 0),
            "上涨家数占比": _round(r["上涨家数占比"], 4),
            "板块均涨幅": _round(r["板块均涨幅"], 4),
            "板块换手中位": _round(r["板块换手中位"], 3),
            "动量_时序分位": _round(r["动量_时序分位"], 4),
            "拥挤分位": _round(r["拥挤分位"], 4),
        })
    n_hot = sum(1 for x in 板块清单 if x["热门"])
    return {
        "date": date, "version": UNIVERSE_VERSION,
        "口径": "申万一级 · 纯量价 · 全A当日聚合(成交占比/涨停/换手/动量共振→活跃度)",
        "热门阈值": {"活跃前N": HOT_TOP_N, "涨停≥": HOT_LIMIT_MIN, "均涨幅≥%": HOT_MEANPCT_MIN,
                    "温前N": WARM_TOP_N, "本次热cut": hot_cut, "本次温cut": warm_cut},
        "n_板块": len(板块清单), "n_热门": n_hot,
        "板块清单": 板块清单,
        "诚实边界": ["概念级细分成分缺(仅申万一级,如低空/机器人/算力不隔离)",
                    "热度=当日量价活跃度、非新闻催化(催化在 S3/S4)",
                    "动量共振缺失板块按 0 计入(honest degrade,不惩罚)"],
        "免责": "测试环境研究模拟,非投资建议。",
    }


def _iso_week(date: str) -> str:
    from datetime import datetime
    y, w, _ = datetime.strptime(date, "%Y-%m-%d").isocalendar()
    return f"{y}-W{w:02d}"


def _prev_universe(cur_path: Path, hist_dir: Path, date: str) -> Optional[dict]:
    """取 diff 基线:优先历史归档中 ISO 周早于本周的最近一份;否则退回当前 sector_universe.json。"""
    cur_week = _iso_week(date)
    if hist_dir.exists():
        cands = []
        for p in hist_dir.glob("sector_universe_*.json"):
            d = p.stem.replace("sector_universe_", "")
            try:
                if _iso_week(d) != cur_week:
                    cands.append((d, p))
            except Exception:
                continue
        if cands:
            cands.sort(reverse=True)                    # 最近的早于本周的一份
            try:
                return json.loads(cands[0][1].read_text(encoding="utf-8"))
            except Exception:
                pass
    if cur_path.exists():
        try:
            prev = json.loads(cur_path.read_text(encoding="utf-8"))
            if prev.get("date") and _iso_week(prev["date"]) != cur_week:
                return prev
        except Exception:
            pass
    return None


def _week_diff(cur: dict, prev: Optional[dict]) -> dict:
    """与上周板块清单 diff:新增/剔除板块、转热/转冷、活跃度排名大变。留痕用。"""
    if not prev:
        return {"基线": None, "说明": "无上周基线(首次生成)"}
    cur_map = {x["板块"]: x for x in cur.get("板块清单", [])}
    prev_map = {x["板块"]: x for x in prev.get("板块清单", [])}
    新增 = sorted(set(cur_map) - set(prev_map))
    剔除 = sorted(set(prev_map) - set(cur_map))
    转热, 转冷, 排名大变 = [], [], []
    for sw in sorted(set(cur_map) & set(prev_map)):
        c, p = cur_map[sw], prev_map[sw]
        if c["热门"] and not p["热门"]:
            转热.append(sw)
        elif p["热门"] and not c["热门"]:
            转冷.append(sw)
        dr = (p["活跃度排名"] or 0) - (c["活跃度排名"] or 0)   # 正=名次上升(变活跃)
        if abs(dr) >= 5:
            排名大变.append({"板块": sw, "上周排名": p["活跃度排名"], "本周排名": c["活跃度排名"],
                           "变化": dr})
    排名大变.sort(key=lambda x: -x["变化"])
    return {"基线": prev.get("date"), "新增板块": 新增, "剔除板块": 剔除,
            "转热": 转热, "转冷": 转冷, "活跃度排名大变(≥5名)": 排名大变}


def write_sector_universe(date: str, *, out_root: Optional[str] = None, frame=None,
                          payload: Optional[dict] = None) -> Path:
    """落 data/analysis/sector_universe.json(原子写)+ 归档 history。payload 可传入复用。"""
    from tools.config import settings
    base = Path(out_root) if out_root else settings.PROJECT_ROOT / "data" / "analysis"
    base.mkdir(parents=True, exist_ok=True)
    out = base / "sector_universe.json"
    hist_dir = base / "sector_universe_history"
    hist_dir.mkdir(parents=True, exist_ok=True)

    if payload is None:
        payload = build_sector_universe(date, frame=frame)
    prev = _prev_universe(out, hist_dir, date)
    payload["与上周diff"] = _week_diff(payload, prev)
    payload["ISO周"] = _iso_week(date)

    body = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.rename(out)
    # 归档当日一份(留痕 + 后续 diff 基线)
    (hist_dir / f"sector_universe_{date}.json").write_text(body, encoding="utf-8")

    d = payload.get("与上周diff", {})
    logger.info("S1 板块universe → %s(%d板块/%d热门;新增%s 剔除%s 转热%s 转冷%s)",
                out, payload.get("n_板块"), payload.get("n_热门"),
                d.get("新增板块"), d.get("剔除板块"), d.get("转热"), d.get("转冷"))
    return out


def _round(v, n=2):
    try:
        f = float(v)
        return None if np.isnan(f) else (int(f) if n == 0 else round(f, n))
    except Exception:
        return None
