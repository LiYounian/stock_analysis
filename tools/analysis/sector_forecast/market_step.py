"""大盘→板块两步框架 · 第一步:大盘/整体市场风险偏好档(产出 E 上半)。

**复用不重造**:大盘方向 = 已有 `market_forecast.json`(targets.hs300 + proxy 分歧 + breadth_snapshot),
不再自算大盘预测。本模块只把它**归纳成一个风险偏好档 {进攻/中性/防守}**供第二步板块加权用。

风险偏好档(规则,预注册):
  进攻 = 大盘 proxy 方向偏多 且 净广度 > 0(个股普涨基础)。
  防守 = proxy 偏空 或 净广度深度为负(< -0.4)或 涨停远少于跌停。
  中性 = 其余(含"权重搭台中小盘偏弱"的风格背离——大盘指数偏多但个股弱)。
诚实:proxy(个股β基准)优先于 hs300(权重股),避免"指数偏多误读成个股偏多"。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sector_forecast.market_step")

STEP_VERSION = "v1-2026-09-16"


def resolve_analysis_file(date: str, name: str) -> Optional[Path]:
    """找 data/analysis/<date>/<name>:先本仓(production=主仓),再回退主仓(worktree联调,只读)。
    按**文件**存在判定(worktree 的 analysis 目录可能已存在但缺该输入文件)。"""
    from tools.config import settings
    from tools.backtest.iet_probe.data import _MAIN
    for base in (settings.PROJECT_ROOT, _MAIN):
        p = Path(base) / "data" / "analysis" / date / name
        if p.exists():
            return p
    return None


def _load_market_forecast(date: str) -> Optional[dict]:
    p = resolve_analysis_file(date, "market_forecast.json")
    if not p:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def market_risk_appetite(date: str, *, macro: Optional[dict] = None) -> dict:
    """归纳大盘风险偏好档 = **广度 + 宏观净方向**(macro 来自 news_store 宏观研判)。

    宏观净方向对档位的修正(用户 P2 强化:第一步不只用广度):
      · 宏观偏空 → 档位下调一级(进攻→中性、中性→防守)。
      · 宏观偏多 且 广度不深弱 → 中性可上调进攻。
    缺 market_forecast → 中性 + 标数据缺(仍叠加宏观修正)。
    """
    macro = macro or {}
    宏观净 = macro.get("宏观净方向", "中性")
    mf = _load_market_forecast(date)
    if not mf:
        base = "中性"
        档 = _apply_macro(base, 宏观净)
        return {"风险偏好": 档, "依据": f"缺 market_forecast.json,广度降级中性;宏观净方向={宏观净}",
                "宏观净方向": 宏观净, "宏观情景": macro.get("宏观情景"),
                "数据缺": True, "version": STEP_VERSION}

    tgt = (mf.get("targets") or {}).get("hs300") or {}
    h1 = (tgt.get("horizons") or {}).get("1") or {}
    hs300_dir = h1.get("direction")
    bs = mf.get("breadth_snapshot") or {}
    net_adv = bs.get("net_adv")
    lu, ld = bs.get("limit_up"), bs.get("limit_down")
    分歧 = (mf.get("分歧标记") or {}).get("触发")

    # proxy 方向(个股β基准)优先:从分歧维度里取,缺则退回 hs300
    proxy_dir = None
    dims = ((mf.get("分歧标记") or {}).get("维度") or {})
    if "1" in dims:
        proxy_dir = dims["1"].get("proxy_direction")

    偏多 = (proxy_dir or hs300_dir) in ("偏多", "上行")
    偏空 = (proxy_dir or hs300_dir) in ("偏空", "下行")
    深度弱 = net_adv is not None and net_adv < -0.4
    涨停弱 = lu is not None and ld is not None and ld > lu * 1.5

    if 偏多 and (net_adv is None or net_adv > 0):
        base = "进攻"
    elif 偏空 or 深度弱 or 涨停弱:
        base = "防守"
    else:
        base = "中性"
    档 = _apply_macro(base, 宏观净, net_adv=net_adv)

    return {
        "风险偏好": 档, "广度档": base, "宏观净方向": 宏观净, "宏观情景": macro.get("宏观情景"),
        "hs300方向": hs300_dir, "proxy方向": proxy_dir,
        "净广度": net_adv, "涨停": lu, "跌停": ld, "风格背离": bool(分歧),
        "依据": f"广度档={base}(proxy/hs300={proxy_dir or hs300_dir}、净广度={net_adv}、涨停{lu}/跌停{ld}"
                + ("、权重搭台中小盘偏弱" if 分歧 else "") + f");宏观净方向={宏观净}→最终{档}",
        "数据缺": False, "version": STEP_VERSION,
    }


def _apply_macro(base: str, 宏观净: str, *, net_adv=None) -> str:
    """宏观净方向修正广度档。偏空下调一级;偏多且广度不深弱可上调。"""
    order = ["防守", "中性", "进攻"]
    i = order.index(base)
    if 宏观净 == "偏空":
        i = max(0, i - 1)
    elif 宏观净 == "偏多" and (net_adv is None or net_adv > -0.3):
        i = min(2, i + 1)
    return order[i]
