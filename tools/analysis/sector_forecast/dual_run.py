"""P3 · 板块定向双跑对比(forward-shadow · non-gating · 不改选股代码)。

**用户指示:双跑→对比结果找差异→进一步论证→便于修正。** 不直接接生产选股,而是让两套并行:
  · **A 基线**:午盘全A重筛原排序(板块-agnostic,量价综合分)。
  · **B 处理**:同一候选池 + 板块定向重排(量价综合分 + sector_hint 加/降分)。
对比 top-N 选票差异(B 新增/剔除)+ forward 收益(次日/T+5,若数据就绪)+ 归因(B 新增的是不是
A 漏的赢家 = 防踏空是否兑现)。落盘攒样本,显著且正向才谈真接(P3 gate)。

数据源:A 候选 = `data/intraday/<date>/noon_fullA_screen.json`(统筹午盘线产出,只读);
sector_hint = 本线 sector_focus + 角色表。forward 收益用 master kline(≤评估时点,防未来)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sector_forecast.dual_run")

DUALRUN_VERSION = "v1-2026-09-16"
TOPN = 8                     # 对比的选票条数(对齐午盘强势候选条数)
FWD_HORIZONS = (1, 5)       # forward 记分区间(交易日)


def _load_noon(date: str) -> Optional[dict]:
    from tools.config import settings
    from tools.backtest.iet_probe.data import _MAIN
    for base in (settings.PROJECT_ROOT, _MAIN):
        p = Path(base) / "data" / "intraday" / date / "noon_fullA_screen.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def _forward_returns(code: str, date: str, entry: float, horizons=FWD_HORIZONS) -> dict:
    """entry 价 → 之后第 k 个交易日收盘的收益%(防未来:只取 date 之后已存在的 K线)。"""
    from tools.backtest.iet_probe import data as D
    D.bind_main_repo()
    from tools.store import repo as store
    try:
        df = store.get_master_kline(code)
    except Exception:
        return {}
    df = df[df["date"].astype(str).str.slice(0, 10) >= date].sort_values("date")
    # df.iloc[0] = 决策日(date)当天;之后第 k 行 = T+k
    out = {}
    for k in horizons:
        if len(df) > k and entry:
            close_k = float(df.iloc[k]["close"])
            out[f"T+{k}"] = round((close_k / entry - 1.0) * 100, 3)
        else:
            out[f"T+{k}"] = None       # forward 数据未就绪(未来日)
    return out


def build_dual_run(date: str, *, topn: int = TOPN) -> dict:
    noon = _load_noon(date)
    if not noon:
        return {"date": date, "error": f"无 noon_fullA_screen.json({date}),双跑需午盘候选"}
    cands = noon.get("候选", [])
    if not cands:
        return {"date": date, "error": "候选为空"}

    from tools.analysis.sector_forecast import sector_hint as SH
    codes = [c["code"] for c in cands]
    hints = SH.build_sector_hint(date, codes)

    # A:量价综合分;B:量价综合分 + 板块定向 bonus
    rows = []
    for c in cands:
        code = c["code"]
        base = c.get("量价综合分")
        if base is None:
            continue
        h = hints.get(code, {})
        bonus = h.get("bonus", 0.0)
        rows.append({
            "code": code, "name": c.get("name"),
            "量价综合分": base, "板块bonus": bonus, "B综合分": round(base + bonus, 3),
            "板块": h.get("板块"), "角色": h.get("角色"), "选级": h.get("选级"),
            "in_focus": h.get("in_focus", False), "in_avoid": h.get("in_avoid", False),
            "entry": c.get("尾盘参考限价") or c.get("factors", {}).get("现价"),
        })
    A = sorted(rows, key=lambda r: -r["量价综合分"])[:topn]
    B = sorted(rows, key=lambda r: -r["B综合分"])[:topn]
    a_codes = {r["code"] for r in A}
    b_codes = {r["code"] for r in B}
    B新增 = [r for r in B if r["code"] not in a_codes]
    B剔除 = [r for r in A if r["code"] not in b_codes]
    # 归因来源(统筹要求):增票是**重点池加权**带上来;减票分**自己在规避池被降权** vs **被加权票挤掉**
    for r in B新增:
        r["来源"] = "重点池加权" if r["板块bonus"] > 0 else "规避腾位(他票降权让出)"
    for r in B剔除:
        r["来源"] = "规避池降权" if r["in_avoid"] else "被重点票挤出"

    # forward 收益(A/B 各自 + 差集)
    def _fwd(r):
        return _forward_returns(r["code"], date, r.get("entry"))
    for r in A + B:
        r["forward"] = _fwd(r)
    fwd_ready = any(r["forward"].get("T+1") is not None for r in A + B)

    def _avg(lst, k):
        vals = [r["forward"].get(k) for r in lst if r["forward"].get(k) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    对比 = {}
    for k in (f"T+{h}" for h in FWD_HORIZONS):
        对比[k] = {"A均值": _avg(A, k), "B均值": _avg(B, k),
                  "B新增均值": _avg(B新增, k), "B剔除均值": _avg(B剔除, k)}

    return {
        "date": date, "version": DUALRUN_VERSION,
        "候选池大小": len(rows), "topn": topn,
        "forward就绪": fwd_ready,
        "A_topN": [_brief(r) for r in A],
        "B_topN": [_brief(r) for r in B],
        "B新增": [_brief(r) for r in B新增],
        "B剔除": [_brief(r) for r in B剔除],
        "forward对比": 对比,
        "归因说明": "B新增=板块定向补进A漏掉的票;若其 forward 均值 > B剔除,则板块定向兑现防踏空价值",
        "口径": "A=午盘量价原排序;B=量价+sector_hint(重点池加权/规避池降权,只调不硬否)",
        "诚实边界": ["forward-shadow·non-gating·不进生产选股(gate 需≥样本+显著)",
                    "forward 收益用 entry=尾盘参考限价、master kline 之后收盘(防未来)",
                    "未来日 forward 未就绪时标 None,待补跑"],
        "免责": "测试环境研究模拟,非投资建议。",
    }


def _brief(r: dict) -> dict:
    d = {"code": r["code"], "name": r["name"], "量价分": r["量价综合分"],
         "板块bonus": r["板块bonus"], "B综合分": r["B综合分"],
         "板块": r["板块"], "角色": r["角色"], "选级": r["选级"],
         "in_avoid": r["in_avoid"], "forward": r["forward"]}
    if "来源" in r:
        d["来源"] = r["来源"]
    return d


def write_dual_run(date: str, *, out_root: Optional[str] = None) -> Optional[Path]:
    from tools.config import settings
    payload = build_dual_run(date)
    if payload.get("error"):
        logger.warning("双跑跳过:%s", payload["error"])
        return None
    root = Path(out_root) if out_root else settings.PROJECT_ROOT / "data" / "analysis" / date
    root.mkdir(parents=True, exist_ok=True)
    out = root / "sector_dualrun.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(out)
    logger.info("落盘 %s:A/B各%d,B新增%d,forward就绪=%s", out, payload["topn"],
                len(payload["B新增"]), payload["forward就绪"])
    return out
