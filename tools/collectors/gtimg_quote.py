"""腾讯 gtimg 实时报价采集(单一事实源)。

背景:实时报价原先只在 `web/realtime.py` 里有一份"顺手写"的解析(只取 现价/涨跌幅/成交额),
盘中快照节点(`tools.pipeline.intraday_snapshot`)需要更全的字段(量比/换手/开高低/量额)。
为免两份解析各自漂移,把**抓取+字段解析**上提到采集层(collectors),web 与 pipeline 共用:
    · web/realtime.py 的 `_fetch_quotes` 已改为委托本模块(字段口径不变、行为不变);
    · tools/pipeline/intraday_snapshot.py 直接调 `fetch_quotes()`。

数据源选择:东财 spot 对本机有 TLS 指纹墙,腾讯 gtimg 是 collectors 一直用的备用源,
一次 URL 可批量拉多只(逗号分隔),沪 sh / 深 sz / 北 bj 前缀。返回 GBK 文本,`~` 分隔。

字段下标(2026-09-03 实测 88 段,个股与指数同构):
    1 名称  2 六位代码  3 现价  4 昨收  5 今开  6 成交量(手)
    30 行情时刻(YYYYMMDDHHMMSS)  31 涨跌额  32 涨跌幅%  33 最高  34 最低
    37 成交额(万元)  38 换手率%  43 振幅%  49 量比
指数行的 换手/量比 由源方给出(口径为交易所侧近似),照抄不加工。

诚实边界:这是**盘中快照**(准实时,取决于源方刷新),不是逐笔;停牌/异常票源方返回空行,
本模块跳过(由调用方按"要了但没回"记 errors),不猜、不补。
"""
from __future__ import annotations

import logging

import requests

from tools.collectors._retry import retry_call
from tools.config import exchange

logger = logging.getLogger("collectors.gtimg_quote")

GTIMG_URL = "http://qt.gtimg.cn/q="
_TIMEOUT_S = 8
_BATCH = 50                      # 单次 URL 拼的标的数上限(URL 长度与源方友好度)
_MIN_PARTS = 50                  # 少于此段数视为异常/空行(停牌返回 v_xxx="";)


def market_prefix(code: str) -> str:
    """6 位 A 股代码 → 腾讯 gtimg 市场前缀 `sh`/`sz`/`bj`。

    判据在 `tools.config.exchange`(**单一真源**),这里只定本源的兜底口径:判不出的
    代码回落 `sz`——与历史行为一致(gtimg 对不存在的 symbol 返空行,上层已按缺失处理)。
    920 段为什么必须先于 9→sh、900 沪B 为什么仍归 sh,见真源模块 docstring。
    """
    return exchange.exchange_of(code) or "sz"


def _num(x):
    """数值化;空串/非数 → None(不静默变 0,缺失就是缺失)。"""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _to_shares(code, vol, price, amt_wan):
    """gtimg 成交量归一到主档口径「股」,**采集层单一归一点**,下游不再 ×100。

    背景:gtimg(`qt.gtimg.cn` 批量快照,以及 `fqkline` K线端点)对**科创板 688/689
    段返回"股"**、其余板块返回"手"(2026-09-17 在线实证见 `exchange.is_star_market`)。
    历史上采集层对全部代码一律 ×100 → 618 只科创板 volume 被高估 100 倍。

    判据优先级:①**量额自证**——`amount(元) ≈ close(元) × volume(股)` ⇒
    `vol×price/(amount)` 比值≈1 说明本就是"股",≈0.01 说明是"手"需 ×100;
    ②自证不了(缺 amount/price 或比值落在灰区)→ 回落**代码段规则**
    (科创板"股"、其余"手→×100")。自证优先于代码段:即便代码段判错、或源方哪天
    改口径,只要 amount 在位就能纠回来;两者都用不上时保守按代码段。
    """
    if vol is None:
        return None
    if price and amt_wan and vol > 0:
        r = vol * price / (amt_wan * 1e4)
        if 0.5 <= r <= 2.0:                 # 已是「股」
            return vol
        if 0.005 <= r <= 0.02:              # 是「手」,×100 → 股
            return vol * 100.0
        logger.warning("gtimg %s 量额比 %.3g 落灰区,无法自证单位,回落代码段规则", code, r)
    return vol if exchange.is_star_market(code) else vol * 100.0


def parse_line(line: str) -> tuple[str, dict] | None:
    """单行 `v_sz002811="51~郑中设计~002811~..."` → (code, 字段 dict);异常/空行 → None。

    `volume` 已归一到主档口径「股」(见 `_to_shares`);`volume_raw` 保留源方原值供审计。
    """
    parts = line.split("~")
    if len(parts) < _MIN_PARTS:
        return None
    code = parts[2].strip()
    if not code:
        return None
    vol_raw = _num(parts[6])                 # 源方成交量:688/689 为股、其余为手
    price = _num(parts[3])
    amt_wan = _num(parts[37])
    return code, {
        "name": parts[1],
        "price": price,                     # 现价
        "prev_close": _num(parts[4]),       # 昨收
        "open": _num(parts[5]),
        "high": _num(parts[33]),
        "low": _num(parts[34]),
        "volume": _to_shares(code, vol_raw, price, amt_wan),   # 已归一「股」,下游不再 ×100
        "volume_raw": vol_raw,              # 源方原值(688/689=股、其余=手),审计用
        "amount_wan": amt_wan,              # 成交额(万元)
        "pct_chg": _num(parts[32]),         # 涨跌幅%
        "change": _num(parts[31]),          # 涨跌额
        "vol_ratio": _num(parts[49]),       # 量比
        "turnover": _num(parts[38]),        # 换手率%
        "amplitude": _num(parts[43]),       # 振幅%
        "quote_time": parts[30].strip() or None,   # 源方行情时刻 YYYYMMDDHHMMSS
    }


def _fetch_batch(symbols: list[str]) -> dict[str, dict]:
    """一次 HTTP 拉一批带前缀 symbol(如 sz002811/sh000001),返回 {6位代码: 字段}。"""
    r = requests.get(GTIMG_URL + ",".join(symbols), timeout=_TIMEOUT_S)
    r.encoding = "gbk"
    out: dict[str, dict] = {}
    for line in r.text.strip().split("\n"):
        got = parse_line(line)
        if got:
            out[got[0]] = got[1]
    return out


def fetch_symbols(symbols: list[str]) -> dict[str, dict]:
    """带前缀 symbol 列表 → {6位代码: 字段}。分批 + 瞬时网络错误重试;彻底失败抛给上层。"""
    if not symbols:
        return {}
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), _BATCH):
        chunk = symbols[i:i + _BATCH]
        out.update(retry_call(_fetch_batch, chunk, label=f"gtimg[{chunk[0]}..{len(chunk)}只]"))
    return out


def fetch_quotes(codes: list[str]) -> dict[str, dict]:
    """A 股 6 位代码列表 → {code: 字段}(市场前缀自动推断)。停牌/异常票不出现在返回里。"""
    codes = [c for c in dict.fromkeys(codes) if c]
    return fetch_symbols([f"{market_prefix(c)}{c}" for c in codes])
