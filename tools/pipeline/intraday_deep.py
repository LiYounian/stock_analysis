"""Headless 午盘深度选股 orchestrator（方案 b · launchd 免漂移）。

设计：docs/计划/2026-09-14_headless午盘深度选股_launchd免漂移_设计.md

编排（全程无 Claude 窗口，纯 Python 直调 DeepSeek）：
  0. 交易日门控 cal.is_trading_day → 非交易日 exit 0
  1. 候选门控：read_noon_candidates(stage1) 有界轮询就绪(≤deadline)→ Top-N codes(数据面综合分排序)
  2.（门控·生产开/手跑关）自采 Top-N 消息面(≤11:30 防未来 cutoff)：message + sentiment + events
  3. deep_analysis.generate(codes, provider=DeepSeek, news_time_cutoff=≤11:30) → units_of()
  4. compute_trade_plan(code, 11:30快照) → 每票 D-0 计划
  5. write_picks.build_picks_json → validate → 自写 日内深度选股.json（独立名，不碰 每日选股.json）
  6. 渲染 日内深度_<date>.md（PICKS锚点 + 市场环境 + 逐票深度 + 买入排序表 + D-0计划）

灰度纪律（统筹拍板 Q3）：首版产 `日内深度_<date>.md` + `日内深度选股.json`（**新名**），
**不动** watch / `日内_<date>.md` / 生产 PICKS 语义；跑 ~3-5 交易日观察后，换线（改名 + 接 watch）由统筹/用户做。

数据根：`--data-root` 指向 `data/` 父目录（手跑指生产只读）；派生 analysis_root=<root>/analysis、
intraday_root=<root>/intraday。缺省 = PROJECT_ROOT/data。**手跑 `--no-collect` 只读现有、绝不写生产。**
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
import time as _time
from pathlib import Path

from tools.analysis import deep_analysis as da
from tools.analysis import write_picks as wp
from tools.collectors import calendar as cal
from tools.pipeline import intraday_screen as isc

logger = logging.getLogger("pipeline.intraday_deep")

PRODUCT_MD_PREFIX = "日内深度"                 # 灰度期新名（不抢 日内_）
CANONICAL_JSON_NAME = "日内深度选股.json"      # 灰度期独立名（不碰 每日选股.json）
GENERATOR = "daily-stock-noon-deep"
NOON_FREEZE_LABEL = "11:30 午休冻结"
NOON_CUTOFF_TIME = "11:30:00"                  # 午休冻结时刻（≤此防未来）
DEFAULT_TOP = 5
DEFAULT_STAGE = "stage1"
DEFAULT_POLL_INTERVAL = 180                    # 候选未就绪重探间隔（秒）
DEFAULT_POLL_DEADLINE = "12:20"                # 有界轮询上限（本地时刻 HH:MM）

# 买入表纳入 PICKS 的 stance（买入候选主体；观望/规避不进 PICKS）
_BUY_STANCES = {"买入", "可参与"}


# ————————————————————————————————————————————————————————————————
# 数据根 / loader
# ————————————————————————————————————————————————————————————————
def _data_roots(data_root: Path | None) -> tuple[Path, Path]:
    """--data-root(=data/ 父目录) → (analysis_root, intraday_root)。缺省 PROJECT_ROOT/data。"""
    if data_root is None:
        from tools.config import settings
        data_root = settings.PROJECT_ROOT / "data"
    data_root = Path(data_root)
    return data_root / "analysis", data_root / "intraday"


def _make_record_loader(analysis_root: Path):
    """给 write_picks.build_picks_json 的 record_loader：从 analysis_root 只读 <date>/<code>.json。"""
    def _load(code: str, pick_date: str):
        p = Path(analysis_root) / pick_date / f"{code}.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        except Exception:
            return None
    return _load


# ————————————————————————————————————————————————————————————————
# 0. 交易日门控
# ————————————————————————————————————————————————————————————————
def gate_trading_day(as_of: str, *, force: bool = False) -> bool:
    """非交易日返回 False（调用方 exit 0 跳过）。--force 旁路（联调）。"""
    if force:
        return True
    try:
        ok = cal.is_trading_day(as_of)
    except Exception as e:                                # noqa: BLE001 日历失败保守放行 + 告警
        logger.warning("交易日历判定失败(%s)，保守放行继续", str(e)[:80])
        return True
    if not ok:
        logger.info("非交易日 %s，午盘深度选股跳过(退 0)", as_of)
    return ok


# ————————————————————————————————————————————————————————————————
# 1. 候选门控（有界轮询 + 降级）
# ————————————————————————————————————————————————————————————————
def _deadline_dt(as_of: str, hhmm: str, now: _dt.datetime) -> _dt.datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def poll_candidates(
    as_of: str,
    intraday_root: Path,
    *,
    top: int,
    stage: str = DEFAULT_STAGE,
    deadline_hhmm: str = DEFAULT_POLL_DEADLINE,
    interval_s: float = DEFAULT_POLL_INTERVAL,
    selection_dir: Path | None = None,
    now_fn=None,
    sleep_fn=None,
    max_polls: int | None = None,
) -> tuple[dict | None, str]:
    """有界轮询等 stage1 候选就绪；超期降级 auto(读 日内全A_ 台账)。返回 (cand|None, source_note)。

    - 就绪(read_noon_candidates 非 None)→ 立即返回。
    - 未就绪 → 每 interval_s 重探，至 deadline_hhmm 本地时刻。
    - 超期仍无 → 最后一次 stage='auto'(全量优先→stage1→md 台账回退)；仍无 → (None, 'unavailable')。
    """
    now_fn = now_fn or (lambda: _dt.datetime.now().astimezone())
    sleep_fn = sleep_fn or _time.sleep
    deadline = _deadline_dt(as_of, deadline_hhmm, now_fn())
    polls = 0
    while True:
        cand = isc.read_noon_candidates(as_of, root=intraday_root, selection_dir=selection_dir,
                                        top=top, stage=stage)
        if cand is not None:
            return cand, f"stage={stage}:source={cand.get('source')}"
        polls += 1
        over_deadline = now_fn() >= deadline
        over_maxpolls = max_polls is not None and polls >= max_polls
        if over_deadline or over_maxpolls:
            # 降级：auto 回退（全量→stage1→md 台账）
            cand = isc.read_noon_candidates(as_of, root=intraday_root, selection_dir=selection_dir,
                                            top=top, stage="auto")
            if cand is not None:
                return cand, f"降级 stage=auto:source={cand.get('source')}(stage1 未在 {deadline_hhmm} 前就绪)"
            return None, f"unavailable(stage1 及 auto 回退均无，deadline {deadline_hhmm})"
        logger.info("stage1 候选未就绪(第 %d 探)，%ds 后重探(deadline %s)", polls, int(interval_s), deadline_hhmm)
        sleep_fn(interval_s)


def topn_codes(cand: dict, top: int) -> tuple[list[str], dict]:
    """从候选台账取 Top-N（按数据面综合分降序）codes + strategies_hint_map(候选来源→提示)。"""
    ledger = cand.get("台账") or []

    def _score(x: dict):
        s = x.get("数据面综合分")
        if s is None:
            s = x.get("完整分")
        return s if isinstance(s, (int, float)) else float("-inf")

    ranked = sorted(ledger, key=_score, reverse=True)
    codes: list[str] = []
    hint_map: dict = {}
    for x in ranked:
        code = str(x.get("code") or "").strip()
        if not code or code in hint_map:
            continue
        codes.append(code)
        srcs = x.get("候选来源") or ""
        if isinstance(srcs, str):
            names = [s.strip() for s in srcs.replace("、", ",").split(",") if s.strip()]
        elif isinstance(srcs, list):
            names = [str(s).strip() for s in srcs if str(s).strip()]
        else:
            names = []
        if names:
            hint_map[code] = [{"name": n} for n in names]
        else:
            hint_map[code] = []
        if len(codes) >= top:
            break
    return codes, hint_map


# ————————————————————————————————————————————————————————————————
# 2. 自采 Top-N 消息面（门控；生产开/手跑关）
# ————————————————————————————————————————————————————————————————
def collect_topn_sentiment(codes: list[str], as_of: str, *, workers: int = 5) -> list[str]:
    """对 Top-N 自采 news + 三层情绪 + 事件（≤11:30 防未来在 deep 读取侧 cutoff 落实）。

    ⚠️ collectors 经 store 写 PROJECT_ROOT/data（无 data-root 覆盖）——**仅生产(dailyjob worktree)调用**；
    手跑走 --no-collect 只读现有档。返回采集告警 notes（不阻断，缺失由 deep 读取侧标 missing）。
    """
    notes: list[str] = []
    from tools import run as run_mod
    from tools.store import repo as store
    try:
        store.set_active_date(as_of)
    except Exception as e:                                # noqa: BLE001
        notes.append(f"set_active_date 失败:{str(e)[:60]}")
    for step, fn in (
        ("message", lambda: run_mod.collect_message(codes, workers=workers)),
        ("sentiment", lambda: run_mod.run_sentiment(codes, workers=workers)),
        ("events", lambda: run_mod.run_events(codes, as_of)),
    ):
        try:
            fn()
        except Exception as e:                            # noqa: BLE001 单步失败不阻断，记 note
            notes.append(f"{step} 采集失败(不阻断):{str(e)[:80]}")
            logger.warning("Top-N %s 采集失败(不阻断):%s", step, str(e)[:120])
    return notes


# ————————————————————————————————————————————————————————————————
# 3+4. 深度研判 + D-0 交易计划
# ————————————————————————————————————————————————————————————————
def load_quotes(as_of: str, intraday_root: Path) -> dict:
    """读 11:30 冻结全A快照 noon_screen_snapshot.json → {code: quote}。缺 → {}。"""
    p = isc.noon_snapshot_path(as_of, out_root=intraday_root)
    try:
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if isinstance(raw, dict) and "quotes" in raw and isinstance(raw["quotes"], dict):
        return raw["quotes"]
    return raw if isinstance(raw, dict) else {}


def build_trade_plans(codes: list[str], quotes: dict, as_of: str) -> dict:
    """对给定票（一般是买入候选）算 D-0 交易计划。停牌/无快照 → 该票缺。"""
    plans: dict = {}
    for code in codes:
        plan = isc.compute_trade_plan(code, quotes.get(code), as_of)
        if plan is not None:
            plans[code] = plan
    return plans


def run_deep(
    as_of: str,
    codes: list[str],
    *,
    analysis_root: Path,
    provider_id: str | None,
    enable_thinking: bool | None,
    strategies_hint_map: dict,
    news_time_cutoff: str | None,
    client=None,
) -> list:
    """deep_analysis.generate 逐票 DeepSeek 研判。news_time_cutoff=<date> HH:MM:SS 时按全时间戳≤cutoff 防未来。"""
    return da.generate(
        as_of, codes,
        client=client,
        provider_id=provider_id,
        enable_thinking=enable_thinking,
        data_root=Path(analysis_root),
        strategies_hint_map=strategies_hint_map,
        news_time_cutoff=news_time_cutoff,
    )


# ————————————————————————————————————————————————————————————————
# 5. 装配 + 校验 + 独立落盘
# ————————————————————————————————————————————————————————————————
def _is_buy(unit: dict) -> bool:
    return (unit.get("stance") in _BUY_STANCES) or (unit.get("type") == "买入候选")


def assign_buy_rank(units: list[dict], codes_order: list[str]) -> list[dict]:
    """给买入候选按 codes_order(数据面综合分序)赋 buy_rank 1..K；非买入票不给 buy_rank。"""
    order = {c: i for i, c in enumerate(codes_order)}
    buys = [u for u in units if _is_buy(u)]
    buys.sort(key=lambda u: order.get(u.get("code"), 1 << 30))
    for rank, u in enumerate(buys, start=1):
        u["buy_rank"] = rank
    return units


def assemble_and_validate(
    as_of: str,
    units: list[dict],
    *,
    analysis_root: Path,
    market_regime: str,
    source_md: str,
    predict_for: str | None = None,
) -> tuple[dict, list[str]]:
    """build_picks_json（客观回填 + 校验）。picks = 买入候选（PICKS/买入表主体）。返回 (doc, errors)。"""
    buy_units = [u for u in units if _is_buy(u)]
    status = "ok" if buy_units else "skipped"
    skip_reason = None if buy_units else "Top-N 逐票深度后无买入候选(全为检验/观望/规避)"
    doc, errors = wp.build_picks_json(
        as_of, buy_units,
        status=status,
        skip_reason=skip_reason,
        market_regime=market_regime,
        generator=GENERATOR,
        source_md=source_md,
        predict_for=predict_for,
        analysis_dir=Path(analysis_root),
        record_loader=_make_record_loader(analysis_root),
    )
    return doc, errors


def write_canonical_json(doc: dict, analysis_root: Path, as_of: str, *, name: str = CANONICAL_JSON_NAME) -> str:
    """原子写独立名 canonical JSON（灰度期不碰 每日选股.json）。"""
    p = Path(analysis_root) / as_of / name
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return str(p)


# ————————————————————————————————————————————————————————————————
# 6. md 渲染（逐票深度叙事 + 复用 write_picks 买入表 + PICKS 锚点）
# ————————————————————————————————————————————————————————————————
def _buy_codes(units: list[dict]) -> list[str]:
    buys = [u for u in units if _is_buy(u)]
    buys.sort(key=lambda u: (u.get("buy_rank") is None, u.get("buy_rank") or 0))
    return [str(u.get("code")) for u in buys if u.get("code")]


def _picks_anchor(units: list[dict]) -> str:
    codes = _buy_codes(units)
    return f"<!-- PICKS: {','.join(codes) if codes else 'none'} -->"


def render_md(
    as_of: str,
    doc: dict,
    results: list,
    *,
    units: list[dict],
    trade_plans: dict,
    market_ctx: str,
    timing: dict,
    degrade_note: str = "",
    cutoff_desc: str = "",
    collect_note: str = "",
) -> str:
    """渲染 日内深度_<date>.md。第一行=PICKS 锚点(买入票)。逐票深度叙事全部来自 units(无额外 LLM)。"""
    by_code = {u.get("code"): u for u in units}
    L: list[str] = []
    # 机读锚点（硬）：第一行
    L.append(_picks_anchor(units))
    L.append("")
    L.append(f"# 午盘深度选股（全A候选池 Top-N 逐票深度）· {as_of}")
    L.append("")
    L.append("> ⚠️ 测试环境研究模拟，非投资建议。**灰度并跑产物（headless DeepSeek），不接 watch/PICKS 生产语义。**")
    L.append(f"> 数据时点：{NOON_FREEZE_LABEL}（半日 bar，量能按半日语境读）｜范围：全A午盘候选池 Top-N（**非盯盘集/自选**）")
    L.append(f"> 模型：DeepSeek headless（think {'开' if timing.get('think') else '关'}）｜生成器：{GENERATOR}")
    # 时点自证
    L.append(f"> 时点自证：触发预期 ~11:50 ｜实际执行 {timing.get('exec_time','?')} ｜数据采集(冻结) 11:30"
             + (f" ｜{timing.get('drift_note')}" if timing.get('drift_note') else ""))
    if cutoff_desc:
        L.append(f"> 防未来：消息面 {cutoff_desc}")
    if degrade_note:
        L.append(f"> ⚠️ 候选来源：{degrade_note}")
    if collect_note:
        L.append(f"> 消息面采集：{collect_note}")
    L.append("")
    # 市场环境
    L.append("## 市场环境（β 背景）")
    L.append("")
    L.append(market_ctx or "（无 market_forecast，未取大盘背景）")
    L.append("")
    # 逐票深度
    L.append("## 逐票深度")
    L.append("")
    for r in results:
        u = by_code.get(r.code) or {"code": r.code}
        name = _unit_name(u, doc)
        L.append(f"### {u.get('buy_rank') and str(u['buy_rank'])+'. ' or ''}{r.code} {name}")
        if r.error:
            L.append(f"- ⚠️ 研判失败/跳过：{r.error}")
            L.append("")
            continue
        L.append(f"- **类型/表态**：{u.get('type','?')} · {u.get('stance','?')}"
                 + (f"（{u.get('stance_qualifier')}）" if u.get('stance_qualifier') else ""))
        L.append(f"- **方向**：1日 {u.get('dir_1d','?')}/{u.get('dir_1d_conf','?')}"
                 f"　5日 {u.get('dir_5d','?')}/{u.get('dir_5d_conf','?')}")
        L.append(f"- **情绪质量**：{u.get('sentiment_quality','?')}")
        if u.get("key_reason"):
            L.append(f"- **核心理由**：{u['key_reason']}")
        if u.get("key_risk"):
            L.append(f"- **核心风险**：{u['key_risk']}")
        if u.get("alpha_beta"):
            L.append(f"- **α/β 拆分**：{u['alpha_beta']}")
        wpz = u.get("watch_points") or []
        if wpz:
            L.append("- **盯点（单问化）**：")
            for w in wpz:
                L.append(f"    - {w}")
        plan = trade_plans.get(r.code)
        if plan:
            L.append(f"- **D-0 交易计划**：现价 {plan.get('现价11:30')}｜{plan.get('进场触发')}｜"
                     f"止损 {plan.get('止损位')}(−{plan.get('止损距离%')}%)｜止盈 {plan.get('止盈位')}(+{plan.get('止盈距离%')}%)｜"
                     f"锚 {plan.get('波动锚')}｜{plan.get('了结')}")
        if r.coercions:
            L.append(f"- _枚举兜底 {len(r.coercions)} 处：{'；'.join(r.coercions[:3])}_")
        L.append("")
    # 买入排序表（复用 write_picks）
    L.append("## 今日可买入排序")
    L.append("")
    L.append(wp.render_picks_md_tables(doc))
    L.append("")
    # D-0 计划表（买入票）
    buy_codes = _buy_codes(units)
    if any(c in trade_plans for c in buy_codes):
        L.append("## D-0 交易计划（进场/止损/止盈/了结）")
        L.append("")
        L.append("| 代码 | 名称 | 现价(11:30) | 进场触发 | 止损位 | 止盈位 | 波动锚 | 了结 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for c in buy_codes:
            p = trade_plans.get(c)
            u = by_code.get(c) or {}
            if not p:
                L.append(f"| {c} | {_unit_name(u, doc)} | — | _无快照(停牌?)_ | — | — | — | 当日收盘了结 |")
                continue
            L.append(f"| {c} | {_unit_name(u, doc)} | {p.get('现价11:30')} | {p.get('进场触发')} | "
                     f"{p.get('止损位')}(−{p.get('止损距离%')}%) | {p.get('止盈位')}(+{p.get('止盈距离%')}%) | "
                     f"{p.get('波动锚')} | {p.get('了结')} |")
        L.append("")
    return "\n".join(L)


def _unit_name(unit: dict, doc: dict) -> str:
    """从 doc.picks 回填的 name 取名（回填后权威）；缺则空。"""
    for p in (doc.get("picks") or []):
        if str(p.get("code")) == str(unit.get("code")):
            return p.get("name") or ""
    return ""


def market_context(as_of: str, analysis_root: Path) -> str:
    """读 market_forecast.json 出一句 β 背景定性。缺 → 空串。"""
    p = Path(analysis_root) / as_of / "market_forecast.json"
    try:
        mf = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return ""
    bits = []
    for k in ("市场环境", "regime", "定性", "结论"):
        if isinstance(mf.get(k), str):
            bits.append(mf[k])
            break
    breadth = mf.get("广度") or mf.get("breadth")
    if isinstance(breadth, dict):
        bits.append(f"广度：{json.dumps(breadth, ensure_ascii=False)}")
    return "；".join(bits) if bits else "（market_forecast 已读，未取到定性字段）"


# ————————————————————————————————————————————————————————————————
# 主编排
# ————————————————————————————————————————————————————————————————
def run(
    as_of: str,
    *,
    data_root: Path | None = None,
    out_dir: Path | None = None,
    top: int = DEFAULT_TOP,
    stage: str = DEFAULT_STAGE,
    provider_id: str | None = None,
    enable_thinking: bool | None = False,
    collect: bool = True,
    news_cutoff: bool = True,
    force: bool = False,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL,
    poll_deadline: str = DEFAULT_POLL_DEADLINE,
    selection_dir: Path | None = None,
    deep_client=None,
    now_fn=None,
) -> int:
    """端到端编排。返回退出码（0=成功/非交易日跳过/无买入候选；非 0=失败）。"""
    now_fn = now_fn or (lambda: _dt.datetime.now().astimezone())
    exec_dt = now_fn()
    analysis_root, intraday_root = _data_roots(data_root)

    # 0. 交易日门控
    if not gate_trading_day(as_of, force=force):
        return 0

    # 1. 候选门控
    cand, source_note = poll_candidates(
        as_of, intraday_root, top=top, stage=stage,
        interval_s=poll_interval_s, deadline_hhmm=poll_deadline,
        selection_dir=selection_dir, now_fn=now_fn)
    if cand is None:
        logger.warning("候选不可用(%s)，产 PICKS:none 跳过留痕", source_note)
        _write_skip(as_of, analysis_root, out_dir, reason=f"候选池未就绪:{source_note}", exec_dt=exec_dt)
        return 0
    codes, hint_map = topn_codes(cand, top)
    if not codes:
        logger.warning("候选台账为空，产 PICKS:none 跳过留痕")
        _write_skip(as_of, analysis_root, out_dir, reason="候选台账为空", exec_dt=exec_dt)
        return 0
    logger.info("Top-%d 候选(数据面综合分序)：%s（%s）", top, ",".join(codes), source_note)

    # 2. 自采消息面（门控）
    collect_note = ""
    if collect:
        cnotes = collect_topn_sentiment(codes, as_of)
        collect_note = "自采 message+sentiment+events" + (f"；告警:{'；'.join(cnotes)}" if cnotes else "")
    else:
        collect_note = "未自采（只读现有 news_ai/sentiment，手跑/隔离模式）"

    # 3. 深度研判
    cutoff = f"{as_of} {NOON_CUTOFF_TIME}" if news_cutoff else None
    cutoff_desc = f"news 全时间戳 ≤{cutoff}" if cutoff else "未启用 intraday cutoff（date 级）"
    results = run_deep(
        as_of, codes,
        analysis_root=analysis_root, provider_id=provider_id, enable_thinking=enable_thinking,
        strategies_hint_map=hint_map, news_time_cutoff=cutoff, client=deep_client)
    units = da.units_of(results)
    if not units:
        logger.warning("deep_analysis 全票失败，产 PICKS:none 跳过留痕")
        _write_skip(as_of, analysis_root, out_dir, reason="deep_analysis 全票失败", exec_dt=exec_dt)
        return 1

    # 4. D-0 计划（买入票）
    quotes = load_quotes(as_of, intraday_root)
    units = assign_buy_rank(units, codes)
    buy_codes = _buy_codes(units)
    trade_plans = build_trade_plans(buy_codes, quotes, as_of)

    # 5. 装配 + 校验 + 落 canonical JSON（独立名）
    md_dir = Path(out_dir) if out_dir else _default_md_dir()
    source_md = f"{_md_rel(md_dir)}/{PRODUCT_MD_PREFIX}_{as_of}.md"
    market_regime = market_context(as_of, analysis_root)
    doc, errors = assemble_and_validate(
        as_of, units, analysis_root=analysis_root,
        market_regime=market_regime, source_md=source_md,
        predict_for=cand.get("as_of") and None)
    if errors:
        logger.error("每日选股 canonical 校验未过(%d)：%s", len(errors), "；".join(errors[:5]))
        # 校验不过不落 canonical JSON，但仍产 md（灰度观察）+ 显式标注
        json_path = None
    else:
        json_path = write_canonical_json(doc, analysis_root, as_of)

    # 6. 渲染 md
    drift_note = _drift_note(exec_dt)
    timing = {"exec_time": exec_dt.strftime("%H:%M"), "think": enable_thinking, "drift_note": drift_note}
    md = render_md(
        as_of, doc, results, units=units, trade_plans=trade_plans,
        market_ctx=market_regime, timing=timing,
        degrade_note=source_note if "降级" in source_note else "",
        cutoff_desc=cutoff_desc, collect_note=collect_note)
    md_path = _write_md(md, md_dir, as_of)
    logger.info("已产：md=%s json=%s（买入 %d 票：%s）",
                md_path, json_path or "(校验未过,未落)", len(buy_codes), ",".join(buy_codes) or "无")
    print(f"[intraday_deep] {as_of} 买入 {len(buy_codes)} 票 {buy_codes} → {md_path}", file=sys.stderr)
    return 0


def _default_md_dir() -> Path:
    from tools.config import settings
    return settings.PROJECT_ROOT / "docs" / "每日分析" / "选股"


def _md_rel(md_dir: Path) -> str:
    try:
        from tools.config import settings
        return str(Path(md_dir).relative_to(settings.PROJECT_ROOT))
    except Exception:
        return "docs/每日分析/选股"


def _write_md(md: str, md_dir: Path, as_of: str) -> str:
    md_dir = Path(md_dir)
    md_dir.mkdir(parents=True, exist_ok=True)
    p = md_dir / f"{PRODUCT_MD_PREFIX}_{as_of}.md"
    p.write_text(md, encoding="utf-8")
    return str(p)


def _write_skip(as_of: str, analysis_root: Path, out_dir: Path | None, *, reason: str, exec_dt: _dt.datetime) -> None:
    """跳过留痕：md 首行 PICKS:none + 原因；canonical JSON status=skipped（独立名）。"""
    md_dir = Path(out_dir) if out_dir else _default_md_dir()
    md = "\n".join([
        "<!-- PICKS: none -->",
        "",
        f"# 午盘深度选股（跳过）· {as_of}",
        "",
        "> ⚠️ 测试环境研究模拟，非投资建议。灰度并跑产物。",
        f"> 跳过原因：{reason}",
        f"> 时点自证：实际执行 {exec_dt.strftime('%H:%M')}",
    ])
    _write_md(md, md_dir, as_of)
    try:
        doc, _ = wp.build_picks_json(
            as_of, [], status="skipped", skip_reason=reason, generator=GENERATOR,
            analysis_dir=Path(analysis_root), record_loader=_make_record_loader(analysis_root))
        write_canonical_json(doc, analysis_root, as_of)
    except Exception as e:                                # noqa: BLE001
        logger.warning("跳过态 canonical JSON 落盘失败(不阻断):%s", str(e)[:80])


def _drift_note(exec_dt: _dt.datetime) -> str:
    """相对 13:00 漂移标注（launchd 下应恒 <13:00，仍自证留痕）。"""
    open_dt = exec_dt.replace(hour=13, minute=0, second=0, microsecond=0)
    if exec_dt < open_dt:
        return ""
    drift_min = int((exec_dt - open_dt).total_seconds() // 60)
    return (f"⚠️ 实际执行 {exec_dt.strftime('%H:%M')} 晚于下午开盘 13:00，漂移 {drift_min}min，"
            f"仅供研究复盘参考、不作当日实时操作依据")


# ————————————————————————————————————————————————————————————————
# CLI
# ————————————————————————————————————————————————————————————————
def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="headless 午盘深度选股 orchestrator（launchd 免漂移）")
    ap.add_argument("--date", help="选股日 YYYY-MM-DD，默认今天")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP, help=f"深挖只数，默认 {DEFAULT_TOP}")
    ap.add_argument("--stage", default=DEFAULT_STAGE, choices=["stage1", "full", "auto"])
    ap.add_argument("--provider", help="provider id（缺省走 deep_analysis 路由主 provider=DeepSeek）")
    ap.add_argument("--think", choices=["on", "off"], default="off", help="think 开关，默认关（午盘省时延）")
    ap.add_argument("--data-root", help="data/ 父目录（手跑指生产只读）；缺省 PROJECT_ROOT/data")
    ap.add_argument("--out-dir", help="md 输出目录（手跑指 scratchpad 隔离）；缺省 docs/每日分析/选股")
    ap.add_argument("--no-collect", action="store_true", help="不自采消息面（手跑/隔离；只读现有档，绝不写生产）")
    ap.add_argument("--no-news-cutoff", action="store_true", help="关闭 ≤11:30 intraday 防未来 cutoff（联调用，慎用）")
    ap.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL, help="候选未就绪重探间隔秒")
    ap.add_argument("--poll-deadline", default=DEFAULT_POLL_DEADLINE, help="候选有界轮询上限 HH:MM")
    ap.add_argument("--force", action="store_true", help="旁路交易日门控（联调）")
    args = ap.parse_args(argv)

    from tools.store import repo as store
    as_of = args.date or store._today()
    data_root = Path(args.data_root) if args.data_root else None
    out_dir = Path(args.out_dir) if args.out_dir else None
    enable_thinking = {"on": True, "off": False}[args.think]

    return run(
        as_of,
        data_root=data_root, out_dir=out_dir, top=args.top, stage=args.stage,
        provider_id=args.provider, enable_thinking=enable_thinking,
        collect=not args.no_collect, news_cutoff=not args.no_news_cutoff,
        force=args.force, poll_interval_s=args.poll_interval, poll_deadline=args.poll_deadline)


if __name__ == "__main__":
    raise SystemExit(_main())
