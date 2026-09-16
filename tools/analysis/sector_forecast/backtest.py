"""P3 论证 · 板块定向 EOD-proxy 历史回测——立即给"双跑对比"方向性证据。

**为什么是 proxy**:午盘全A重筛候选无历史(模块新),真 forward 双跑只能从上线日起累积。
为立即论证,用**收盘量价**近似午盘候选(EOD 收盘打分选强势票),历史日 A/B 对比 + forward。
诚实:EOD≠午盘(entry=收盘价、含全天量能),量级偏乐观;仅作方向性证据,真结论以 forward-shadow 为准。

流程(逐历史决策日):
  A = EOD 量价 proxy 排序 top-N(pct_chg/换手/成交额百分位;剔涨停不可买)。
  B = A 候选池 + 板块定向(sector_focus 重点池加权/规避池降权)。
  forward = entry(收盘)→ T+1/T+5 收盘收益(防未来只取决策日后 K线)。
聚合 A vs B 的 forward 均值/胜率,并看 **B新增票**(板块定向补进 A 漏的)forward 是否为正。
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger("sector_forecast.backtest")

POOL_TOPN = 8
FWD = (1, 5)


def _usable_dates(min_fwd: int = 1) -> list[str]:
    """有 sentiment_policy(催化维)且 forward 数据够(≤ master 最新 − min_fwd 交易日)的决策日。"""
    from tools.analysis.sector_forecast.market_step import resolve_analysis_file
    from tools.backtest.iet_probe import data as D
    D.bind_main_repo()
    from tools.store import repo as store
    df = store.get_master_kline("000001")
    tdays = sorted(df["date"].astype(str).str.slice(0, 10).unique())
    import glob
    from tools.config import settings
    from tools.backtest.iet_probe.data import _MAIN
    have = set()
    for base in (settings.PROJECT_ROOT, _MAIN):
        for p in glob.glob(str(__import__("pathlib").Path(base) / "data" / "analysis" / "*" / "sentiment_policy.json")):
            have.add(__import__("pathlib").Path(p).parent.name)
    out = []
    for d in tdays:
        if d not in have:
            continue
        idx = tdays.index(d)
        if idx + min_fwd < len(tdays):        # forward 至少有 min_fwd 天
            out.append(d)
    return out


def _eod_candidates(frame: pd.DataFrame, topn_pool: int = 40) -> list[dict]:
    """收盘量价 proxy 候选(剔涨停不可买):pct_chg/换手/成交额百分位合成,取 pool。"""
    f = frame[~frame["limit_up"]].copy()
    if f.empty:
        return []
    for col in ("pct_chg", "turnover", "amount"):
        f[col + "_r"] = f[col].rank(pct=True)
    f["proxy"] = 0.5 * f["pct_chg_r"] + 0.3 * f["turnover_r"] + 0.2 * f["amount_r"]
    f = f.sort_values("proxy", ascending=False).head(topn_pool)
    return [{"code": r["code"], "proxy": round(float(r["proxy"]) * 100, 2),
             "entry": float(r["close"]), "pct_chg": float(r["pct_chg"])}
            for _, r in f.iterrows()]


def _batch_thermo(dates: list[str], membership: dict) -> dict[str, dict]:
    """一次构建整窗口温度计面板(含 420 日 trailing 保证因果分位),按日切成 {date: {sw: 信号}}。

    免逐日重跑温度计(逐日 = N×60s;批量 = 1×~90s,board kline 只加载一次)。
    """
    from tools.backtest.iet_probe import data as D
    D.bind_main_repo()                               # 交易日历/board kline 读主仓
    from tools.analysis.industry_temp import thermometer as TH
    span = TH._trailing_dates(max(dates))            # 覆盖所有目标日的稠密日历(因果分位需连续历史)
    df = TH.build_thermometer_panel(span, membership)
    out: dict[str, dict] = {}
    want = set(dates)
    for d, g in df.groupby("date"):
        if d not in want:
            continue
        out[d] = {r["industry"]: {
            "拥挤分位": r["拥挤分位"], "拥挤": r["拥挤"],
            "动量_时序分位": r["动量_时序分位"], "动量_时序档": r["动量_时序档"],
            "动量_截面分位": r["动量_截面分位"], "动量_截面档": r["动量_截面档"],
            "情绪": r["情绪"], "n_members": int(r["n_members"] or 0), "asof": d,
        } for _, r in g.iterrows()}
    return out


def run_backtest(dates: Optional[list[str]] = None, *, topn: int = POOL_TOPN) -> dict:
    from tools.analysis.sector_forecast import regime_panel as RP
    from tools.analysis.sector_forecast import focus as F
    from tools.analysis.sector_forecast import sector_hint as SH
    from tools.analysis.sector_forecast import universe as U
    from tools.analysis.sector_forecast import dual_run as DR

    dates = dates or _usable_dates(min_fwd=1)
    membership = U._membership()
    thermo_by_date = _batch_thermo(dates, membership)     # 一次构建,按日切片
    per_day = []
    for date in dates:
        frame = U.load_sector_frame(date)
        if frame.empty:
            continue
        panel = RP.build_sector_regime(date, frame=frame, therm=thermo_by_date.get(date))
        focus = F.build_focus(date, panel=panel)          # 真 focus(复用批量温度计切片)
        cands = _eod_candidates(frame)
        if not cands:
            continue
        codes = [c["code"] for c in cands]
        hints = SH.build_sector_hint(date, codes, focus=focus, membership=membership)
        for c in cands:
            h = hints.get(c["code"], {})
            c["bonus"] = h.get("bonus", 0.0)
            c["B"] = c["proxy"] + c["bonus"]
            c["板块"] = h.get("板块"); c["in_avoid"] = h.get("in_avoid", False)
            c["fwd"] = DR._forward_returns(c["code"], date, c["entry"])
        A = sorted(cands, key=lambda x: -x["proxy"])[:topn]
        B = sorted(cands, key=lambda x: -x["B"])[:topn]
        a_codes = {c["code"] for c in A}
        新增 = [c for c in B if c["code"] not in a_codes]
        per_day.append({"date": date, "A": A, "B": B, "新增": 新增})
        logger.info("回测 %s: 候选池%d A/B各%d B新增%d", date, len(cands), len(A), len(新增))

    return {"dates": [d["date"] for d in per_day], "per_day": per_day,
            "汇总": _aggregate(per_day)}


def run_coverage_backtest(dates: Optional[list[str]] = None, *, topn: int = POOL_TOPN,
                          top_sectors: int = 3, per_role: int = 2) -> dict:
    """修正版 B':**覆盖式**——量价 top ∪ 占优板块 roster 里量价漏掉的补涨先锋/中军(主选)。

    检验双跑暴露的假设:踏空对象(热板块里低位待接力的补涨/中军)量价榜捞不到,主动补进是否兑现 α。
    对比 A(量价 top) vs 补入票(补涨/中军)的 forward。
    """
    from tools.analysis.sector_forecast import regime_panel as RP
    from tools.analysis.sector_forecast import focus as F
    from tools.analysis.sector_forecast import roster as RO
    from tools.analysis.sector_forecast import universe as U
    from tools.analysis.sector_forecast import dual_run as DR

    dates = dates or _usable_dates(min_fwd=1)
    membership = U._membership()
    mem_by_sw = RO._members_by_sector()
    thermo_by_date = _batch_thermo(dates, membership)
    per_day = []
    for date in dates:
        frame = U.load_sector_frame(date)
        if frame.empty:
            continue
        panel = RP.build_sector_regime(date, frame=frame, therm=thermo_by_date.get(date))
        focus = F.build_focus(date, panel=panel)
        cands = _eod_candidates(frame)
        if not cands:
            continue
        for c in cands:
            c["fwd"] = DR._forward_returns(c["code"], date, c["entry"])
        A = sorted(cands, key=lambda x: -x["proxy"])[:topn]
        a_codes = {c["code"] for c in A}
        close_map = frame.set_index("code")["close"].to_dict()

        # 占优板块 top_sectors → roster 补涨/中军主选,补入量价漏掉的
        补入 = []
        for fr in focus["重点板块池"][:top_sectors]:
            sw = fr["板块"]
            members = mem_by_sw.get(sw, [])
            if not members:
                continue
            roster = RO.build_roster(sw, date, members)
            for role in ("补涨先锋", "中军"):
                for it in roster["roles"].get(role, [])[:per_role]:
                    code = it["code"]
                    if code in a_codes or code in {x["code"] for x in 补入}:
                        continue
                    entry = close_map.get(code)
                    if not entry:
                        continue
                    补入.append({"code": code, "name": it.get("name"), "板块": sw, "角色": role,
                                 "选级": it.get("选级"), "entry": float(entry),
                                 "fwd": DR._forward_returns(code, date, float(entry))})
        per_day.append({"date": date, "A": A, "补入": 补入})
        logger.info("覆盖回测 %s: A%d 补入%d(补涨/中军)", date, len(A), len(补入))

    # 聚合:A vs 补入(按角色分)
    def _mean(key, fwd_key, role=None):
        vals = []
        for d in per_day:
            for c in d[key]:
                if role and c.get("角色") != role:
                    continue
                v = c["fwd"].get(fwd_key)
                if v is not None:
                    vals.append(v)
        return {"均值": round(sum(vals) / len(vals), 3) if vals else None, "n": len(vals),
                "胜率": round(sum(1 for v in vals if v > 0) / len(vals), 3) if vals else None}
    汇总 = {"决策日数": len(per_day)}
    for fk in (f"T+{h}" for h in FWD):
        汇总[fk] = {"A量价top": _mean("A", fk), "补入全部": _mean("补入", fk),
                   "补入_补涨先锋": _mean("补入", fk, "补涨先锋"),
                   "补入_中军": _mean("补入", fk, "中军")}
    return {"dates": [d["date"] for d in per_day], "per_day": per_day, "汇总": 汇总}


def _aggregate(per_day: list[dict]) -> dict:
    def _mean(picks_key, fwd_key):
        vals = []
        for d in per_day:
            for c in d[picks_key]:
                v = c["fwd"].get(fwd_key)
                if v is not None:
                    vals.append(v)
        return {"均值": round(sum(vals) / len(vals), 3) if vals else None, "n": len(vals),
                "胜率": round(sum(1 for v in vals if v > 0) / len(vals), 3) if vals else None}
    out = {"决策日数": len(per_day)}
    for fk in (f"T+{h}" for h in FWD):
        out[fk] = {"A": _mean("A", fk), "B": _mean("B", fk), "B新增": _mean("新增", fk)}
    return out
