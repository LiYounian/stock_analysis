"""① 选股产物 / 自选池 / 手动 → Watchlist(机读监控格式)。

设计见方案 §1:确定性 md 表格解析打底,llm_augment 兜底(默认 no-op)。
**只读 md,不改选股代码**(不动生产主链路)。

支持两类操作卡表:
  A. 日内 D-0 交易计划: 列含「进场触发/止损位/止盈位」,价位在单元格文本里(如「10.67(−4.26%)」)。
  B. 隔夜/波段操作卡  : 列含「挂单价/止损价/不追高上限」,单元格是纯数字(selection_synth 回填)。
两表都没有 → 回退 PICKS 锚点/买入表代码,生成「仅监测无价位」的条目。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from tools.config import settings, stock_pool
from tools.monitor.schema import Trigger, WatchItem, Watchlist

logger = logging.getLogger("monitor.converter")

PICK_DIR = settings.PROJECT_ROOT / "docs" / "每日分析" / "选股"
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_ANCHOR_RE = re.compile(r"<!--\s*PICKS?\s*:\s*(.*?)\s*-->", re.IGNORECASE | re.DOTALL)
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# 抓单元格里第一个数(允许负号/千分位无关);「≤11.2」「10.67(−4.26%)」都能取到目标价。
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_LE_RE = re.compile(r"[≤<]\s*(-?\d+(?:\.\d+)?)")     # 进场触发里「现价≤11.2」


def _first_num(s: str) -> Optional[float]:
    m = _NUM_RE.search(s or "")
    return float(m.group()) if m else None


def _parse_md_tables(md: str) -> list[list[dict[str, str]]]:
    """把 md 里每张管道表解析成 [ {表头列: 单元格}, ... ]。表间以非表格行分隔。"""
    tables: list[list[dict[str, str]]] = []
    header: Optional[list[str]] = None
    rows: list[dict[str, str]] = []

    def _cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    def _flush():
        nonlocal header, rows
        if header and rows:
            tables.append(rows)
        header, rows = None, []

    for line in md.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = _cells(s)
            if set(cells) <= {"", *[c for c in cells if set(c) <= set("-: ")]} and header:
                continue                            # 分隔行 ---|--- 跳过
            if header is None:
                header = cells
            else:
                if len(cells) == len(header):
                    rows.append(dict(zip(header, cells)))
        else:
            _flush()
    _flush()
    return tables


def _find_table(tables: list[list[dict[str, str]]], must_have: list[str]) -> Optional[list[dict]]:
    for t in tables:
        if not t:
            continue
        cols = set(t[0].keys())
        if all(any(k in c for c in cols) for k in must_have):
            return t
    return None


def _col(row: dict, key: str) -> str:
    """按列名子串取单元格(表头可能带口径后缀,如「现价(11:30)」)。"""
    for c, v in row.items():
        if key in c:
            return v
    return ""


def _triggers_from_prices(entry, stop, take) -> list[Trigger]:
    ts: list[Trigger] = []
    if entry is not None:
        ts.append(Trigger(id="entry", kind="entry_limit", field="price", op="<=",
                          value=entry, action="可挂单进场(不追高)"))
    if stop is not None:
        ts.append(Trigger(id="stop", kind="stop_loss", field="price", op="<=",
                          value=stop, action="止损/放弃进场"))
    if take is not None:
        ts.append(Trigger(id="take", kind="take_profit", field="price", op=">=",
                          value=take, action="止盈了结"))
    return ts


def _eod_gate(kind: str) -> list[Trigger]:
    """日内体裁给一条收盘了结提示闸门。"""
    if "日内" in kind or "午盘" in kind or "尾盘" in kind:
        return [Trigger(id="eod_flat", kind="time", at="14:57",
                        action="当日收盘无条件了结提示", once=True)]
    return []


def _latest_pick_md() -> Optional[Path]:
    files = sorted(PICK_DIR.glob("*.md"), reverse=True)
    return files[0] if files else None


def from_selection_md(path: str | Path | None = None, *, date: str | None = None) -> Watchlist:
    """选股 md → Watchlist。path 缺省取最新一份;date 缺省从文件名/正文推断。"""
    p = Path(path) if path else _latest_pick_md()
    if p is None or not p.exists():
        raise FileNotFoundError(f"选股 md 不存在:{path or PICK_DIR}")
    md = p.read_text(encoding="utf-8")
    if date is None:
        m = _DATE_RE.search(p.stem) or _DATE_RE.search(md[:200])
        date = m.group(1) if m else p.stem
    kind = "日内午盘" if ("午盘" in md[:300] or "日内" in md[:300]) else "隔夜波段"

    tables = _parse_md_tables(md)
    items: list[WatchItem] = []
    seen: set[str] = set()

    # A. 日内 D-0 交易计划表
    t = _find_table(tables, ["代码", "进场触发", "止损", "止盈"])
    if t:
        for row in t:
            code_cell = _col(row, "代码")
            mcode = _CODE_RE.search(code_cell)
            if not mcode:
                continue
            code = mcode.group(1)
            entry_cell = _col(row, "进场触发")
            le = _LE_RE.search(entry_cell)
            entry = float(le.group(1)) if le else None
            stop = _first_num(_col(row, "止损"))
            take = _first_num(_col(row, "止盈"))
            ref = _first_num(_col(row, "现价"))
            items.append(WatchItem(
                code=code, name=_col(row, "名称") or code, role="买入", ref_price=ref,
                triggers=_triggers_from_prices(entry, stop, take), gates=_eod_gate(kind),
                meta={"波动锚": _col(row, "波动锚"), "了结纪律": _col(row, "了结纪律")},
            ))
            seen.add(code)

    # B. 隔夜/波段操作卡表
    if not items:
        t = _find_table(tables, ["代码", "挂单价", "止损价"])
        if t:
            for row in t:
                mcode = _CODE_RE.search(_col(row, "代码"))
                if not mcode:
                    continue
                code = mcode.group(1)
                entry = _first_num(_col(row, "挂单价"))
                stop = _first_num(_col(row, "止损价"))
                cap = _first_num(_col(row, "不追高"))
                ts = _triggers_from_prices(entry, stop, None)
                if cap is not None:
                    ts.append(Trigger(id="cap", kind="no_chase", field="price", op=">=",
                                      value=cap, action="已达不追高上限,勿追"))
                items.append(WatchItem(code=code, name=_col(row, "名称") or code,
                                       role="买入", triggers=ts))
                seen.add(code)

    # C. 兜底:PICKS 锚点 / 全文买入代码 → 仅监测无价位
    if not items:
        anchor = _ANCHOR_RE.search(md)
        codes = _CODE_RE.findall(anchor.group(1)) if anchor else _CODE_RE.findall(md)
        for code in dict.fromkeys(codes):
            if code in seen:
                continue
            items.append(WatchItem(code=code, name=code, role="观察"))

    return Watchlist(date=date, items=items,
                     source={"type": "selection_md", "path": str(p), "as_of": date, "kind": kind})


def from_watchpool(*, date: str) -> Watchlist:
    """自选池 A 股 → Watchlist(仅监测无价位;价位可后续手动/agent 补)。"""
    items = [WatchItem(code=s.code, name=s.name, role="自选")
             for s in stock_pool.get_pool() if s.market == "A"]
    return Watchlist(date=date, items=items, source={"type": "watchpool", "as_of": date})


def merge(*wls: Watchlist) -> Watchlist:
    """去重合并多个 watchlist;同 code 合并触发(按 id 去重),role 取先出现的非「自选」。"""
    if not wls:
        raise ValueError("merge 至少需要一个 watchlist")
    date = wls[0].date
    by_code: dict[str, WatchItem] = {}
    for wl in wls:
        for it in wl.items:
            cur = by_code.get(it.code)
            if cur is None:
                by_code[it.code] = WatchItem(**{**it.__dict__})
                continue
            have = {t.id for t in cur.triggers}
            cur.triggers.extend(t for t in it.triggers if t.id not in have)
            gate_ids = {t.id for t in cur.gates}
            cur.gates.extend(t for t in it.gates if t.id not in gate_ids)
            if cur.role == "自选" and it.role != "自选":
                cur.role = it.role
            if cur.ref_price is None:
                cur.ref_price = it.ref_price
    return Watchlist(date=date, items=list(by_code.values()),
                     source={"type": "merged", "as_of": date,
                             "parts": [w.source.get("type") for w in wls]})


def llm_augment(item: WatchItem, md_context: str) -> list[Trigger]:
    """兜底 hook:把 md 里的文字闸门(量能转弱/板块降级…)交 Agent 转成固定指标 trigger。

    默认 no-op(返回空)。接 LLM 后覆写本函数,不影响确定性价位链路(defense-in-depth)。
    """
    return []
