"""entry_price_tool（①塔基·价位回填）· P1a 样板工具。

公理 G5：价位一律程序回填、单调夹逼，LLM 不产数字。复用
`tools.analysis.selection_synth.fill_entry_exit`（回踩三式 + 突破旁路 + 单调夹逼）。

输出四价位：首入场限价(挂单) / 加仓位(回踩) / 红线(不追高上限) / 止损。
- 首入场 = D+1 开盘附近限价（默认回踩 MA5，最保守；不打高开）。
- 回踩 = 加仓口径（拍板③：回踩限价从"入场闸门"降为"加仓"，见规划 §10）。
"""
from __future__ import annotations

from typing import Optional
import math

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import load_kline, 浓缩块

_ENTRY_METHODS = ("回踩MA5", "回踩MA20", "回踩前低", "突破确认", "缩量企稳", "突破新高确认")


def _form_from_kline(df) -> Optional[dict]:
    if df is None or len(df) < 20:
        return None
    close = df["close"]
    ma5 = float(close.tail(5).mean())
    ma20 = float(close.tail(20).mean())
    现价 = float(close.iloc[-1])
    前低 = float(df["low"].tail(20).min())
    当日high = float(df["high"].iloc[-1])
    当日low = float(df["low"].iloc[-1])
    return {
        "ma5": ma5,
        "ma20": ma20,
        "前低": 前低,
        "现价": 现价,
        "当日high": 当日high,
        "当日low": 当日low,
    }


class EntryPriceTool:
    name = "entry_price"
    塔层 = "①塔基"
    source = "主档 K 线 + selection_synth.fill_entry_exit"

    def run(
        self,
        as_of: str,
        code: Optional[str] = None,
        method: str = "回踩MA5",
        root: Optional[str] = None,
        **kw,
    ) -> ToolResult:
        if not code:
            raise ValueError("entry_price 需 --code")
        if method not in _ENTRY_METHODS:
            method = "回踩MA5"
        df = load_kline(code, as_of, root=root, min_bars=20)
        form = _form_from_kline(df)
        if form is None:
            return ToolResult(
                name=self.name,
                塔层=self.塔层,
                as_of=as_of,
                code=code,
                浓缩块="价位: 数据不足(K线<20根或缺失)·人工确认",
                fields={"数据不足": True},
                freshness="missing",
                防未来=True,
                source=self.source,
            )
        # 延迟 import，避免 registry 在无 pandas/网关环境被 import 时炸
        from tools.analysis.selection_synth import fill_entry_exit

        stock = {"code": code, "入场方式": method}
        stock = fill_entry_exit(stock, form)
        entry, stop, cap = stock.get("挂单价"), stock.get("止损价"), stock.get("不追高上限")
        现价 = form["现价"]
        # 单调性自检（防未来重写破坏 G5 铁律）
        单调ok = all(
            isinstance(v, (int, float)) for v in (entry, stop, cap)
        ) and stop < entry <= cap
        止损幅 = (stop / entry - 1) * 100 if isinstance(entry, (int, float)) and entry else None
        lines = [
            f"入场方式: {method}（首入场=开盘附近限价·回踩=加仓）",
            f"首入场限价: {entry}　现价: {round(现价,3)}（限价不追高开）",
            f"加仓位(回踩): {form['ma5']:.3f}=MA5　止损: {stop}"
            + (f"（{止损幅:+.1f}%）" if 止损幅 is not None else ""),
            f"红线(不追高上限): {cap}　单调性: {'OK' if 单调ok else '⚠违规'}",
        ]
        return ToolResult(
            name=self.name,
            塔层=self.塔层,
            as_of=as_of,
            code=code,
            浓缩块=浓缩块(lines),
            fields={
                "入场方式": method,
                "挂单价": entry,
                "止损价": stop,
                "不追高上限": cap,
                "现价": round(现价, 3),
                "止损幅pct": round(止损幅, 2) if 止损幅 is not None else None,
                "单调性ok": bool(单调ok),
            },
            freshness="fresh",
            防未来=True,
            source=self.source,
        )


register(EntryPriceTool())
