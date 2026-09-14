"""每日 forward 影子跑:v2r 候选池 → DeepSeek 研判 → forward 证据库(待回填标签)→ 样本外 α。

承接:docs/计划/2026-09-14_每日forward影子跑_v2r扩样接线计划.md
定位:**纯影子 / 不接 live**。构建于已合入 origin/main 的影子库之上(shadow_recall.build_pool_v2r
+ deep_analysis + shadow_score),零改动它们。每交易日盘后由 pull_refresh.sh 闭环末尾 best-effort
调用(`python -m tools.run shadow_forward`),无人值守增量攒**样本外**证据。

═══ 铁律(硬,测试锁死)═══
- 纯 dry-run 旁路:只写 `--evidence-dir`(默认 <cwd>/data/shadow_forward/),**绝不**写 live 选股
  产物(data/analysis/<日>/每日选股.json)、绝不动 live SKILL / write_picks / model_registry live
  路径 / 任何生产定时任务。
- 防未来:决策日只用当日盘后已披露数据(池 = 冻结的 v2r;close_0 = 信号日收盘)。前向标签
  r_1/r_5 = close[idx+N]/close[idx]-1,**未到期留 null**,将来结算由 backfill_labels 就地回填。
- 防偷看:v2r 池逻辑冻结、不按日调参;forward 只记录不调参。

═══ 收益 / α 口径 ═══
- 前向收益 canonical(与 tools/backtest/forward_scorecard.py 完全一致):
  r_N = (close[idx+N] / close[idx] - 1) * 100,close 来自 market.load_kline(code)(主档,不触网),
  idx = 该票 K线中 date==信号日 行下标;idx+N 越界(未到期)→ None。买入侧**全覆盖**(不受
  forward_scorecard 只含 live picks 限制)。
- α 基准 = 当日 forward_scorecard.csv 全样本 r 均值(闭环 ②.5 刚产出、shadow_score 已复用此口径、
  与 16 日 AB 同源)。⚠️ 该基准是 picks 样本对全A的代理,同 AB 局限,报告标注。
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from tools.analysis import deep_analysis as da
from tools.analysis import shadow_recall as sr
from tools.analysis import shadow_score as ss

BUY = ss.BUY                       # {"买入", "可参与"}(单一真源,复用 shadow_score)
DEFAULT_HORIZONS = (1, 5)          # T+1 / T+5 前向标签
PROVIDER_DEFAULT = "deepseek_v4pro"
VERSION = "v2r"


# ── 证据文件 I/O ────────────────────────────────────────────────────────────
def evidence_path(evidence_dir, date: str) -> Path:
    return Path(evidence_dir) / f"evidence_{date}.json"


def index_path(evidence_dir) -> Path:
    return Path(evidence_dir) / "_index.jsonl"


def _load_json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(p: Path, obj) -> None:
    Path(p).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# ── K线 / 前向收益(canonical,无未来函数)─────────────────────────────────────
def _kline_index(code: str, cache: dict):
    """返回 (close: list[float]|None, date2idx: dict[str,int])。按 code 缓存,主档只读不触网。"""
    if code in cache:
        return cache[code]
    close = None
    d2i: dict[str, int] = {}
    try:
        from tools.collectors import market
        df = market.load_kline(code).reset_index(drop=True)
        if "close" in df.columns and "date" in df.columns:
            close = df["close"].astype(float).tolist()
            d2i = {str(x)[:10]: i for i, x in enumerate(df["date"].tolist())}
    except Exception:  # noqa: BLE001 - 缺票/坏档:留空,标签保守留 null
        close, d2i = None, {}
    cache[code] = (close, d2i)
    return cache[code]


def _forward_return(close, idx, N: int):
    """r_N% = (close[idx+N]/close[idx]-1)*100;未到期/无效 → None。"""
    if close is None or idx is None:
        return None
    if 0 <= idx and idx + N < len(close) and close[idx] > 0:
        return float(close[idx + N] / close[idx] - 1.0) * 100.0
    return None


def _labels_for(code: str, date: str, horizons, cache: dict) -> dict:
    """单票标签:close_0(信号日收盘)+ 各 horizon 前向收益(未到期 None)。"""
    close, d2i = _kline_index(code, cache)
    idx = d2i.get(date)
    close_0 = (close[idx] if (close is not None and idx is not None
                              and 0 <= idx < len(close)) else None)
    lab = {"close_0": close_0}
    for N in horizons:
        lab[f"r_{N}"] = _forward_return(close, idx, N)
    return lab


def _label_status(labels: dict, horizons) -> str:
    """settled=所有票所有 horizon 到期;pending=一个都没到;partial=居中。"""
    cells = [labels[c].get(f"r_{N}") for c in labels for N in horizons]
    if not cells:
        return "pending"
    filled = sum(1 for v in cells if v is not None)
    if filled == 0:
        return "pending"
    return "settled" if filled == len(cells) else "partial"


# ── 证据索引(每日一行,按 date upsert → 幂等)───────────────────────────────
def append_index(evidence_dir, summary: dict) -> None:
    """把当日摘要 upsert 进 _index.jsonl(同 date 覆盖,保证一日一行、可重跑幂等)。"""
    p = index_path(evidence_dir)
    date = summary.get("date")
    lines = []
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if row.get("date") != date:      # 丢弃旧的同 date 行
                lines.append(json.dumps(row, ensure_ascii=False))
    lines.append(json.dumps(summary, ensure_ascii=False))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── ① 当日 forward 影子跑 ───────────────────────────────────────────────────
def run_forward_day(date: str, data_root, evidence_dir, provider: str = PROVIDER_DEFAULT,
                    exp_base=None, think=None, force: bool = False,
                    horizons=DEFAULT_HORIZONS, code_commit: str | None = None) -> dict:
    """建 v2r 池 → DeepSeek 研判全池 → 写当日证据(labels 待回填)→ upsert 索引。

    幂等:当日证据已存在且非 force → 跳过研判(不重复烧 token)。返回摘要 dict。
    """
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    out = evidence_path(evidence_dir, date)
    if out.exists() and out.stat().st_size > 2 and not force:
        prev = _load_json(out) or {}
        return {"date": date, "skipped": True, "reason": "exists",
                "pool_size": len(prev.get("pool", [])), "n_buy": len(prev.get("buy_side", []))}

    pool, meta = sr.build_pool_v2r(data_root, date)
    results = da.generate(date, pool, provider_id=provider, enable_thinking=think,
                          data_root=Path(data_root),
                          experience_base=Path(exp_base) if exp_base else None)
    units = da.units_of(results)
    errs = [{"code": r.code, "error": r.error} for r in results if r.error]
    buy_side = [u["code"] for u in units if u.get("stance") in BUY]

    cache: dict = {}
    labels = {code: _labels_for(code, date, horizons, cache) for code in pool}
    status = _label_status(labels, horizons)

    rec = {
        "date": date, "version": VERSION, "provider": provider,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "code_commit": code_commit,
        "horizons": list(horizons),
        "pool": list(pool),
        "pool_meta": meta,
        "units": units,
        "buy_side": buy_side,
        "errors": errs,
        "labels": labels,
        "label_status": status,
    }
    _write_json(out, rec)

    summary = {"date": date, "version": VERSION, "provider": provider,
               "pool_size": len(pool), "base_pool_size": meta.get("base_pool_size"),
               "recall_added": meta.get("recall_added"),
               "n_units": len(units), "n_err": len(errs),
               "n_buy": len(buy_side), "buy_side": buy_side,
               "label_status": status,
               "generated_at": rec["generated_at"]}
    append_index(evidence_dir, summary)
    return summary


# ── ② 前向标签回填(遍历未结算证据,填已到期 r_N;幂等)──────────────────────
def backfill_labels(evidence_dir, horizons=DEFAULT_HORIZONS) -> dict:
    """对所有 label_status != settled 的证据日,用主档 K线回填**已到期**的 r_N。

    只填当前为 None 的 cell(已填不动),幂等;未到期仍留 None。返回 {n_filled, days_touched}。
    """
    evidence_dir = Path(evidence_dir)
    cache: dict = {}
    n_filled = 0
    days_touched = 0
    for p in sorted(evidence_dir.glob("evidence_*.json")):
        rec = _load_json(p)
        if not rec or rec.get("label_status") == "settled":
            continue
        date = rec.get("date")
        hs = rec.get("horizons", list(horizons))
        labels = rec.get("labels", {})
        changed = False
        for code, lab in labels.items():
            need = [N for N in hs if lab.get(f"r_{N}") is None]
            if not need and lab.get("close_0") is not None:
                continue
            close, d2i = _kline_index(code, cache)
            idx = d2i.get(date)
            if lab.get("close_0") is None and close is not None and idx is not None \
                    and 0 <= idx < len(close):
                lab["close_0"] = close[idx]
                changed = True
            for N in need:
                r = _forward_return(close, idx, N)
                if r is not None:
                    lab[f"r_{N}"] = r
                    n_filled += 1
                    changed = True
        if changed:
            rec["label_status"] = _label_status(labels, hs)
            _write_json(p, rec)
            days_touched += 1
    return {"n_filled": n_filled, "days_touched": days_touched}


# ── ③ 样本外 α(复用 shadow_score;买入侧 r 用证据自带标签→全覆盖)────────────
def _day_card_from_labels(labels: dict, horizons) -> dict:
    """把证据 labels 转成 shadow_score 记分卡当日切片 {code: {r_1, r_5}}。"""
    return {c: {f"r_{N}": labels[c].get(f"r_{N}") for N in horizons} for c in labels}


def forward_alpha(evidence_dir, scorecard_path=None, horizons=DEFAULT_HORIZONS) -> dict:
    """跨已积累证据日算样本外 α:买入侧 & 看多方向侧,诚实标 N/SE。

    基准 = forward_scorecard 全样本当日均值(若可读);买入侧收益取证据自带标签(全覆盖)。
    为让基准贴近全A、又不漏掉未进 picks 的影子票,当日记分卡 = 全样本卡 ∪ 本池标签(池票覆盖式并入)。
    """
    evidence_dir = Path(evidence_dir)
    real_card = ss.load_scorecard(scorecard_path) if (
        scorecard_path and Path(scorecard_path).exists()) else {}

    buy_days, bull_days = [], []
    per_day = []
    for p in sorted(evidence_dir.glob("evidence_*.json")):
        rec = _load_json(p)
        if not rec:
            continue
        date = rec.get("date")
        units = rec.get("units", [])
        hs = rec.get("horizons", list(horizons))
        our_card = _day_card_from_labels(rec.get("labels", {}), hs)
        merged = dict(real_card.get(date, {}))
        merged.update(our_card)                    # 本池票覆盖式并入(全覆盖买入侧收益)
        card = {date: merged}
        rb = ss.score_day(date, units, card, want_t5=True, select=ss.is_buy)
        rl = ss.score_day(date, units, card, want_t5=True, select=ss.is_bull)
        buy_days.append(rb)
        bull_days.append(rl)
        h1 = rb["horizons"].get("r_1", {})
        per_day.append({"date": date, "n_units": rb["n_units"], "n_buy": rb["n_buy"],
                        "buy": rb["buy_side"], "alpha_r1": h1.get("alpha_pp"),
                        "hit_r1": h1.get("hit_rate"), "label_status": rec.get("label_status"),
                        "bench_from_scorecard": date in real_card})
    return {
        "n_days": len(buy_days),
        "days_with_ge1_buy": sum(1 for r in buy_days if r["n_buy"] >= 1),
        "total_buys": sum(r["n_buy"] for r in buy_days),
        "buy_agg": {hz: ss.aggregate(buy_days, horizon=hz) for hz in ("r_1", "r_5")},
        "bull_agg": {hz: ss.aggregate(bull_days, horizon=hz) for hz in ("r_1", "r_5")},
        "per_day": per_day,
    }
