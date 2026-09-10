"""IET 行业面板构建:个股 → 申万一级聚合(持久 store kind=industry_panel)。

面板主键 (industry, date),**只存原始聚合值**(pe/pb 中位、换手均值),
分位在温度打分时因果 rolling 算(见 temperature.causal_rolling_pctile)——防未来函数。

⚠️ 成分股历史归属源(members_provider)是**待统筹拍板项**:
  - PIT 严谨源 industry_at → to_sw:防前视✅,但实测申万一级覆盖仅~6%(证监会门类粗桶+数据缺半),探针不可用。
  - 当前快照 code_industry → to_sw:覆盖 93.6%,但当前快照回填历史有前视(二阶,聚合到行业中位后稀释)。
  两个 provider 都在此备好;实际用哪个由统筹/用户拍板后传入,未获批不擅自跑 snapshot 路径。

⚠️ 跨 worktree 共享写红线:探针写到独立命名空间(probe),别污染生产。见 store_probe_* 辅助。
"""
from __future__ import annotations

from statistics import median
from typing import Callable, Optional

import pandas as pd

from tools.analysis import industry_map

# 成分源 provider 签名:给定 date,返回 {code: 申万一级规范名}(已 to_sw 归一,归不动的 code 不出现在 dict 里)
MembersProvider = Callable[[str], dict]

# 面板字段(探针 3 聚合字段)
PANEL_COLS = ["date", "industry", "n_members", "pe_median", "pb_median", "turnover_mean"]


# ————————————————————— 纯聚合(可单测,无 IO) —————————————————————
def aggregate_industry(
    industry: str,
    date: str,
    codes: list[str],
    pe_map: dict,
    pb_map: dict,
    turnover_map: dict,
    *,
    min_members: int,
) -> Optional[dict]:
    """把一个行业当日成分股的个股量聚合成一行面板记录。

    - pe/pb:剔除 ≤0 / None / NaN 后取中位数(估值负值无意义)。
    - turnover:剔除 None/NaN 后取均值。
    - n_members = 有效成分股数;< min_members → 返回 None(该行业当日弃权,不硬填)。
    任一聚合量无有效样本 → 该量置 None(温度打分时该维弃权 → 整行业弃权)。
    """
    codes = [c for c in codes if c]
    n = len(codes)
    if n < min_members:
        return None

    def _clean_pos(m):
        return [v for c in codes for v in [m.get(c)]
                if v is not None and not pd.isna(v) and v > 0]

    def _clean(m):
        return [v for c in codes for v in [m.get(c)]
                if v is not None and not pd.isna(v)]

    pe_vals = _clean_pos(pe_map)
    pb_vals = _clean_pos(pb_map)
    turn_vals = _clean(turnover_map)
    return {
        "date": date,
        "industry": industry,
        "n_members": n,
        "pe_median": float(median(pe_vals)) if pe_vals else None,
        "pb_median": float(median(pb_vals)) if pb_vals else None,
        "turnover_mean": float(sum(turn_vals) / len(turn_vals)) if turn_vals else None,
    }


def aggregate_date(
    date: str,
    members: dict,
    pe_map: dict,
    pb_map: dict,
    turnover_map: dict,
    *,
    min_members: int,
) -> list[dict]:
    """一个交易日的全行业聚合。members: {code: 申万一级名}。返回 [面板行,...]。"""
    by_ind: dict[str, list[str]] = {}
    for code, ind in members.items():
        if ind:
            by_ind.setdefault(ind, []).append(code)
    rows = []
    for industry, codes in by_ind.items():
        row = aggregate_industry(
            industry, date, codes, pe_map, pb_map, turnover_map, min_members=min_members
        )
        if row is not None:
            rows.append(row)
    return rows


# ————————————————————— 成分源 providers(待拍板选用) —————————————————————
def members_pit(codes: list[str], date: str) -> dict:
    """PIT 严谨源:industry_at(code,date) 历史时点归属 → to_sw 申万一级。防前视✅。

    ⚠️ 实测覆盖仅~6%(证监会门类粗桶 to_sw 多返 None + industry_history 数据缺半),探针不可用。
    """
    from tools.collectors import industry_history

    out = {}
    for c in codes:
        try:
            raw = industry_history.industry_at(c, date)
        except Exception:
            raw = None
        sw = industry_map.to_sw(raw) if raw else None
        if sw:
            out[c] = sw
    return out


def members_snapshot(codes: list[str], _date: str, snapshot: dict) -> dict:
    """当前快照源:code_industry(baostock 现状) → to_sw。覆盖 93.6%,但回填历史有前视(caveat)。

    snapshot: {code: 行业名}(由调用方一次性 load,避免逐日重读)。日期参数忽略(快照恒定)。
    """
    out = {}
    for c in codes:
        raw = snapshot.get(c) or snapshot.get(c.split(".")[0])
        sw = industry_map.to_sw(raw) if raw else None
        if sw:
            out[c] = sw
    return out


# ————————————————————— store 读写(probe 命名空间,防污染生产) —————————————————————
_PROBE_INDUSTRY = "_probe"   # 面板落盘时的伪 code 前缀? 实际按 industry 名落,probe 版用独立 date 分区标注


def panel_to_frame(rows: list[dict]) -> pd.DataFrame:
    """[面板行] → 规范 DataFrame(列序固定,date 升序)。"""
    df = pd.DataFrame(rows, columns=PANEL_COLS)
    if not df.empty:
        df = df.sort_values(["industry", "date"]).reset_index(drop=True)
    return df
