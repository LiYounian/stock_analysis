"""price_volume_tool（①塔基·量价快照）· P1 窗1。

从主档 K 线出：现价 / 涨幅 / 量比 / 位置(pos60) / 距60高 / vs MA5·MA20 + session 标记。
每个数值带【口径】【档位】【一句解释】；量比、pos60 档位表**写死**并加语义锁测试。

session：因走主档 K 线（已实现 bar），一律标「收盘」——午盘快照口径由另路快变源负责，本工具不碰。
688/689 量能由 _common.load_kline 反推自校（H2），本工具不再自乘除。
"""
from __future__ import annotations

from typing import Optional

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import load_kline, volume_ratio, pos60, 格档, 浓缩块

# ── 档位表（写死·语义锁测试锁死；升序，value ≤ 上界即命中，末档兜底）──
# 量比 = 当日量 / 前5日均量（不含当日）
量比档 = [
    (0.7, "缩量", "量能萎缩·承接偏弱"),
    (1.2, "平量", "量能与近期持平"),
    (2.5, "放量", "放量·资金关注度升"),
    (1e9, "爆量", "爆量·亢奋或出货存疑"),
]
# pos60 = 收盘在近60日[低,高]区间分位
pos60档 = [
    (0.3, "低", "低位·上方套牢盘轻"),
    (0.7, "中", "中枢区·多空相对均衡"),
    (1.01, "高", "高位·获利抛压偏重"),
]

# ── v2 影响模板（共性区间定义已进统一词表，此处只讲本股本值影响）──
_量比影响 = {
    "缩量": "承接偏弱、需量能配合", "平量": "量能平稳、中性",
    "放量": "资金关注度升、偏正", "爆量": "亢奋或出货存疑、需结合位置辨",
}
_pos60影响 = {
    "低": "低位·上方套牢盘轻、偏机会", "中": "中枢区·多空均衡",
    "高": "高位·获利抛压偏重、追高谨慎",
}


def _label_vs_ma(pct: float) -> str:
    """vs 均线的方位标签（非写死语义锁，仅方向提示）。"""
    if pct >= 3:
        return "强站上"
    if pct >= 0:
        return "站上"
    if pct >= -3:
        return "微跌破"
    return "跌破"


class PriceVolumeTool:
    name = "price_volume"
    塔层 = "①塔基"
    面 = "技术面"  # 量价快照（趋势/量能/位置）
    source = "主档 K 线（688/689 amount/close 自校）"

    def run(
        self,
        as_of: str,
        code: Optional[str] = None,
        root: Optional[str] = None,
        **kw,
    ) -> ToolResult:
        if not code:
            raise ValueError("price_volume 需 --code")
        df = load_kline(code, as_of, root=root, min_bars=20)
        if df is None or len(df) < 20:
            return ToolResult(
                name=self.name,
                塔层=self.塔层,
                as_of=as_of,
                code=code,
                浓缩块="量价: 数据不足(K线<20根或缺失)·人工确认",
                fields={"数据不足": True},
                freshness="missing",
                防未来=True,
                source=self.source,
            )
        close = df["close"]
        现价 = float(close.iloc[-1])
        prev_close = float(close.iloc[-2])
        涨幅 = (现价 / prev_close - 1) * 100 if prev_close else 0.0
        vr = volume_ratio(df)  # 可能 None
        p60 = pos60(df)  # 可能 None
        hi60 = float(df["high"].tail(60).max())
        距60高 = (现价 / hi60 - 1) * 100 if hi60 else 0.0
        ma5 = float(close.tail(5).mean())
        ma20 = float(close.tail(20).mean())
        vsma5 = (现价 / ma5 - 1) * 100 if ma5 else 0.0
        vsma20 = (现价 / ma20 - 1) * 100 if ma20 else 0.0

        vr档名, _ = 格档(vr, 量比档)
        p60档名, _ = 格档(p60, pos60档)
        vr文 = f"{vr:.2f}" if vr is not None else "NA"
        p60文 = f"{p60:.2f}" if p60 is not None else "NA"
        vr影响 = _量比影响.get(vr档名, "") if vr is not None else "量比缺"
        p60影响 = _pos60影响.get(p60档名, "") if p60 is not None else "位置缺"

        lines = [
            f"现价: {round(现价,3)}　涨幅: {涨幅:+.2f}%【主档K线收盘·较前收】",
            f"量比: {vr文}【{vr档名}】影响：{vr影响}",
            f"位置pos60: {p60文}【{p60档名}】影响：{p60影响}",
            f"距60高: {距60高:+.1f}%　vsMA5: {vsma5:+.1f}%({_label_vs_ma(vsma5)})　vsMA20: {vsma20:+.1f}%({_label_vs_ma(vsma20)})",
            "session: 收盘（走主档K线·已实现bar）　688/689量能已自校",
        ]
        return ToolResult(
            name=self.name,
            塔层=self.塔层,
            as_of=as_of,
            code=code,
            浓缩块=浓缩块(lines),
            fields={
                "现价": round(现价, 3),
                "涨幅pct": round(涨幅, 2),
                "量比": round(vr, 2) if vr is not None else None,
                "量比档": vr档名,
                "pos60": round(p60, 3) if p60 is not None else None,
                "pos60档": p60档名,
                "距60高pct": round(距60高, 2),
                "vsMA5pct": round(vsma5, 2),
                "vsMA20pct": round(vsma20, 2),
                "session": "收盘",
            },
            freshness="fresh",
            防未来=True,
            source=self.source,
        )


register(PriceVolumeTool())
