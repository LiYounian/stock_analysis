"""gate_tool（①塔基·分层闸门）· P1 窗1。

对单票判 T1/T2/T3 分层闸门，阈值**写死**（参考 tools/experimental/pyramid_select_v1.py 的口径）。
输出：层级 + 每条闸门 pass/fail + 触发原因。阈值加档位语义锁测试。

铁律：作用于 as_of 当日已实现 bar；688/689 量能由 _common.load_kline 自校（H2）；防未来 assert。
涨停 / 极高位(pos60≥0.95) → 踢出（先于分层判定）。
"""
from __future__ import annotations

from typing import Optional

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import load_kline, volume_ratio, pos60, 浓缩块

# ── 阈值（写死·语义锁测试锁死）──
_TH = {
    "T1量比": 1.5,
    "T2量比": 1.2,
    "T3量比": 1.0,
    "极高位pos60": 0.95,   # pos60 ≥ 此值 → 极高位踢出
    "涨停_主板": 9.7,      # 涨跌幅% ≥ 此值 → 涨停（60/00 主板口径）
    "涨停_20cm": 19.5,     # 300/301/688/689 二十厘米板
    "近涨停缓冲": 0.3,     # 涨幅 ≥ 涨停线-缓冲 → 近涨停（T1 需未近涨停）
}


def _is_20cm(code: str) -> bool:
    return str(code).startswith(("300", "301", "688", "689"))


class GateTool:
    name = "gate"
    塔层 = "①塔基"
    source = "主档 K 线分层闸门（阈值写死·pyramid_select_v1 口径）"

    def run(
        self,
        as_of: str,
        code: Optional[str] = None,
        root: Optional[str] = None,
        **kw,
    ) -> ToolResult:
        if not code:
            raise ValueError("gate 需 --code")
        df = load_kline(code, as_of, root=root, min_bars=20)
        if df is None or len(df) < 20:
            return ToolResult(
                name=self.name,
                塔层=self.塔层,
                as_of=as_of,
                code=code,
                浓缩块="闸门: 数据不足(K线<20根或缺失)·人工确认",
                fields={"数据不足": True},
                freshness="missing",
                防未来=True,
                source=self.source,
            )
        close = df["close"]
        open_ = df["open"]
        现价 = float(close.iloc[-1])
        prev_close = float(close.iloc[-2])
        prev_high = float(df["high"].iloc[-2])
        ma5 = float(close.tail(5).mean())
        涨幅 = (现价 / prev_close - 1) * 100 if prev_close else 0.0
        vr = volume_ratio(df)
        p60 = pos60(df)
        涨停线 = _TH["涨停_20cm"] if _is_20cm(code) else _TH["涨停_主板"]

        # ── 各闸门 ──
        收阳 = 现价 > float(open_.iloc[-1])
        站上MA5 = 现价 > ma5
        站上前日高 = 现价 > prev_high
        涨停 = 涨幅 >= 涨停线
        近涨停 = 涨幅 >= 涨停线 - _TH["近涨停缓冲"]
        极高位 = p60 is not None and p60 >= _TH["极高位pos60"]
        vr值 = vr if vr is not None else 0.0

        # ── 分层（踢出先于 T1/T2/T3）──
        if 涨停 or 极高位:
            层级 = "踢出"
            reason = ("涨停不可追" if 涨停 else "") + ("|" if (涨停 and 极高位) else "") + (
                f"极高位pos60={p60:.2f}≥{_TH['极高位pos60']}" if 极高位 else ""
            )
        elif 收阳 and 站上MA5 and 站上前日高 and vr值 >= _TH["T1量比"] and not 近涨停:
            层级 = "T1"
            reason = f"量比{vr值:.2f}≥{_TH['T1量比']}∧收阳∧站上MA5∧破前高∧未近涨停"
        elif 收阳 and 站上MA5 and vr值 >= _TH["T2量比"]:
            层级 = "T2"
            reason = f"量比{vr值:.2f}≥{_TH['T2量比']}∧收阳∧站上MA5（未破前高或量比不足T1）"
        elif (收阳 and 站上MA5) or vr值 >= _TH["T3量比"]:
            层级 = "T3"
            reason = "收阳∧站上MA5" if (收阳 and 站上MA5) else f"量比{vr值:.2f}≥{_TH['T3量比']}"
        else:
            层级 = "未入层"
            reason = "收阴或跌破MA5且量比<T3量比"

        def m(b: bool) -> str:
            return "✓" if b else "✗"

        vr文 = f"{vr:.2f}" if vr is not None else "NA"
        p60文 = f"{p60:.2f}" if p60 is not None else "NA"
        lines = [
            f"层级: {层级}【T1/T2/T3分层闸门·阈值写死】{reason}",
            f"闸门: 收阳{m(收阳)} 站上MA5{m(站上MA5)} 站上前日高{m(站上前日高)}【收盘价形态】",
            f"量比: {vr文}（T1≥{_TH['T1量比']}/T2≥{_TH['T2量比']}/T3≥{_TH['T3量比']}）【当日量/前5日均量】",
            f"排雷: 涨停{m(not 涨停)}未触 近涨停{m(not 近涨停)}未触 极高位pos60={p60文}(≥{_TH['极高位pos60']}踢出)",
            "口径: 收盘已实现bar·688/689量能自校·防未来as_of",
        ]
        return ToolResult(
            name=self.name,
            塔层=self.塔层,
            as_of=as_of,
            code=code,
            浓缩块=浓缩块(lines),
            fields={
                "层级": 层级,
                "触发原因": reason,
                "收阳": bool(收阳),
                "站上MA5": bool(站上MA5),
                "站上前日高": bool(站上前日高),
                "涨停": bool(涨停),
                "近涨停": bool(近涨停),
                "极高位": bool(极高位),
                "量比": round(vr, 2) if vr is not None else None,
                "pos60": round(p60, 3) if p60 is not None else None,
                "涨幅pct": round(涨幅, 2),
            },
            freshness="fresh",
            防未来=True,
            source=self.source,
        )


register(GateTool())
