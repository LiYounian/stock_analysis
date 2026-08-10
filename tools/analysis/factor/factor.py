"""多因子·单票原始因子提取(F6)。

从中心记录(财务/估值块)+ K线 + 可选北向趋势,抽出各子指标的**原始值**(不做截面标准化)。
截面标准化/合成在 score.py(需全票池)。低换手因子、规避纯动量(A股动量弱/反转强,见调研 §类别1)。

子指标(键与 config THRESHOLDS["合议"]["多因子"]["因子"] 对齐):
  质量: ROE / 毛利率 / 负债率      —— record["fundamental"]
  价值: PE_TTM / PB               —— record["valuation"]
  低波: 年化波动率                —— kline 日收益 std × sqrt(244)
  成长: 净利增速                  —— record["fundamental"]
  股息: 股息率 = 每股股利(TTM现金分红,采集层baostock) / 最新收盘价 × 100
                                  —— 无分红票每股股利=0 → 股息率0(真 0);每股股利缺失→None(降级)
  资金流: 北向净流入趋势           —— 北向 5–10 日趋势(拿不到→None,I4 降级)

缺任一 → 该子指标 None(score.py 截面时跳过 None,并据齐全度降置信度)。
依赖方向:分析层,只读记录字段 + 算数;不 import store/web/采集。
"""
from __future__ import annotations

import math

from tools.config.strategy import THRESHOLDS

_CFG = THRESHOLDS["合议"]["多因子"]


def _num(v):
    """转 float;None/非数/NaN → None。"""
    try:
        if v is None:
            return None
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def annualized_vol(kline_df, win: int = None) -> float | None:
    """年化波动率 = 近 win 日日收益率 std × sqrt(244)。样本不足→None。"""
    win = int(win or _CFG["低波窗口"])
    if kline_df is None or "close" not in getattr(kline_df, "columns", []):
        return None
    closes = [float(x) for x in kline_df["close"].tolist()]
    if len(closes) < win + 1:
        return None
    seg = closes[-(win + 1):]
    rets = [seg[i] / seg[i - 1] - 1.0 for i in range(1, len(seg)) if seg[i - 1]]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return round(math.sqrt(var) * math.sqrt(244) * 100.0, 4)      # 百分数年化波动


def _latest_close(kline_df) -> float | None:
    """K线最新收盘价;无数据 → None。"""
    if kline_df is None or "close" not in getattr(kline_df, "columns", []):
        return None
    closes = kline_df["close"].tolist()
    if not closes:
        return None
    return _num(closes[-1])


def dividend_yield(每股股利, kline_df=None) -> float | None:
    """股息率(%)= 每股股利(TTM现金分红) / 最新收盘价 × 100。

    每股股利 None(采集缺失)/ 无收盘价 → None(缺失);每股股利=0(无分红)→ 0.0(真 0)。
    """
    dps = _num(每股股利)
    if dps is None:
        return None
    if dps == 0:
        return 0.0                                    # 无分红:真 0,非缺失
    close = _latest_close(kline_df)
    if not close or close <= 0:
        return None                                   # 无价格分母 → 无法算,降级缺失
    return round(dps / close * 100.0, 4)


def raw_factors(record: dict, kline_df=None, 北向净流入趋势=None) -> dict:
    """单票各子指标原始值 {子指标: float|None}。缺块降级 None,不抛。"""
    fund = (record or {}).get("fundamental") or {}
    val = (record or {}).get("valuation") or {}
    return {
        # 质量
        "ROE": _num(fund.get("ROE")),
        "毛利率": _num(fund.get("毛利率")),
        "负债率": _num(fund.get("负债率")),
        # 价值
        "PE_TTM": _num(val.get("pe_ttm")),
        "PB": _num(val.get("pb")),
        # 低波
        "年化波动率": annualized_vol(kline_df),
        # 成长
        "净利增速": _num(fund.get("净利增速")),
        # 股息:每股股利(TTM现金分红,采集层)/ 最新收盘价 × 100;无分红→0(真 0),缺采集→None
        "股息率": dividend_yield(fund.get("每股股利") if isinstance(fund, dict) else None, kline_df),
        # 资金流(北向趋势;拿不到 → None,I4 降级)
        "北向净流入趋势": _num(北向净流入趋势),
    }
