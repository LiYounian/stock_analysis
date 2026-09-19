"""fund_flow（①塔基·资金面）· 体检卡四面重构 Wave2 · 资金面填充工具。

金字塔缺"资金面"整面：主力资金进出、主买盘强弱、股东户数(筹码集中/涣散)、换手率
(过低关注不足/过热炒作)、龙虎榜席位方向，以及**竞品=板块内相对**(同板块换手排名 + vs
龙头/板块均值)。本工具读现成 per-stock json + 主档 K 线，按"口径三段"(名/值/口径/意味)产出
资金面 字段解读，供 d2_package 四面重排与选股 LLM 消费。设计依据：docs/计划/
2026-09-19_体检卡四面重构_口径贯通设计.md §2c/§2d/§3b/§4b（竞品=板块内相对·用户已拍板）。

数据源（防未来·只用 as_of 当日及之前）：
  - data/analysis/<as_of>/<code>.json 的 fundflow / tick / holder / lhb_veto / financing。
  - 主档 K 线 turnover 列（as_of 当日换手率）；净占比缺失时 amount 折算（口径日期成交额）。
  - config/code_industry.json（code→申万一级·单一真源）+ data/sector_roster/<板块>.json（竞品对照）。

口径贯通（Wave1 契约）：各维档位阈值一律写死在本文件常量（语义锁测试锁死），
经 _common.字段() 原样传上 ToolResult.字段解读，拼装层零加工。缺数据→值 NA，绝不编。

诚实边界（口径注明）：
  - 净占比 缺失率高（生产实测 ~57%）→ 按同定义折算(净流入/口径日成交额)，口径明标"折算"+新鲜度。
  - 换手率 单表·创业/科创天然偏高，意味段注明。
  - 两融：financing 无结构化 margin 字段（collectors.margin 采集未落 record）→ 标"待补落盘"，不动 collectors。
  - 竞品：板块内 = roster 精选成分(~18~20名)池内相对，非全行业；换手对照为 roster 快照口径。
    仅 10 个申万一级有 roster，无 roster / code_industry 未含 → NA 不编。
"""
from __future__ import annotations

from typing import Optional, Any
import json
import os

import pandas as pd

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import data_root, load_kline, 格档, 字段

_亿 = 1e8

# ── 档位阈值（写死·语义锁测试锁死；格档 = value ≤ 上界 命中，升序）──────────────
# A. 主力净占比%（净额/成交额；实测 p10=-6.2/p50=1.65/p90=14.7；对称·±1中性带·±5强档）
_净占比档 = [
    (-5.0, "强流出", "主力大幅净流出"),
    (-1.0, "流出", "主力净流出"),
    (1.0, "中性", "主力进出均衡"),
    (5.0, "流入", "主力净流入"),
    (float("inf"), "强流入", "主力大幅净流入"),
]
# B. 主力连续净流入天数（int；实测 p75=2/p90=4）
_连续天数档 = [
    (0, "无", "当日未连续净流入"),
    (2, "短", "连续净流入1-2日"),
    (5, "持续", "连续净流入3-5日"),
    (float("inf"), "强持续", "连续净流入>5日"),
]
# C. tick 主买占比（0~1；实测 p25=.465/p50=.508/p75=.546/p90=.585）
_主买占比档 = [
    (0.45, "弱", "卖压主导"),
    (0.52, "均衡", "买卖均衡"),
    (0.56, "偏强", "买盘偏强"),
    (float("inf"), "强", "买盘主导"),
]
# D. 股东户数环比%（负=筹码集中·正向；实测 p10=-15.4/p50=+0.5/p75=+15.8）
_户数环比档 = [
    (-10.0, "明显集中", "户数大降·筹码明显集中"),
    (-3.0, "集中", "户数下降·筹码集中"),
    (3.0, "平稳", "户数基本平稳"),
    (10.0, "分散", "户数上升·筹码分散"),
    (float("inf"), "明显分散", "户数大增·筹码涣散"),
]
# E. 连续减少期数（int；实测 p75=1/p90=3）
_连减期数档 = [
    (0, "无", "未连续减少"),
    (2, "初步集中", "连续减少1-2期"),
    (5, "持续集中", "连续减少3-5期"),
    (float("inf"), "强持续集中", "连续减少>5期"),
]
# F. 换手率%（as_of K线；A股惯例·创业/科创天然偏高见意味）
_换手率档 = [
    (1.0, "过低", "交投清淡·关注不足"),
    (3.0, "正常", "换手正常"),
    (7.0, "活跃", "交投活跃"),
    (float("inf"), "过热炒作", "换手过高·炒作/接力风险"),
]
# H. 竞品·板块内相对（按换手降序 rank/n 分位，pr 越小换手越靠前）
_竞品档 = [
    (0.2, "龙头级", "换手居板块前20%"),
    (0.4, "前排", "换手居板块前20~40%"),
    (0.7, "中游", "换手居板块中游"),
    (float("inf"), "尾部", "换手居板块尾部"),
]


