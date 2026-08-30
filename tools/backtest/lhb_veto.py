"""龙虎榜「否决 / 反转」风控信号(WI-6 Phase 2 —— veto 专线)。

===== 为什么是否决信号,不是多头信号 =====
前一轮可回测性验证(lhb_block_lab)结论明确:**龙虎榜作多头 alpha 是负的**——
净买上榜票 T+1 开盘入、持有 5 日,按沪深300去市场后**显著跑输**(见光死/追高);
net_buy_ratio 的前向 rank-IC 显著为负。方向反过来用才有价值:

  · **入选否决(entry veto)**:一只票近日净买上榜(游资/散户追高)→ **不进多头候选**。
  · **持仓离场(de-risk / exit)**:持仓票净买上榜 → **减仓/离场**(见光死,H5 尤甚)。
  · **短反转(reversal,弱)**:净卖上榜票 T+1 有小幅反弹(H1),扣成本后边际,谨慎用。

本模块只产出**风控裁决**(veto verdict),不产出买入建议。与「红旗 veto」(基本面
排雷)正交——龙虎榜是**市场行为/微结构事件**,红旗是**财报质量事件**——两者可 OR
合成统一风控否决线(见 lhb_veto_lab 结论)。

===== 防未来函数(红线) =====
龙虎榜盘后披露:上榜日 T 的席位榜 T 收盘后才公开 → 最早可用 = **T+1 开盘**。
所有裁决只用 **list_date 严格小于 as_of** 的上榜记录(复用 lhb.lhb_asof 语义),
在 as_of 当天做决策时,只有 as_of 之前已公开的上榜才纳入。绝不用 as_of 当日盘中信息。

非投资建议;历史披露数据仅供研究,否决信号的价值由回测检验(见 lhb_veto_lab)。
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, timedelta

logger = logging.getLogger("backtest.lhb_veto")

# ── 默认门槛(env/调用方可覆盖)──
VETO_WINDOW_DAYS = 7          # "近日上榜"窗口:as_of 前多少**自然日**内的上榜才算活跃(≈5 交易日)
MIN_NET_BUY_RATIO = 0.0       # 入选否决/离场触发的最小净买占比(%);>0 可只否决"强追高"
MIN_NET_SELL_RATIO = 0.0      # 反转触发的最小净卖占比绝对值(%)

MODE_ENTRY_VETO = "entry_veto"   # 入选否决:近日净买上榜 → 不进多头候选
MODE_EXIT = "exit"               # 持仓离场:近日净买上榜 → 减仓/离场(语义同 entry_veto,施加对象不同)
MODE_REVERSAL = "reversal"       # 短反转:近日净卖上榜 → 弱反弹候选(H1)


@dataclass
class Verdict:
    """一次风控裁决(纯数据,便于序列化/落审计轨迹)。

    triggered   是否触发(entry_veto/exit=是否否决;reversal=是否为反转候选)。
    mode        裁决模式(见 MODE_*)。
    reason      人读原因(含触发上榜日、方向、净买占比)。
    list_date   触发裁决的那条上榜日(最近一条命中);未触发 → None。
    direction   命中上榜的方向(+1 净买 / −1 净卖);未触发 → 0。
    net_buy_ratio 命中上榜的净买额占总成交比(%);未触发 → None。
    days_since  as_of 距该上榜日的自然日数;未触发 → None。
    n_recent    窗口内命中方向的上榜条数(强度参考)。
    """
    triggered: bool
    mode: str
    reason: str
    list_date: str | None = None
    direction: int = 0
    net_buy_ratio: float | None = None
    days_since: int | None = None
    n_recent: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _ratio_of(ev: dict) -> float | None:
    """从事件 dict 取净买占比:优先 net_buy_ratio,回退 sig(lab 长表用 sig 名)。"""
    for k in ("net_buy_ratio", "sig"):
        v = ev.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def _days_between(list_date: str, as_of: str) -> int | None:
    """as_of − list_date 的自然日数;解析失败 → None。"""
    try:
        d0 = date.fromisoformat(str(list_date)[:10])
        d1 = date.fromisoformat(str(as_of)[:10])
        return (d1 - d0).days
    except ValueError:
        return None


def verdict_from_events(events: list[dict], as_of: str, mode: str = MODE_ENTRY_VETO,
                        window_days: int = VETO_WINDOW_DAYS,
                        min_net_buy_ratio: float = MIN_NET_BUY_RATIO,
                        min_net_sell_ratio: float = MIN_NET_SELL_RATIO) -> Verdict:
    """纯函数裁决核心:给一票的上榜事件列表 + as_of,产出风控裁决(可离线单测)。

    events:每元素含 list_date、direction、net_buy_ratio(或 sig)。**不要求已按 as_of 过滤**,
            本函数内部再做**严格小于 as_of**(防未来函数)+ 窗口(近 window_days 自然日)双重过滤。
    mode:  entry_veto / exit → 触发条件 = 净买上榜(direction=+1 且 ratio≥min_net_buy_ratio);
           reversal        → 触发条件 = 净卖上榜(direction=−1 且 |ratio|≥min_net_sell_ratio)。
    命中多条时取**最近**一条锚定裁决;n_recent 计窗口内同向命中条数。
    """
    cutoff = str(as_of)[:10]
    want_dir = -1 if mode == MODE_REVERSAL else 1
    min_ratio = min_net_sell_ratio if mode == MODE_REVERSAL else min_net_buy_ratio
    hits = []
    for ev in events or []:
        ld = str(ev.get("list_date", ""))[:10]
        if len(ld) != 10 or ld >= cutoff:            # 严格小于 as_of(盘后披露,防未来函数)
            continue
        ds = _days_between(ld, cutoff)
        if ds is None or ds > window_days or ds < 0:  # 只看近 window_days 自然日内的活跃上榜
            continue
        if int(ev.get("direction", 0)) != want_dir:
            continue
        r = _ratio_of(ev)
        if r is None or abs(r) < min_ratio:
            continue
        hits.append((ld, ds, int(ev.get("direction", 0)), r))
    if not hits:
        return Verdict(triggered=False, mode=mode,
                       reason=f"近{window_days}日无{'净卖' if mode == MODE_REVERSAL else '净买'}上榜")
    hits.sort(key=lambda x: x[0], reverse=True)       # 最近一条锚定
    ld, ds, d, r = hits[0]
    kind = "净卖反转候选" if mode == MODE_REVERSAL else ("否决入选" if mode == MODE_ENTRY_VETO else "触发离场")
    reason = f"{ld} {'净买' if d > 0 else '净卖'}上榜(占比{r:.2f}%,距今{ds}日)→ {kind}"
    return Verdict(triggered=True, mode=mode, reason=reason, list_date=ld,
                   direction=d, net_buy_ratio=round(r, 4), days_since=ds, n_recent=len(hits))


def veto_asof(code: str, as_of: str, mode: str = MODE_ENTRY_VETO,
              window_days: int = VETO_WINDOW_DAYS,
              min_net_buy_ratio: float = MIN_NET_BUY_RATIO,
              min_net_sell_ratio: float = MIN_NET_SELL_RATIO,
              part_date: str | None = None) -> Verdict:
    """生产入口:从落盘龙虎榜快照读该票 as-of 切片 → 裁决。

    走 lhb.lhb_asof(严格 list_date < as_of)取无未来函数事件,再交 verdict_from_events。
    缺快照(该票从未上榜/未采集)→ 不触发(保守放行,由上层其它风控兜底)。
    part_date:锁读哪个采集分区(默认最新)。
    """
    from tools.collectors import lhb
    try:
        events = lhb.lhb_asof(code, as_of, date=part_date)
    except FileNotFoundError:
        return Verdict(triggered=False, mode=mode, reason="无龙虎榜快照(未上榜/未采集)")
    return verdict_from_events(events, as_of, mode=mode, window_days=window_days,
                               min_net_buy_ratio=min_net_buy_ratio,
                               min_net_sell_ratio=min_net_sell_ratio)


def entry_veto_asof(code: str, as_of: str, **kw) -> Verdict:
    """入选否决:as_of 做多头选股时,该票近日净买上榜 → 否决(triggered=True 即不选)。"""
    return veto_asof(code, as_of, mode=MODE_ENTRY_VETO, **kw)


def exit_signal_asof(code: str, as_of: str, **kw) -> Verdict:
    """持仓离场:as_of 该持仓票近日净买上榜 → 减仓/离场(triggered=True 即触发离场)。"""
    return veto_asof(code, as_of, mode=MODE_EXIT, **kw)


def reversal_signal_asof(code: str, as_of: str, **kw) -> Verdict:
    """短反转(弱):as_of 该票近日净卖上榜 → 短期弱反弹候选(triggered=True)。谨慎,扣成本后边际。"""
    return veto_asof(code, as_of, mode=MODE_REVERSAL, **kw)


def _default_window() -> tuple[str, str]:
    """便捷:返回 (as_of 前 VETO_WINDOW_DAYS 日, 今天) 供批量扫描默认区间。"""
    end = date.today()
    return (end - timedelta(days=VETO_WINDOW_DAYS)).isoformat(), end.isoformat()
