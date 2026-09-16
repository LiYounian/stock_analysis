"""消息驱动板块选股 · M3 forward 记分器(non-gating advisory·纯记录·**绝不接 live 选股**)。

契约:docs/计划/2026-09-16_选股侧消费板块利好标签_接口设计.md(§1 v2 schema / §3 进池口径 /
§4 防未来·防偷看·non-gating)。上游:板块线把「消息驱动」块扩进
`data/analysis/<date>/sector_focus.json`(as_of + 利好板块[{board,强弱,关键事件,
龙头候选[{code,name,已动}],跟涨候选[{code,name,联动依据}]}])。

本模块做什么(与 veto_track / sentiment_shadow 同型的 forward-shadow):
  对信号日 D 读「消息驱动」候选 → 预注册"回踩限价"入场(限价=D 收盘价,次日 D+1 回踩才成交,
  不追高开)→ 记 D+1 / D+2 **绝对收益**(相对成交价)+ 全A等权基准同期收益(超额)→ append-only
  落 `data/shadow_forward/sector_news/<D>.json`,**分层**:
    ① 龙头 vs 跟涨(严格分开·两条独立 forward,不混档)
    ② 强弱(强/中/弱)
    ③ 已动 true/false(龙头);跟涨挂 board_已动(该板块龙头是否已动)以验联动假设。

龙头 vs 跟涨为何分开(§4 联动单独验):
  · 龙头候选 = 有个股级消息催化 → 主 forward。
  · 跟涨候选 = 同板块补涨先锋(龙头已动→后排接力)→ **独立「联动观察」forward 单列**,
    验的假设 = 「消息利好板块内、龙头已动 → 后排跟涨」;条件在**消息催化**、不在低位
    (补涨先锋在纯量价语境被证伪过是防守低β,只有这条消息联动 forward 能验它)。别混进主档。

铁律(预注册·落盘后只读不改·append-only):
  · **non-gating · forward-only · 纯记录**——绝不动 live 选股任何产物、绝不写 sector_focus.json。
  · 防未来/防偷看:as-of 守卫(只消费 as_of ≤ 信号日 D 的块);入场/收益只用 ≤ 到期日 K 线
    (限价成交用 D+1 low/open、基准=全A等权 market_ew);判据(限价口径/已动阈)预注册写死、
    绝不对既往赢家(通鼎002491/双星002585/奥士康002913)调参;未到期 label 留 null 待 --backfill 回填。
  · 样本 <120 只报 N、不下结论。
⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date as _date
from datetime import datetime

logger = logging.getLogger("research.sector_news_forward")

# ── 预注册常量(落盘后只读不改;绝不对个例调参)─────────────────────────────
MAIN_STREAM = "龙头"          # 主 forward(有个股催化)
FOLLOW_STREAM = "跟涨"        # 联动观察 forward(补涨先锋·单列验)
STRENGTH_LEVELS = ("强", "中", "弱")
LIMIT_RULE = "prev_close"     # 回踩限价 = 信号日 D 收盘价(次日回踩到此才成交,不追高开)
MOVED_PCT = 3.0               # 与板块线 news_focus_block.MOVED_PCT 一致(仅文档用,已动值由上游块给)
MIN_SAMPLE = 120             # 样本 <此只报 N、不下结论

# 收益横档:成交价(D+1)→ D+1 收盘 / D+2 收盘 的绝对收益%(对齐契约"记 D+1 与 D+2 绝对收益")
HORIZON_KEYS = ("r_d1", "r_d2")

DEFAULT_OUT_DIR = "data/shadow_forward/sector_news"
_EW_REL = "analysis/backtest/finval/market_ew.parquet"   # 复用 build_market_index 产的全A等权基准


# ── 数据根 / I/O ──────────────────────────────────────────────────────────
def _resolve_root(data_root: str | None) -> str:
    if data_root:
        return data_root
    try:
        from tools.analysis.market_forecast.dataroot import resolve_data_root
        return str(resolve_data_root(None))
    except Exception:  # noqa: BLE001
        return "data"


def _load_json(p: str):
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _block_from_focus(date: str, data_root: str | None) -> dict | None:
    """契约主源:sector_focus.json 的「消息驱动」块(本仓 → 主仓回退,只读)。缺/无块 → None。"""
    root = _resolve_root(data_root)
    obj = _load_json(os.path.join(root, "analysis", date, "sector_focus.json"))
    if isinstance(obj, dict) and isinstance(obj.get("消息驱动"), dict):
        return obj["消息驱动"]
    try:
        from tools.analysis.sector_forecast.market_step import resolve_analysis_file
        p = resolve_analysis_file(date, "sector_focus.json")
        if p:
            obj = _load_json(str(p))
            if isinstance(obj, dict) and isinstance(obj.get("消息驱动"), dict):
                return obj["消息驱动"]
    except Exception:  # noqa: BLE001
        pass
    return None


def _block_from_catalyst(date: str, data_root: str | None) -> dict | None:
    """回退源:读真 catalyst_<date>.json 的「板块研判」块,复用板块线**官方转换器**
    news_focus_block.build_news_driven_block(cat=…) 现算出等价「消息驱动」块(补 已动 + 跟涨候选)。

    不重造 schema(§5 复用不重造)、不烧 LLM(cat 直接喂已落盘研判)。sector_focus 未落盘/未装
    「消息驱动」块时用此拿真候选。缺 catalyst / 转换失败 → None。
    """
    root = _resolve_root(data_root)
    cat = None
    for base in (root, "data"):
        p = os.path.join(base, "sector_news", f"catalyst_{date}.json")
        obj = _load_json(p)
        if isinstance(obj, dict) and isinstance(obj.get("板块研判"), dict):
            cat = obj["板块研判"]
            break
    if cat is None:
        return None
    try:
        from tools.analysis.market_forecast.dataroot import ensure_data_root
        ensure_data_root(data_root)
        from tools.analysis.sector_forecast import news_focus_block as NF
        return NF.build_news_driven_block(date, cat=cat)
    except Exception as e:  # noqa: BLE001
        logger.warning("从 catalyst 构「消息驱动」块失败(%r)", e)
        return None


def _news_block(date: str, data_root: str | None) -> dict | None:
    """取信号日「消息驱动」块:先契约主源 sector_focus,缺则回退真 catalyst 官方转换。"""
    return _block_from_focus(date, data_root) or _block_from_catalyst(date, data_root)


# ── as-of 守卫(§4 防未来)─────────────────────────────────────────────────
def _asof_date(block: dict) -> str | None:
    """从块里取 as_of 的日期部分(ISO8601 或 YYYY-MM-DD 皆可)。缺 → None。"""
    raw = block.get("as_of")
    if not raw:
        return None
    s = str(raw)
    return s[:10] if len(s) >= 10 else s


def asof_ok(block: dict, signal_date: str) -> bool:
    """只消费 as_of ≤ 信号日 D 的块;as_of 晚于 D(未来信号)→ 拒。缺 as_of → 保守拒。"""
    ad = _asof_date(block)
    if not ad:
        return False
    try:
        return _date.fromisoformat(ad) <= _date.fromisoformat(signal_date)
    except ValueError:
        return False


# ── 候选抽取:龙头 / 跟涨 严格分开 ────────────────────────────────────────
def extract_candidates(block: dict) -> tuple[list[dict], list[dict]]:
    """把「消息驱动」块拆成 (龙头候选[], 跟涨候选[]) 两条独立流。

    每条记录带板块级 强弱;龙头带 已动;跟涨带 联动依据 + board_已动(该板块龙头是否已动,
    验联动假设「龙头已动→跟涨接力」的触发条件)。
    """
    leaders: list[dict] = []
    followers: list[dict] = []
    for b in block.get("利好板块", []):
        if b.get("tag") not in (None, "利好"):   # 本块只列利好;非利好跳过
            continue
        board = b.get("board")
        strength = b.get("强弱")
        lead_list = b.get("龙头候选", []) or []
        board_moved = any(bool(d.get("已动")) for d in lead_list)
        for d in lead_list:
            code = d.get("code")
            if not code:
                continue
            leaders.append({
                "stream": MAIN_STREAM, "board": board, "code": code,
                "name": d.get("name", ""), "强弱": strength,
                "已动": d.get("已动"),
            })
        for d in b.get("跟涨候选", []) or []:
            code = d.get("code")
            if not code:
                continue
            followers.append({
                "stream": FOLLOW_STREAM, "board": board, "code": code,
                "name": d.get("name", ""), "强弱": strength,
                "board_已动": board_moved,           # 龙头是否已动(联动触发条件)
                "联动依据": d.get("联动依据", ""),
            })
    return leaders, followers


# ── 全A等权基准(复用 build_market_index 产物)─────────────────────────────
def _load_ew(data_root: str | None) -> dict[str, float]:
    """{date_str: ew_index};缺 parquet → {}(基准留 null,不阻塞绝对收益)。"""
    p = os.path.join(_resolve_root(data_root), _EW_REL)
    if not os.path.exists(p):
        return {}
    try:
        import pandas as pd
        df = pd.read_parquet(p, columns=["date", "ew_index"])
        return {str(x)[:10]: float(v) for x, v in zip(df["date"], df["ew_index"])}
    except Exception:  # noqa: BLE001
        return {}


# ── 个股 K 线(主档只读不触网)────────────────────────────────────────────
def _kline(code: str, cache: dict):
    """{code: (rows: list[(date,open,low,close)]|None, date2idx)}。缺票/坏档 → (None,{})。"""
    if code in cache:
        return cache[code]
    rows, d2i = None, {}
    try:
        from tools.collectors import market
        import pandas as pd
        df = market.load_kline(code).reset_index(drop=True)
        need = {"date", "open", "low", "close"}
        if need.issubset(df.columns):
            ds = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").tolist()
            o = df["open"].astype(float).tolist()
            lo = df["low"].astype(float).tolist()
            cl = df["close"].astype(float).tolist()
            rows = list(zip(ds, o, lo, cl))
            d2i = {d: i for i, d in enumerate(ds)}
    except Exception:  # noqa: BLE001 - 缺票/坏档:保守留 null
        rows, d2i = None, {}
    cache[code] = (rows, d2i)
    return cache[code]


def _entry_labels(code: str, signal_date: str, cache: dict, ew: dict) -> dict:
    """预注册回踩限价入场 + D+1/D+2 绝对收益 + 全A等权基准同期收益(超额)。

    入场(§3 回踩限价·不追高开):限价 = 信号日 D 收盘;次日 D+1 若 low ≤ 限价 → 成交,
    成交价 = min(限价, D+1 open)(低开跳空按开盘,更保守);否则未成交(高开未回踩)。
    收益:r_d1 = 成交价 → D+1 收盘 %;r_d2 = 成交价 → D+2 收盘 %。未到期留 None(待回填)。
    基准:bench_dN = 全A等权 D → D+N 收益%;excess_dN = r_dN − bench_dN。
    """
    rows, d2i = _kline(code, cache)
    out: dict = {
        "entry": {"limit_rule": LIMIT_RULE, "limit": None, "date": None,
                  "filled": None, "price": None, "note": None},
        "labels": {k: None for k in HORIZON_KEYS}
        | {f"bench_{k[2:]}": None for k in HORIZON_KEYS}
        | {f"excess_{k[2:]}": None for k in HORIZON_KEYS},
    }
    if rows is None:
        return out
    i = d2i.get(signal_date)
    if i is None:
        return out
    limit = rows[i][3]                       # D 收盘价(预注册限价)
    out["entry"]["limit"] = limit
    if i + 1 >= len(rows) or limit is None or limit <= 0:
        return out                            # D+1 未到期 → pending(entry 未知)
    d1_date, d1_open, d1_low, d1_close = rows[i + 1]
    out["entry"]["date"] = d1_date
    if d1_low <= limit:                       # 回踩到限价 → 成交
        price = min(limit, d1_open)           # 低开跳空按开盘成交(更保守)
        out["entry"]["filled"] = True
        out["entry"]["price"] = price
        out["labels"]["r_d1"] = (d1_close / price - 1.0) * 100.0
        if i + 2 < len(rows):
            out["labels"]["r_d2"] = (rows[i + 2][3] / price - 1.0) * 100.0
    else:                                     # 高开未回踩 → 不追高开、未成交(final)
        out["entry"]["filled"] = False
        out["entry"]["note"] = "高开未回踩(≥限价)、按纪律不追、未成交"
    # 全A等权基准同期(D → D+N),与是否成交无关,便于观察"信号日买入市场"对照
    base = ew.get(signal_date)
    if base:
        for n, key in ((1, "r_d1"), (2, "r_d2")):
            if i + n < len(rows):
                bv = ew.get(rows[i + n][0])
                if bv:
                    bench = (bv / base - 1.0) * 100.0
                    out["labels"][f"bench_d{n}"] = bench
                    r = out["labels"].get(key)
                    if r is not None:
                        out["labels"][f"excess_d{n}"] = r - bench
    return out


def _rec_status(rec: dict) -> str:
    """settled / partial / pending / not_entered(高开未成交=final,无收益)。"""
    filled = rec["entry"]["filled"]
    if filled is False:
        return "not_entered"
    cells = [rec["labels"].get(k) for k in HORIZON_KEYS]
    if all(c is not None for c in cells):
        return "settled"
    if any(c is not None for c in cells):
        return "partial"
    return "pending"


def _day_status(records: list[dict]) -> str:
    st = [r.get("status") for r in records]
    if not st:
        return "empty"
    live = [s for s in st if s != "not_entered"]
    if not live:
        return "settled"                       # 全未成交也算收敛(无待回填)
    if all(s == "settled" for s in live):
        return "settled"
    if any(s in ("settled", "partial") for s in live):
        return "partial"
    return "pending"


# ── 每日 shadow 记录器(non-gating·只写自己的 out_dir)───────────────────────
def run_daily(date: str, out_dir: str, data_root: str | None = None,
              force: bool = False) -> dict | None:
    """信号日 date 的「消息驱动」候选 → 龙头/跟涨两条 forward advisory(append-only 落盘)。

    返回 advisory dict;块缺失/as_of 不合规 → 返回 None(整段忽略、按常规,不落坏盘)。
    """
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{date}.json")
    if os.path.exists(out_path) and not force:
        logger.info("%s advisory 已存在(幂等跳过,--force 覆盖):%s", date, out_path)
        return _load_json(out_path)

    try:
        from tools.analysis.market_forecast.dataroot import ensure_data_root
        ensure_data_root(data_root)            # market.load_kline 需主档数据根
    except Exception:  # noqa: BLE001
        pass

    block = _news_block(date, data_root)
    if not block:
        logger.info("%s 无「消息驱动」块(整段忽略、按常规选股)", date)
        return None
    if not asof_ok(block, date):
        logger.warning("%s as_of=%s 晚于信号日或缺失 → as-of 守卫拒(防未来)",
                       date, block.get("as_of"))
        return None

    leaders, followers = extract_candidates(block)
    ew = _load_ew(data_root)
    cache: dict = {}

    def _score(cands: list[dict]) -> list[dict]:
        recs = []
        for c in cands:
            el = _entry_labels(c["code"], date, cache, ew)
            rec = dict(c)
            rec.update(el)
            rec["status"] = _rec_status(rec)
            recs.append(rec)
        return recs

    龙头票 = _score(leaders)
    跟涨票 = _score(followers)

    advisory = {
        "date": date, "as_of": block.get("as_of"),
        "非validated": True, "non_gating": True, "forward_only": True,
        "limit_rule": LIMIT_RULE, "horizon_keys": list(HORIZON_KEYS),
        "n_龙头候选": len(龙头票), "n_跟涨候选": len(跟涨票),
        "龙头票": 龙头票, "跟涨票": 跟涨票,
        "label_status": _day_status(龙头票 + 跟涨票),
        "note": ("消息驱动板块选股 forward 记分器(non-gating·纯记录);龙头(个股催化,主档)与"
                 "跟涨(联动观察·单列)严格分开;入场=回踩限价(D 收盘,D+1 回踩成交、不追高开),"
                 "收益=成交价→D+1/D+2 绝对收益%(未到期 null 待 --backfill 回填);基准=全A等权同期;"
                 "**绝不动 live 选股、绝不写 sector_focus.json**;样本 <120 只报 N。"),
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(advisory, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out_path)
    logger.info("sector_news_forward %s:龙头 %d / 跟涨 %d → %s",
                date, len(龙头票), len(跟涨票), out_path)
    return advisory


# ── 前向标签回填(遍历未结算 advisory,只填已到期 None cell;幂等·append-only)──
def backfill_labels(out_dir: str, data_root: str | None = None) -> dict:
    """对 label_status != settled 的 advisory 用主档 K 线回填**已到期** entry/r_dN(只填 None)。"""
    if not os.path.isdir(out_dir):
        return {"n_filled": 0, "days_touched": 0}
    try:
        from tools.analysis.market_forecast.dataroot import ensure_data_root
        ensure_data_root(data_root)
    except Exception:  # noqa: BLE001
        pass
    ew = _load_ew(data_root)
    cache: dict = {}
    n_filled = days_touched = 0
    for fn in sorted(os.listdir(out_dir)):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(out_dir, fn)
        adv = _load_json(p)
        if not adv or adv.get("label_status") == "settled":
            continue
        date = adv.get("date")
        changed = False
        for key in ("龙头票", "跟涨票"):
            for rec in adv.get(key, []):
                if rec.get("status") in ("settled", "not_entered"):
                    continue
                fresh = _entry_labels(rec["code"], date, cache, ew)
                # 只填 None → 已成交/已结算的 cell 绝不改写(append-only)
                for slot in ("filled", "price", "date", "limit", "note"):
                    if rec["entry"].get(slot) is None and fresh["entry"].get(slot) is not None:
                        rec["entry"][slot] = fresh["entry"][slot]
                        changed = True
                for lk, lv in fresh["labels"].items():
                    if rec["labels"].get(lk) is None and lv is not None:
                        rec["labels"][lk] = lv
                        if lk in HORIZON_KEYS:
                            n_filled += 1
                        changed = True
                new_status = _rec_status(rec)
                if new_status != rec.get("status"):
                    rec["status"] = new_status
                    changed = True
        if changed:
            adv["label_status"] = _day_status(adv.get("龙头票", []) + adv.get("跟涨票", []))
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(adv, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
            days_touched += 1
    return {"n_filled": n_filled, "days_touched": days_touched}


# ── forward 汇总(龙头/跟涨分开·分层;样本 <120 只报 N)────────────────────
def _stat(rows: list[float]) -> dict:
    if not rows:
        return {"n": 0, "均值绝对收益%": None, "为正占比": None}
    return {"n": len(rows), "均值绝对收益%": round(sum(rows) / len(rows), 3),
            "为正占比": round(sum(1 for x in rows if x > 0) / len(rows), 3)}


def _summ_stream(records: list[dict], *, by_已动: bool) -> dict:
    """一条流(龙头 or 跟涨)的分层汇总:总 + 按强弱 + 按已动(龙头)/board_已动(跟涨)。"""
    def collect(recs):
        out = {k: [] for k in HORIZON_KEYS}
        n_filled = 0
        for r in recs:
            if r["entry"].get("filled"):
                n_filled += 1
            for k in HORIZON_KEYS:
                v = r["labels"].get(k)
                if v is not None:
                    out[k].append(v)
        return out, n_filled

    obs, n_filled = collect(records)
    res = {
        "n_候选": len(records),
        "n_成交": n_filled,
        "n_未成交": sum(1 for r in records if r["entry"].get("filled") is False),
        "分横档": {k: _stat(obs[k]) for k in HORIZON_KEYS},
        "按强弱": {},
    }
    for lvl in STRENGTH_LEVELS:
        sub = [r for r in records if r.get("强弱") == lvl]
        so, _ = collect(sub)
        res["按强弱"][lvl] = {"n_候选": len(sub),
                              "分横档": {k: _stat(so[k]) for k in HORIZON_KEYS}}
    key = "已动" if by_已动 else "board_已动"
    res[f"按{key}"] = {}
    for flag in (True, False):
        sub = [r for r in records if bool(r.get(key)) is flag]
        so, _ = collect(sub)
        res[f"按{key}"][str(flag)] = {"n_候选": len(sub),
                                      "分横档": {k: _stat(so[k]) for k in HORIZON_KEYS}}
    return res


def summarize(out_dir: str) -> dict:
    """跨已积累 advisory 出分层汇总。龙头/跟涨严格分开;settled cell 累计 <120 → 只报 N。"""
    龙头: list[dict] = []
    跟涨: list[dict] = []
    n_days = 0
    if os.path.isdir(out_dir):
        for fn in sorted(os.listdir(out_dir)):
            if not fn.endswith(".json"):
                continue
            adv = _load_json(os.path.join(out_dir, fn))
            if not adv:
                continue
            n_days += 1
            龙头.extend(adv.get("龙头票", []))
            跟涨.extend(adv.get("跟涨票", []))

    def n_settled(recs):
        return sum(1 for r in recs for k in HORIZON_KEYS if r["labels"].get(k) is not None)

    n_lead_cells = n_settled(龙头)
    n_follow_cells = n_settled(跟涨)
    enough = (n_lead_cells >= MIN_SAMPLE)
    return {
        "n_days": n_days,
        "min_sample": MIN_SAMPLE,
        "龙头_settled_cell": n_lead_cells,
        "跟涨_settled_cell": n_follow_cells,
        "样本充足_龙头": enough,
        "样本充足_跟涨": n_follow_cells >= MIN_SAMPLE,
        "结论": ("样本充足,可看方向" if enough else
                 f"样本不足(龙头 settled cell {n_lead_cells} < {MIN_SAMPLE})→ 只报 N、不下结论"),
        "龙头": _summ_stream(龙头, by_已动=True),
        "跟涨_联动观察": _summ_stream(跟涨, by_已动=False),
        "读法": ("龙头 = 消息催化主 forward;跟涨_联动观察 = 补涨先锋单列验「龙头已动→接力」假设;"
                 "均值绝对收益%>基准全A等权 → 候选有 α;样本 <120 仅方向参考、绝不 gate 生产选股。"),
    }


# ── CLI(每日 shadow runner 入口,供 launchd 调度)──────────────────────────
def main(argv: list[str] | None = None) -> int:
    """每日 forward-shadow runner(non-gating)。
    退出码:0=成功/非交易日跳过/幂等跳过/块缺失/backfill/summary;非0=无。"""
    import argparse

    from tools.collectors import calendar as cal

    ap = argparse.ArgumentParser(
        description="消息驱动板块选股 M3 forward 记分器(纯记录·non-gating)")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD,缺省今天")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="advisory 落盘目录")
    ap.add_argument("--data-root", default=None, help="缺省自动探测含 analysis/ 的主仓数据根")
    ap.add_argument("--force", action="store_true", help="覆盖当日已有 advisory")
    ap.add_argument("--backfill", action="store_true", help="回填历史 advisory 已到期收益后退出")
    ap.add_argument("--summary", action="store_true", help="打印 forward 分层汇总后退出")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.summary:
        print(json.dumps(summarize(args.out_dir), ensure_ascii=False, indent=2))
        return 0
    if args.backfill:
        r = backfill_labels(args.out_dir, args.data_root)
        logger.info("backfill:填 %d cell,触及 %d 日", r["n_filled"], r["days_touched"])
        return 0

    date = args.date or datetime.now().strftime("%Y-%m-%d")
    if not cal.is_trading_day(date):
        logger.info("%s 非交易日,跳过 shadow", date)
        return 0

    run_daily(date, args.out_dir, args.data_root, force=args.force)
    # 收尾 best-effort 回填历史(幂等·只填 None cell),不影响退出码
    try:
        r = backfill_labels(args.out_dir, args.data_root)
        logger.info("backfill:填 %d cell,触及 %d 日", r["n_filled"], r["days_touched"])
    except Exception as e:  # noqa: BLE001
        logger.warning("backfill 失败(不阻塞):%r", e)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