# ── 数据装配 ─────────────────────────────────────────────────────────
def _pstock_path(root: Optional[str], as_of: str, code: str) -> str:
    return os.path.join(data_root(root), "data", "analysis", as_of, f"{code}.json")


def _load_pstock(root: Optional[str], as_of: str, code: str) -> Optional[dict]:
    p = _pstock_path(root, as_of, code)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _code_industry(root: Optional[str]) -> dict:
    """读 config/code_industry.json（code→申万一级·单一真源）。缺失返回 {}。"""
    p = os.path.join(data_root(root), "config", "code_industry.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _turnover_asof(code: str, as_of: str, root: Optional[str]) -> Optional[float]:
    """as_of 当日换手率%（主档 K 线 turnover 列末行·防未来）。"""
    df = load_kline(code, as_of, root=root, min_bars=1)
    if df is None or "turnover" not in df.columns or not len(df):
        return None
    v = df["turnover"].iloc[-1]
    return float(v) if pd.notna(v) else None


def _amount_on(code: str, date: Optional[str], as_of: str, root: Optional[str]) -> Optional[float]:
    """口径日期当日成交额（元）·用于净占比折算。date 须 ≤ as_of（防未来）。"""
    if not date:
        return None
    df = load_kline(code, as_of, root=root, min_bars=1)
    if df is None or "amount" not in df.columns:
        return None
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    row = d[d["date"] == pd.to_datetime(str(date)[:10])]
    if not len(row):
        return None
    v = row["amount"].iloc[0]
    return float(v) if pd.notna(v) else None


def _roster_pool(sector: str, root: Optional[str]) -> tuple[dict, list, Optional[str]]:
    """读 sector_roster/<板块>.json 全 roles 池换手快照。

    返回 (成分换手 {code:换手}, 龙头组换手 list, roster更新日)。无文件 → ({}, [], None)。
    """
    p = os.path.join(data_root(root), "data", "sector_roster", f"{sector}.json")
    if not os.path.exists(p):
        return {}, [], None
    try:
        with open(p, "r", encoding="utf-8") as f:
            r = json.load(f)
    except Exception:
        return {}, [], None
    roles = r.get("roles") or {}
    pool: dict[str, float] = {}
    lead: list[float] = []
    for role, lst in roles.items():
        if not isinstance(lst, list):
            continue
        for e in lst:
            c = str(e.get("code"))
            t = e.get("换手")
            if isinstance(t, (int, float)):
                pool[c] = float(t)  # 同码多角色去重（后者覆盖·值相同）
                if role == "龙头":
                    lead.append(float(t))
    return pool, lead, r.get("更新日")


# ── 各维字段构造（口径三段·经 字段() 强制不空编）─────────────────────────
def _f_主力(fundflow: dict, code: str, as_of: str, root: Optional[str]) -> dict:
    z = fundflow.get("今日主力净占比")
    ni = fundflow.get("今日主力净流入")
    days = fundflow.get("主力连续净流入天数")
    stale = str(fundflow.get("新鲜度") or "") == "陈旧"
    口径日 = fundflow.get("口径日期")
    折算注 = ""
    if z is None and isinstance(ni, (int, float)):
        amt = _amount_on(code, 口径日, as_of, root)
        if amt:
            z = ni / amt * 100.0
            折算注 = "·折算(净流入/当日成交额)"
    档, 释 = 格档(float(z), _净占比档) if isinstance(z, (int, float)) else ("NA", "净占比缺且不可折算")
    d档, d释 = 格档(int(days), _连续天数档) if isinstance(days, (int, float)) else ("NA", "")
    z_txt = f"净占比{z:+.2f}%" if isinstance(z, (int, float)) else "净占比NA"
    ni_txt = f"净流入{ni / _亿:+.2f}亿" if isinstance(ni, (int, float)) else "净流入NA"
    days_txt = f"连{int(days)}d" if isinstance(days, (int, float)) else "连NA"
    新鲜注 = f"·口径{str(口径日)[:10]}陈旧" if stale else ""
    return 字段(
        名="主力资金",
        值=f"{z_txt}·{ni_txt}·{days_txt}",
        口径=f"强流出≤-5/流出/中性(-1,1]/流入/强流入>5(%){折算注}{新鲜注}",
        意味=f"主力{档}·{释}；连续净流入{d档}" if 档 != "NA" else f"主力净占比缺·仅净流入参考；连续净流入{d档}",
    )


def _f_主买(tick: dict) -> dict:
    mb = tick.get("主买占比")
    大单 = tick.get("大单笔数")
    档, 释 = 格档(float(mb), _主买占比档) if isinstance(mb, (int, float)) else ("NA", "主买占比缺")
    mb_txt = f"主买占比{mb * 100:.1f}%" if isinstance(mb, (int, float)) else "主买占比NA"
    大单_txt = f"·大单{int(大单)}笔" if isinstance(大单, (int, float)) else ""
    return 字段(
        名="主买盘",
        值=f"{mb_txt}{大单_txt}",
        口径="弱≤0.45/均衡(0.45,0.52]/偏强(0.52,0.56]/强>0.56(主动买占比)",
        意味=f"盘中主动买盘{档}·{释}" if 档 != "NA" else "主买占比缺·盘口强弱不可判",
    )


def _f_户数(holder: dict) -> dict:
    hb = holder.get("户数环比")
    ls = holder.get("连续减少期数")
    stale = str(holder.get("新鲜度") or "") == "陈旧"
    口径日 = holder.get("口径日期")
    档, 释 = 格档(float(hb), _户数环比档) if isinstance(hb, (int, float)) else ("NA", "户数环比缺")
    l档, l释 = 格档(int(ls), _连减期数档) if isinstance(ls, (int, float)) else ("NA", "")
    hb_txt = f"户数环比{hb:+.2f}%" if isinstance(hb, (int, float)) else "户数环比NA"
    ls_txt = f"·连减{int(ls)}期" if isinstance(ls, (int, float)) else ""
    新鲜注 = f"·口径{str(口径日)[:10]}陈旧" if stale else ""
    return 字段(
        名="股东户数",
        值=f"{hb_txt}{ls_txt}",
        口径=f"负=筹码集中：明显集中≤-10/集中/平稳(-3,3]/分散/明显分散>10(%){新鲜注}",
        意味=f"筹码{档}·{释}" + (f"；{l档}" if l档 != "NA" else "") if 档 != "NA" else "户数环比缺·筹码集中度不可判",
    )


def _f_换手(turnover: Optional[float], code: str) -> dict:
    档, 释 = 格档(float(turnover), _换手率档) if isinstance(turnover, (int, float)) else ("NA", "K线换手缺")
    t_txt = f"{turnover:.2f}%" if isinstance(turnover, (int, float)) else "NA"
    板注 = "·创业/科创天然偏高" if code.startswith(("30", "688", "689")) else ""
    return 字段(
        名="换手率",
        值=t_txt,
        口径=f"过低≤1/正常(1,3]/活跃(3,7]/过热炒作>7(%·as_of当日){板注}",
        意味=f"交投{档}·{释}" if 档 != "NA" else "换手缺·活跃度不可判",
    )


def _f_龙虎榜(lhb: dict) -> dict:
    """龙虎榜=只读 surface（不重算 veto）：状态档 近期无上榜/净买上榜/净卖上榜/否决候选。"""
    triggered = bool(lhb.get("triggered"))
    direction = lhb.get("direction")
    n_recent = lhb.get("n_recent")
    nbr = lhb.get("net_buy_ratio")
    reason = lhb.get("reason")
    if triggered:
        档 = "龙虎榜否决候选"
        释 = f"触发entry_veto·{reason or ''}".rstrip("·")
    elif isinstance(direction, (int, float)) and direction > 0:
        档, 释 = "净买上榜", "近期龙虎榜净买(游资/机构进场)"
    elif isinstance(direction, (int, float)) and direction < 0:
        档, 释 = "净卖上榜", "近期龙虎榜净卖(抛压)"
    else:
        档, 释 = "近期无上榜", reason or "近7日无净买上榜"
    nbr_txt = f"·净买占比{nbr:.2f}" if isinstance(nbr, (int, float)) else ""
    n_txt = f"·近{int(n_recent)}次" if isinstance(n_recent, (int, float)) and n_recent else ""
    return 字段(
        名="龙虎榜",
        值=f"{档}{n_txt}{nbr_txt}",
        口径="状态档(只读·不重算veto)：近期无上榜/净买上榜/净卖上榜/龙虎榜否决候选",
        意味=释,
    )


def _f_竞品(code: str, sector: Optional[str], turnover: Optional[float],
           root: Optional[str], as_of: str) -> dict:
    """竞品=板块内相对（按换手降序）：板块内排名 + vs龙头均值 + vs板块均值。"""
    if not sector:
        return 字段(名="竞品·板块内相对", 值="NA",
                   口径="code_industry.json 未含该票·无法解析申万一级",
                   意味="板块归属缺·竞品相对不可算")
    pool, lead, 更新日 = _roster_pool(sector, root)
    if not pool:
        return 字段(名="竞品·板块内相对", 值="NA",
                   口径=f"{sector} 无 sector_roster（仅10个申万一级有roster）",
                   意味="该板块无roster·竞品相对不可算")
    tt = pool.get(code)
    源注 = "roster快照"
    if tt is None:
        tt = turnover
        源注 = "as_of当日K线"
    if tt is None:
        return 字段(名="竞品·板块内相对", 值="NA",
                   口径=f"{sector} roster成分{len(pool)}名·目标换手缺",
                   意味="目标换手缺·板块内相对不可算")
    vals = list(pool.values())
    if code not in pool:
        vals = vals + [tt]
    n = len(vals)
    rank = sum(1 for x in vals if x > tt) + 1
    pr = rank / n
    档, 释 = 格档(pr, _竞品档)
    板均 = sum(pool.values()) / len(pool)
    龙头均 = sum(lead) / len(lead) if lead else None
    lead_txt = f"·{tt / 龙头均:.2f}x龙头" if 龙头均 else ""
    avg_txt = f"·{tt / 板均:.2f}x板均" if 板均 else ""
    鲜注 = f"·roster{str(更新日)[:10]}" if 更新日 else ""
    return 字段(
        名="竞品·板块内相对",
        值=f"第{rank}/{n}【{档}】{lead_txt}{avg_txt}",
        口径=f"按换手降序rank：龙头级≤20%/前排/中游/尾部>70%；板块内=roster精选{len(pool)}名非全行业·换手{源注}{鲜注}",
        意味=f"换手活跃度板块{档}·{释}",
    )


def _f_两融() -> dict:
    """两融：financing 无结构化 margin 字段（采集未落 record）→ 待补落盘·不动 collectors。"""
    return 字段(
        名="两融",
        值="NA",
        口径="两融余额=collectors.margin 已采但未落 record（另一chip补落盘）",
        意味="两融维度暂缺·待补落盘后接入",
    )


# ── 工具 ─────────────────────────────────────────────────────────────
class FundFlowTool:
    name = "fund_flow"
    塔层 = "①塔基"  # 资金量能=选股地基（与 price_volume/gate 同族）
    面 = "资金面"
    source = ("per-stock json fundflow/tick/holder/lhb_veto/financing + 主档K线turnover"
              " + code_industry→sector_roster(竞品板块内相对)")

    def run(self, as_of: str, code: Optional[str] = None,
            root: Optional[str] = None, **kw) -> ToolResult:
        if not code:
            raise ValueError("fund_flow 需 --code")
        code = str(code)
        d = _load_pstock(root, as_of, code)
        if d is None:
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
                浓缩块="资金面: 数据缺失（无 per-stock json·不编造）",
                fields={"数据缺": True}, freshness="missing", 防未来=True,
                面=self.面, source=self.source,
            )
        fundflow = d.get("fundflow") or {}
        tick = d.get("tick") or {}
        holder = d.get("holder") or {}
        lhb = d.get("lhb_veto") or {}
        turnover = _turnover_asof(code, as_of, root)
        sector = _code_industry(root).get(code)

        字段解读 = [
            _f_主力(fundflow, code, as_of, root),
            _f_主买(tick),
            _f_户数(holder),
            _f_换手(turnover, code),
            _f_龙虎榜(lhb),
            _f_竞品(code, sector, turnover, root, as_of),
            _f_两融(),
        ]
        # 机读 fields（供回测/审计；不进 prompt）
        fields: dict[str, Any] = {
            "今日主力净占比": fundflow.get("今日主力净占比"),
            "今日主力净流入": fundflow.get("今日主力净流入"),
            "近5日主力合计": fundflow.get("近5日主力合计"),
            "主力连续净流入天数": fundflow.get("主力连续净流入天数"),
            "主买占比": tick.get("主买占比"),
            "户数环比": holder.get("户数环比"),
            "连续减少期数": holder.get("连续减少期数"),
            "换手率": turnover,
            "龙虎榜triggered": bool(lhb.get("triggered")),
            "龙虎榜direction": lhb.get("direction"),
            "板块": sector,
        }
        stale = str(fundflow.get("新鲜度") or "") == "陈旧"
        return ToolResult(
            name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
            浓缩块="",  # __post_init__ 由 字段解读 自动派生（展示=传输同源）
            fields=fields, freshness=("stale" if stale else "fresh"),
            防未来=True, 面=self.面, source=self.source, 字段解读=字段解读,
        )


register(FundFlowTool())
