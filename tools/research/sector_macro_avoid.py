"""C · 宏观/板块级消息规避 · forward-shadow（non-gating advisory，纯记录）。

诊断与设计见 docs/计划/2026-09-16_选股规避能力整改_诊断与回测设计.md（§2C/§3/§6）。

统筹裁决 scope（**消费已有基建·不重造**）：
  · 消息底座 = `data/analysis/<date>/sentiment_policy.json`（每条已带 `industries`+`影响方向`
    +`影响强度`+`region`，采集侧 `tools/collectors/policy.py` 产出，本层只读）。
  · 行业口径 = `industry_map.to_sw`（任意行业名 → 申万一级，单一真源）。
  · 板块冷热确认 = regime `thermometer.build_thermometer_panel`（拥挤度 A/B + 动量时序档，
    因果 PIT，消费不重造；缺数据优雅降级为 None）。

逻辑（预注册·落盘后只读不改·append-only）：
  ① 从当日 sentiment_policy 里筛出**命中三主题**的消息：美联储加息/利率 · 汇率(人民币) ·
     关税/出口管制（用户 2026-09-16 拍板先限高信噪比三主题，攒稳再扩全宏观词）。
  ② 命中消息按 `industries` rollup 到申万一级 → 聚合板块级**宏观净方向 + 净强度**
     （利好=+强度、利空=−强度、中性=0；同一申万一级多条累加）。
  ③ 对"宏观净利空"的板块打**规避标记**；叠加 thermometer 的拥挤/动量档作确认
     （"利空 + 拥挤 A / 热动量"= 高置信规避：坏消息叠拥挤，回撤空间大）。

铁律：
  · **non-gating · forward-only · 纯记录**——绝不进任何生产选股决策；≥120 样本 + 显著才谈 validate。
  · 防未来/防偷看：只用 ≤当日已披露 sentiment_policy + ≤当日 board 收盘（thermometer 因果）；
    三主题判据写死后只读不改。样本 < 120 只报 N。
⚠️ 测试环境研究模拟，非投资建议。
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field, asdict

from tools.analysis import industry_map
from tools.collectors import code_industry

logger = logging.getLogger("research.sector_macro_avoid")

# ── 预注册：三主题关键词（写死·只读不改；命中 = 消息 title+summary+keyword 含任一词）─────────
# 依据用户 2026-09-16 拍板：先限高信噪比三主题，与 policy.py 采集词同源、取其宏观子集。
THEMES: dict[str, tuple[str, ...]] = {
    "美联储加息/利率": (
        "美联储", "加息", "降息", "降准", "利率", "LPR", "FOMC", "议息",
        "鲍威尔", "非农", "CPI", "通胀", "PCE",
    ),
    "汇率(人民币)": (
        "汇率", "人民币", "离岸", "在岸", "中间价", "结售汇", "外汇储备",
    ),
    "关税/出口管制": (
        "关税", "出口管制", "实体清单", "BIS", "半导体出口", "301调查",
        "商务部工业与安全局", "贸易战", "制裁", "加征",
    ),
}

# 影响方向 → 符号（利好 = 看多、利空 = 看空、中性/其它 = 0）
_DIR_SIGN = {"利好": 1, "利空": -1, "中性": 0}

# 规避置信阈值（预注册·净利空强度绝对值）
_STRONG_ABS = 3.0     # |净强度| ≥ 3 视为强信号
_MILD_ABS = 1.0       # ≥ 1 视为有效信号（下限）


# ---------- 数据装配 ----------
def _resolve_root(data_root: str | None) -> str:
    """含 analysis/ 的数据根。显式 > 自动探测主仓（worktree 无本地 data 时回主仓）> 'data'。"""
    if data_root:
        return data_root
    try:
        from tools.analysis.market_forecast.dataroot import resolve_data_root
        return str(resolve_data_root(None))
    except Exception:  # noqa: BLE001
        return "data"


def policy_path(date: str, data_root: str | None = None) -> str:
    """当日 sentiment_policy.json 路径。缺省自动探测含 analysis/ 的数据根。"""
    return os.path.join(_resolve_root(data_root), "analysis", date, "sentiment_policy.json")


def load_policy(date: str, data_root: str | None = None) -> list[dict]:
    """读当日消息列表；缺文件 → []（优雅缺省，交由上层报 N）。"""
    p = policy_path(date, data_root)
    if not os.path.exists(p):
        return []
    try:
        d = json.load(open(p, encoding="utf-8"))
        return d if isinstance(d, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def match_themes(msg: dict) -> list[str]:
    """消息命中哪些主题（title+summary+keyword 子串匹配，可命中多主题）。"""
    text = " ".join(str(msg.get(k, "")) for k in ("title", "summary", "keyword"))
    hit = []
    for theme, words in THEMES.items():
        if any(w in text for w in words):
            hit.append(theme)
    return hit


# ---------- 板块级宏观净方向聚合 ----------
@dataclass
class SectorMacro:
    industry: str                       # 申万一级
    净强度: float = 0.0                  # Σ(sign×影响强度)
    命中主题: list = field(default_factory=list)
    利空条数: int = 0
    利好条数: int = 0
    证据: list = field(default_factory=list)  # [{title, 方向, 强度, 主题, 源行业}]

    @property
    def 净方向(self) -> str:
        if self.净强度 < 0:
            return "利空"
        if self.净强度 > 0:
            return "利好"
        return "中性"


def aggregate_macro(msgs: list[dict]) -> dict[str, SectorMacro]:
    """命中三主题的消息 → 按 industries rollup 到申万一级 → 聚合净方向/净强度。

    每条命中消息的每个 industry 映射到申万一级（to_sw，映射失败弃权），
    按 影响方向符号 × 影响强度 累加到该申万一级。"""
    agg: dict[str, SectorMacro] = {}
    for msg in msgs:
        themes = match_themes(msg)
        if not themes:
            continue
        try:
            strength = float(msg.get("影响强度") or 0)
        except (TypeError, ValueError):
            strength = 0.0
        sign = _DIR_SIGN.get(str(msg.get("影响方向") or ""), 0)
        direction = str(msg.get("影响方向") or "")
        inds = msg.get("industries") or msg.get("受影响行业") or []
        for raw in inds:
            sw = industry_map.to_sw(str(raw))
            if not sw:
                continue
            sm = agg.setdefault(sw, SectorMacro(industry=sw))
            sm.净强度 += sign * strength
            for t in themes:
                if t not in sm.命中主题:
                    sm.命中主题.append(t)
            if sign < 0:
                sm.利空条数 += 1
            elif sign > 0:
                sm.利好条数 += 1
            sm.证据.append({"title": str(msg.get("title", ""))[:60], "方向": direction,
                            "强度": strength, "主题": themes, "源行业": str(raw)})
    return agg


# ---------- thermometer 确认（消费 regime 板块层，不重造）----------
def thermometer_confirm(date: str, industries: list[str], data_root: str | None = None,
                        lookback: int = 30) -> dict[str, dict]:
    """返回 {申万一级: {拥挤, 动量档, 动量分位}}；缺数据 → 该行业留空 dict（优雅降级）。"""
    if not industries:
        return {}
    try:
        from tools.analysis.market_forecast.dataroot import ensure_data_root
        from tools.analysis.industry_temp import thermometer as th
        from tools.backtest.iet_probe import pipeline as PIPE
        from datetime import datetime, timedelta
        ensure_data_root(data_root)
        # 用交易日历回溯 lookback 个交易日（含当日），供 thermometer 因果算冷热档
        start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=lookback * 2)).strftime("%Y-%m-%d")
        cal_dates = list(PIPE.trading_calendar(start=start, end=date))
        panel = th.build_thermometer_panel(cal_dates, code_industry.load())
    except Exception as e:  # noqa: BLE001 - 缺数据/环境:整体降级为空确认
        logger.warning("thermometer 确认不可用（降级）：%s", str(e)[:80])
        return {}
    if panel.empty:
        return {}
    day = panel[panel["date"] == date]
    out: dict[str, dict] = {}
    for ind in industries:
        row = day[day["industry"] == ind]
        if row.empty:
            out[ind] = {}
            continue
        r = row.iloc[-1]
        out[ind] = {
            "拥挤": r.get("拥挤"),
            "动量档": r.get("动量_时序档"),
            "动量分位": (None if r.get("动量_时序分位") is None
                       else round(float(r["动量_时序分位"]), 3)),
        }
    return out


def avoid_confidence(sm: SectorMacro, confirm: dict) -> tuple[str, str]:
    """规避置信档 + 依据说明。净利空为前提；thermometer 拥挤/热动量 → 抬高置信。"""
    mag = abs(sm.净强度)
    crowded = confirm.get("拥挤") == "A"          # A=拥挤（高抛压/回撤空间）
    hot = confirm.get("动量档") == "热"           # 热动量 = 涨多、坏消息更易触发回撤
    reasons = [f"净强度{sm.净强度:+.1f}"]
    if crowded:
        reasons.append("拥挤A")
    if hot:
        reasons.append("热动量")
    if mag >= _STRONG_ABS and (crowded or hot):
        return "高", "；".join(reasons) + "（强利空+拥挤/热确认）"
    if mag >= _STRONG_ABS:
        return "中", "；".join(reasons) + "（强利空·无冷热确认）"
    if mag >= _MILD_ABS:
        return "中" if (crowded or hot) else "低", "；".join(reasons)
    return "低", "；".join(reasons) + "（弱信号）"


# ---------- 每日 shadow 记录器（non-gating advisory）----------
def run_daily_shadow(date: str, out_dir: str, data_root: str | None = None) -> dict:
    """筛三主题命中消息 → 板块级净方向 → 宏观利空板块打规避标记（+thermometer 确认）→ 落盘。"""
    os.makedirs(out_dir, exist_ok=True)
    msgs = load_policy(date, data_root)
    agg = aggregate_macro(msgs)
    avoid_inds = [ind for ind, sm in agg.items() if sm.净强度 < 0]
    confirm = thermometer_confirm(date, avoid_inds, data_root)

    板块规避 = []
    for ind in sorted(avoid_inds, key=lambda i: agg[i].净强度):   # 最利空在前
        sm = agg[ind]
        c = confirm.get(ind, {})
        conf, why = avoid_confidence(sm, c)
        板块规避.append({
            "industry": ind,
            "命中主题": sm.命中主题,
            "净方向": sm.净方向,
            "强度": round(sm.净强度, 2),
            "利空条数": sm.利空条数,
            "利好条数": sm.利好条数,
            "thermometer确认": c,
            "规避置信": conf,
            "置信依据": why,
            "证据": sm.证据,
        })

    advisory = {
        "date": date,
        "非validated": True,
        "non_gating": True,
        "主题": list(THEMES.keys()),
        "命中消息数": sum(1 for m in msgs if match_themes(m)),
        "总消息数": len(msgs),
        "板块规避": 板块规避,
        "note": ("forward-shadow advisory；未 validated 前不进任何生产选股决策；"
                 "样本 <120 只报 N；三主题判据预注册·只读不改"),
    }
    path = os.path.join(out_dir, f"{date}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(advisory, f, ensure_ascii=False, indent=2)
    logger.info("sector_macro_avoid %s：命中消息 %d，规避板块 %d",
                date, advisory["命中消息数"], len(板块规避))
    return advisory


# ---------- 31 天窗口方向性 sanity（只报方向·不作 validate）----------
def _board_forward_return(industry: str, date: str, N: int, close_cache: dict):
    """申万一级板块指数 date→date+N 交易日绝对收益%（因果，读 board_kline）；无效 → None。"""
    if industry not in close_cache:
        try:
            from tools.analysis.industry_temp import thermometer as th
            close_cache[industry] = th._board_close(industry)   # 复用 regime 板块层
        except Exception:  # noqa: BLE001
            close_cache[industry] = None
    close = close_cache[industry]
    if close is None or close.empty:
        return None
    idxs = [i for i, d in enumerate(close.index) if d <= date]
    if not idxs:
        return None
    t = idxs[-1]
    if close.index[t] != date or t + N >= len(close):
        return None
    c0, cN = float(close.iloc[t]), float(close.iloc[t + N])
    if c0 <= 0:
        return None
    return (cN / c0 - 1.0) * 100.0


def sanity_31d(data_root: str | None = None, horizons=(1, 5)) -> dict:
    """对 sentiment_policy 现有全部历史日做方向性检验：宏观利空命中板块在该窗口是否真跑输。

    只报方向（利空板块 next-N 收益 vs 当日全板块均值），**不作 validate**。样本不足如实报 N。"""
    try:
        from tools.analysis.market_forecast.dataroot import ensure_data_root
        ensure_data_root(data_root)   # board_kline 需先设数据根
    except Exception:  # noqa: BLE001
        pass
    base = os.path.join(_resolve_root(data_root), "analysis")
    dates = sorted(d for d in (os.listdir(base) if os.path.isdir(base) else [])
                   if os.path.exists(os.path.join(base, d, "sentiment_policy.json")))
    close_cache: dict = {}
    per_day = []
    # 三个桶的 forward 收益（跨全部日 pool）：
    #   avoid  = 净方向利空板块（= 生产规避触发口径，净强度<0）
    #   bearish= 有≥1条利空命中的板块（消息级方向性 lens，不受净washout；sanity 用）
    #   all    = 全部三主题命中板块（对照基准）
    pooled = {N: {"avoid": [], "bearish": [], "all": []} for N in horizons}
    for date in dates:
        agg = aggregate_macro(load_policy(date, data_root))
        if not agg:
            continue
        avoid = [i for i, sm in agg.items() if sm.净强度 < 0]
        bearish = [i for i, sm in agg.items() if sm.利空条数 > 0]
        allhit = list(agg.keys())
        row = {"date": date, "n_avoid": len(avoid), "n_bearish": len(bearish), "n_hit": len(allhit)}
        for N in horizons:
            for key, inds in (("avoid", avoid), ("bearish", bearish), ("all", allhit)):
                vals = [r for r in (_board_forward_return(i, date, N, close_cache) for i in inds)
                        if r is not None]
                pooled[N][key].extend(vals)
                row[f"{key}_r{N}"] = round(sum(vals) / len(vals), 3) if vals else None
        per_day.append(row)
    summary = {}
    for N in horizons:
        p = pooled[N]
        def _m(xs):
            return round(sum(xs) / len(xs), 3) if xs else None
        avm, bem, alm = _m(p["avoid"]), _m(p["bearish"]), _m(p["all"])
        summary[f"r{N}"] = {
            "n_avoid_obs": len(p["avoid"]), "avoid_mean%": avm,
            "n_bearish_obs": len(p["bearish"]), "bearish_mean%": bem,
            "n_allhit_obs": len(p["all"]), "allhit_mean%": alm,
            "净利空跑输幅度pp": (round(avm - alm, 3) if (avm is not None and alm is not None) else None),
            "有利空命中跑输幅度pp": (round(bem - alm, 3) if (bem is not None and alm is not None) else None),
        }
    return {
        "窗口": f"{dates[0]}..{dates[-1]}" if dates else "空",
        "n_days": len(per_day),
        "方向性结论": ("跑输幅度 <0 → 利空板块方向一致（真跑输）；仅方向参考·不作 validate。"
                   "净利空(生产触发口径)在本窗口 N 可能为 0（消息利好偏置），另给'有利空命中'消息级 lens。"),
        "汇总": summary,
        "per_day": per_day,
    }


# ---------- CLI（每日 shadow runner 入口，供 launchd 调度）----------
DEFAULT_OUT_DIR = "data/shadow_forward/sector_macro_avoid"


def main(argv: list[str] | None = None) -> int:
    """每日 shadow runner：宏观三主题板块规避 advisory（non-gating）。
    退出码：0=成功/非交易日跳过/幂等跳过；非0=当日无 sentiment_policy（数据缺）。"""
    import argparse
    from datetime import datetime

    from tools.collectors import calendar as cal

    ap = argparse.ArgumentParser(
        description="C 宏观/板块级消息规避 forward-shadow runner（纯记录·non-gating）")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD，缺省今天")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="advisory 落盘目录")
    ap.add_argument("--data-root", default=None, help="缺省 data/（sentiment_policy 与 thermometer 探测）")
    ap.add_argument("--force", action="store_true", help="覆盖当日已有 advisory")
    ap.add_argument("--sanity", action="store_true", help="跑 31 天窗口方向性 sanity（打印，不落 shadow）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.sanity:
        res = sanity_31d(args.data_root)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    today = datetime.now().strftime("%Y-%m-%d")
    date = args.date or today

    if not cal.is_trading_day(date):
        logger.info("%s 非交易日，跳过 shadow", date)
        return 0

    out_path = os.path.join(args.out_dir, f"{date}.json")
    if os.path.exists(out_path) and not args.force:
        logger.info("%s advisory 已存在（幂等跳过，--force 覆盖）：%s", date, out_path)
        return 0

    msgs = load_policy(date, args.data_root)
    if not msgs:
        logger.error("%s 无 sentiment_policy（数据缺），无法出 shadow", date)
        return 2

    adv = run_daily_shadow(date, args.out_dir, args.data_root)
    logger.info("shadow 落盘 %s：规避板块 %d → %s", date, len(adv["板块规避"]), out_path)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
