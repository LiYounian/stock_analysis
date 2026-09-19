"""fake_good_news_tool（②消息塔层）· 判个股"假利好"嫌疑。

背景：程序每日独立跑、无跨日记忆，容易把"利好兑现日高开走弱→随后回吐"的假利好
当成真利好加分。本工具用写死判据给出嫌疑档，并回看利好落地后的价格表现打补丁。

三条判据（阈值全部写死·档位语义锁）：
①【近期利好】读 per-stock events（`data/analysis/<as_of>/<code>.json` 的 events），
   取 impact=利好 且日期在 as_of 前 RECENT_CAL_DAYS 日内的最近一条为"兑现日"锚点。
②【兑现日高开走弱】主档 K 线看兑现日：高开（开盘涨幅≥GAP_UP）但收盘明显走弱
   （收盘涨幅 < 开盘涨幅 − FADE），或放量长上影（上影占比≥UPPER_SHADOW 且量比≥VOL_SURGE）。
③【利好后回看】兑现日之后一日（若 ≤ as_of）收盘回吐（涨幅 ≤ DROP_NEXT）——
   给"程序无记忆"打的补丁：利好落地后价格是否真的没兑现成上涨。

嫌疑档：无嫌疑（无近期利好）/ 低（有利好但价格行为健康）/ 中（命中 1 条）/ 高（命中≥2 条）。
无 events 数据 → freshness=missing，仅凭 as_of 当日 K 线给保守判断，绝不编造利好。
数据铁律：只信主档 K 线（688/689 量能自校由 load_kline 处理）；as_of 防未来。
"""
from __future__ import annotations

from typing import Optional
import json
import os

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import load_kline, data_root, volume_ratio, 浓缩块

# ── 写死阈值（语义锁）──
RECENT_CAL_DAYS = 7      # "近期"利好窗口：as_of 前 7 个自然日内
GAP_UP = 3.0            # 高开：开盘涨幅(%) ≥
FADE = 2.0             # 走弱：收盘涨幅 比 开盘涨幅 低 ≥（pp）
UPPER_SHADOW = 0.5      # 长上影：上影 / 当日振幅 ≥
VOL_SURGE = 1.5        # 放量：量比（对前 5 日均量）≥
DROP_NEXT = -2.0        # 利好后回吐：次日收盘涨幅(%) ≤

# 嫌疑档位（命中判据数 → 档）——语义锁死这张表
嫌疑档枚举 = ("无嫌疑", "低", "中", "高")

# ── v2 影响模板（假利好嫌疑档→对选股影响；共性"假利好是什么"已进统一词表）──
_假利好影响 = {
    "高": "假利好嫌疑高、利好或不真、减分",
    "中": "假利好嫌疑、需警惕",
    "低": "利好价格行为尚健康",
    "无嫌疑": "无假利好信号",
}


