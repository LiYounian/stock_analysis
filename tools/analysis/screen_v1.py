"""V1 模块二·图形审美选股:硬规则 AND 达标判定 + 护栏 + 全市场达标占比。

达标(硬规则 AND,任一不满足即出局)= **形态完成 + RS 达标 + 量能配合 + 通过护栏**。
复用 `analysis/pattern`(形态)、`analysis/rs`(相对强度);护栏借估值/公告思路。
产出:每票达标结果 + 全市场「达标占比」(F2.6,供模块一当宽度信号)。
参数走 Config `strategy.THRESHOLDS["V1形态选股"]`。
需求见 docs/计划/V1_形态选股与市场状态系统.md F2.3/F2.4/F2.6。
"""
from __future__ import annotations

from tools.analysis import pattern, rs
from tools.config.strategy import THRESHOLDS

_CFG = THRESHOLDS["V1形态选股"]


# ———————————————————— 护栏(F2.3,仅负向剔除)————————————————————
def guardrail(pe_percentile: float | None, net_profit_growth: float | None,
              ann_titles: list[str] | None = None, cfg: dict = None) -> tuple[bool, list[str]]:
    """三条负向护栏(各有开关):PE分位极端 / 净利增速为负 / 近期合规风险公告。

    返回 (是否通过, 剔除原因列表)。缺数据(None)不主动剔除(避免误杀)。
    """
    g = (cfg or _CFG)["护栏"]
    reasons: list[str] = []
    if g.get("启用PE护栏") and pe_percentile is not None and pe_percentile > g["PE分位剔除"]:
        reasons.append(f"PE分位{pe_percentile:.2f}>{g['PE分位剔除']}(极度高估)")
    if g.get("启用净利增速护栏") and net_profit_growth is not None \
            and net_profit_growth < g["净利增速下限%"]:
        reasons.append(f"净利增速{net_profit_growth}%<{g['净利增速下限%']}%")
    if g.get("启用合规护栏") and ann_titles:
        hits = sorted({kw for t in ann_titles for kw in g["合规风险关键词"] if kw in (t or "")})
        if hits:
            reasons.append("合规风险:" + ",".join(hits))
    return (len(reasons) == 0, reasons)


# ———————————————————— 量能配合 ————————————————————
def volume_ok(kline_df, cfg: dict = None) -> bool:
    """末根量 > 前 5 日均量 × 突破放量倍数。"""
    c = (cfg or _CFG)["量能"]
    v = kline_df["volume"]
    if len(v) < 6:
        return False
    base = v.iloc[-6:-1].mean()
    return bool(base > 0 and v.iloc[-1] > base * c["突破放量倍数"])


# ———————————————————— 硬规则 AND 达标(F2.4)————————————————————
def is_qualified(kline_df, rs_stock_vs_board: float, rs_board_vs_hs300: float,
                 pe_percentile: float | None = None, net_profit_growth: float | None = None,
                 ann_titles: list[str] | None = None, cfg: dict = None) -> dict:
    """单票硬规则 AND 达标判定。四项全过才达标;不达标给可追溯剔除原因。"""
    cfg = cfg or _CFG
    pat = pattern.detect(kline_df, cfg)
    form_ok = pat["达标"]
    rs_ok = rs.is_strong(rs_stock_vs_board, "个股vs板块") and \
        rs.is_strong(rs_board_vs_hs300, "板块vs沪深300")
    vol_ok = volume_ok(kline_df, cfg)
    grd_ok, grd_reasons = guardrail(pe_percentile, net_profit_growth, ann_titles, cfg)

    reasons: list[str] = []
    if not form_ok:
        reasons.append("无形态命中")
    if not rs_ok:
        reasons.append("RS不达标")
    if not vol_ok:
        reasons.append("量能不配合")
    reasons += grd_reasons

    qualified = bool(form_ok and rs_ok and vol_ok and grd_ok)
    return {"达标": qualified, "命中形态": pat["命中形态"],
            "各项": {"形态": form_ok, "RS": rs_ok, "量能": vol_ok, "护栏": grd_ok},
            "剔除原因": reasons}


# ———————————————————— 全市场达标占比(F2.6)————————————————————
def market_breadth(results: dict[str, dict]) -> dict:
    """由全市场逐票 is_qualified 结果算达标占比。供模块一当宽度信号。

    results: {code: is_qualified(...) 输出}。有效样本=能判定的票;占比=达标数/有效样本。
    """
    valid = {c: r for c, r in results.items() if r and "达标" in r}
    hit = sorted(c for c, r in valid.items() if r.get("达标"))
    n = len(valid)
    return {"有效样本": n, "达标数": len(hit),
            "达标占比": round(len(hit) / n, 4) if n else 0.0,
            "达标清单": hit}