def _load_events(code: str, as_of: str, root: Optional[str]) -> Optional[list]:
    """读 per-stock events；文件缺或 events 非列表→None（missing，不编）。"""
    p = os.path.join(data_root(root), "data", "analysis", as_of, f"{code}.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    ev = d.get("events")
    return ev if isinstance(ev, list) else None


def _recent_good(events: list, as_of: str) -> Optional[dict]:
    """取 as_of 前 RECENT_CAL_DAYS 内、impact=利好 的最近一条（防未来：日期 ≤ as_of）。"""
    import datetime as _dt

    try:
        cut = _dt.date.fromisoformat(as_of)
    except ValueError:
        return None
    best = None
    for e in events:
        if not isinstance(e, dict) or e.get("impact") != "利好":
            continue
        ds = e.get("date")
        try:
            d = _dt.date.fromisoformat(str(ds))
        except (ValueError, TypeError):
            continue
        if d > cut or (cut - d).days > RECENT_CAL_DAYS:
            continue
        if best is None or d > best[0]:
            best = (d, e)
    return best[1] if best else None


def _bar_on_or_after(df, date_str: str):
    """兑现日 = event 日或其后首个交易日（≤ as_of，df 已防未来截断）。返回 (idx, row) 或 None。"""
    import pandas as pd

    dates = pd.to_datetime(df["date"])
    target = pd.to_datetime(date_str)
    mask = dates >= target
    if not mask.any():
        return None
    idx = mask.idxmax()
    pos = df.index.get_loc(idx)
    return pos, df.iloc[pos]


def _pct(cur: float, prev: float) -> Optional[float]:
    if prev in (0, None) or cur is None:
        return None
    return (cur / prev - 1.0) * 100.0


class FakeGoodNewsTool:
    name = "fake_good_news"
    塔层 = "②消息"
    面 = "消息情绪面"  # 消息真实性·负向排雷
    source = "per-stock events + 主档 K 线 D0/D0+1 价格行为"

    def run(self, as_of: str, code: Optional[str] = None, root: Optional[str] = None, **kw) -> ToolResult:
        if not code:
            raise ValueError("fake_good_news 需 --code")
        df = load_kline(code, as_of, root=root, min_bars=6)
        events = _load_events(code, as_of, root)

        if df is None or len(df) < 2:
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
                浓缩块="假利好: K线不足·无法判价格行为·人工确认",
                fields={"数据不足": True, "嫌疑档": "无嫌疑"},
                freshness="missing", 防未来=True, source=self.source,
            )

        # events 缺失：仅凭 as_of 当日 K 线做保守判断（绝不编造利好存在）
        if events is None:
            hit_shadow, shadow_desc = self._long_upper_shadow(df, len(df) - 1)
            gap_fade, gf_desc = self._gap_fade(df, len(df) - 1)
            命中 = int(gap_fade) + int(hit_shadow)
            # 无 events 锚点时仅凭当日 K 线：0 信号=无嫌疑（不扣分），1=低，≥2=中。
            # 绝不因"数据缺失"默认判低——池内多数票无 per-stock json，命中0误判低会系统性扭曲排雷。
            档 = "中" if 命中 >= 2 else ("低" if 命中 == 1 else "无嫌疑")
            lines = [
                f"假利好嫌疑: {档}（events 缺失·仅凭 as_of 当日 K 线·不编造利好）影响：{_假利好影响.get(档, '')}",
                f"当日高开走弱: {'命中' if gap_fade else '未命中'}（{gf_desc}）",
                f"当日放量长上影: {'命中' if hit_shadow else '未命中'}（{shadow_desc}）",
                "利好后回看: 不可判（无 events 锚点）",
            ]
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code, 浓缩块=浓缩块(lines),
                fields={"嫌疑档": 档, "命中数": 命中, "events": "missing",
                        "高开走弱": bool(gap_fade), "放量长上影": bool(hit_shadow),
                        "利好后回吐": None},
                freshness="missing", 防未来=True, source=self.source,
            )

        good = _recent_good(events, as_of)
        if good is None:
            n利好 = sum(1 for e in events if isinstance(e, dict) and e.get("impact") == "利好")
            lines = [
                f"假利好嫌疑: 无嫌疑（近 {RECENT_CAL_DAYS} 日无利好事件锚点）影响：{_假利好影响['无嫌疑']}",
                f"events 总 {len(events)} 条·其中利好 {n利好} 条（均早于窗口或无）",
            ]
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code, 浓缩块=浓缩块(lines),
                fields={"嫌疑档": "无嫌疑", "命中数": 0, "events": "present",
                        "利好事件数": n利好, "近期利好": None},
                freshness="fresh", 防未来=True, source=self.source,
            )

        # 有近期利好 → 定位兑现日 D0
        loc = _bar_on_or_after(df, good.get("date"))
        if loc is None:
            lines = [
                f"假利好嫌疑: 低（利好 {good.get('date')} 后无 ≤as_of 交易日可核价格行为）影响：{_假利好影响['低']}",
                f"利好: {str(good.get('title'))[:36]}",
            ]
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code, 浓缩块=浓缩块(lines),
                fields={"嫌疑档": "低", "命中数": 0, "events": "present",
                        "近期利好日": good.get("date"), "兑现日": None},
                freshness="fresh", 防未来=True, source=self.source,
            )

        pos, _ = loc
        gap_fade, gf_desc = self._gap_fade(df, pos)
        hit_shadow, shadow_desc = self._long_upper_shadow(df, pos)
        判据2 = bool(gap_fade or hit_shadow)
        # 判据③ 利好后回看：D0 次日（若 ≤ as_of）
        回吐 = None
        回吐_desc = "次日超出 as_of·待观察"
        if pos + 1 <= len(df) - 1:
            nxt = df.iloc[pos + 1]
            nxt_pct = float(nxt.get("pct_chg")) if nxt.get("pct_chg") is not None else _pct(
                float(nxt["close"]), float(df.iloc[pos]["close"]))
            回吐 = bool(nxt_pct is not None and nxt_pct <= DROP_NEXT)
            回吐_desc = f"兑现次日收盘涨幅 {nxt_pct:+.2f}%（阈值 ≤{DROP_NEXT}）" if nxt_pct is not None else "次日涨幅缺失"
        判据3 = bool(回吐)

        命中 = int(判据2) + int(判据3)
        档 = "高" if 命中 >= 2 else ("中" if 命中 == 1 else "低")
        d0 = str(df.iloc[pos]["date"])[:10]
        lines = [
            f"假利好嫌疑: {档}（近期利好 {good.get('date')} · 兑现日 {d0} · 命中 {命中}/2）影响：{_假利好影响.get(档, '')}",
            f"利好: {str(good.get('title'))[:34]}（impact=利好）",
            f"①高开走弱: {'命中' if gap_fade else '未命中'}（{gf_desc}）",
            f"②放量长上影: {'命中' if hit_shadow else '未命中'}（{shadow_desc}）",
            f"③利好后回吐: {'命中' if 判据3 else '未命中'}（{回吐_desc}）",
        ]
        return ToolResult(
            name=self.name, 塔层=self.塔层, as_of=as_of, code=code, 浓缩块=浓缩块(lines),
            fields={
                "嫌疑档": 档, "命中数": 命中, "events": "present",
                "近期利好日": good.get("date"), "兑现日": d0,
                "利好标题": str(good.get("title")),
                "高开走弱": bool(gap_fade), "放量长上影": bool(hit_shadow),
                "利好后回吐": 回吐,
            },
            freshness="fresh", 防未来=True, source=self.source,
        )

    # ── 判据实现 ──
    @staticmethod
    def _gap_fade(df, pos: int) -> tuple[bool, str]:
        """兑现日高开走弱：开盘涨幅≥GAP_UP 且 收盘涨幅 比 开盘涨幅 低 ≥FADE。"""
        if pos < 1:
            return False, "无前收盘"
        row = df.iloc[pos]
        prev_close = float(df.iloc[pos - 1]["close"])
        open_pct = _pct(float(row["open"]), prev_close)
        close_pct = float(row.get("pct_chg")) if row.get("pct_chg") is not None else _pct(
            float(row["close"]), prev_close)
        if open_pct is None or close_pct is None:
            return False, "涨幅缺失"
        hit = open_pct >= GAP_UP and (open_pct - close_pct) >= FADE
        return hit, f"开盘{open_pct:+.1f}%→收盘{close_pct:+.1f}%"

    @staticmethod
    def _long_upper_shadow(df, pos: int) -> tuple[bool, str]:
        """放量长上影：上影/振幅≥UPPER_SHADOW 且 量比≥VOL_SURGE。"""
        row = df.iloc[pos]
        hi, lo = float(row["high"]), float(row["low"])
        o, c = float(row["open"]), float(row["close"])
        rng = hi - lo
        if rng <= 0:
            return False, "振幅为0(一字)"
        shadow = (hi - max(o, c)) / rng
        vr = volume_ratio(df.iloc[: pos + 1]) if pos >= 5 else None
        hit = shadow >= UPPER_SHADOW and vr is not None and vr >= VOL_SURGE
        vr_s = f"量比{vr:.2f}" if vr is not None else "量比NA"
        return hit, f"上影占比{shadow:.2f}·{vr_s}"


register(FakeGoodNewsTool())
