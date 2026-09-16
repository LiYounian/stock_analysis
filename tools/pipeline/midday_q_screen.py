"""午盘 Q · Q1/Q2/Q3 选股 pipeline(launchd 触发)。

三段式,仿 midday_q_gate 结构:
    stage="1430" → 读 gate.stage1_1430 → 触发 dispatch → 落 screen.stage1_1430
    stage="1450" → 读 gate.stage2_1450 + stage1 结果作 confirm_from → 落 screen.stage2_1450
    stage="final" → 读 gate.final + screen.stage2 → 若 gate.final 空仓 → 清空;否则取 stage2 为 final

数据覆盖率闸门(2026-09-16 加,见 MIN_UNIVERSE_COVERAGE):
    每段落盘都带 universe_coverage / data_insufficient。首判数据不足时,
    1450 不继承其空集(否则永久锁死无法选出任何票),改为退回全票池重筛并
    标 confirm_degraded —— 单点命中不冒充双点确认,标记透传到 final。

依赖(上游必须先落):
    · data/analysis/midday_q/gate_<date>.json                              (M1 pipeline 落)
    · data/intraday/<date>/T<slot>.json                                    (intraday_snapshot 落)
      ⚠️ 午盘 Q 需要**扫焦点池**,故调用 intraday_snapshot 时必须传 --codes <焦点池>;
         不传的话它默认只跟踪"上一交易日选股 ∪ 自选池"(约 18 只),覆盖率会不达标。
    · Q3 触发时:M1 fundflow_intraday.collect(候选池)

纪律:
    · 幂等/非交易日跳过/上游缺失 exit 1(同 midday_q_gate 家族)

用法:
    python -m tools.pipeline.midday_q_screen --stage 1430
    python -m tools.pipeline.midday_q_screen --stage 1450
    python -m tools.pipeline.midday_q_screen --stage final

⚠️ 测试环境研究用,非投资建议;只读行情、不下单。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from tools.analysis.midday_q import gate as G
from tools.collectors import calendar as cal
from tools.config import settings
from tools.config import midday_q_universe as UNIV
from tools.strategy import midday_q_screener as SC

logger = logging.getLogger("pipeline.midday_q_screen")

SCRIPT_VERSION = "0.1.0"
LOG_PATH = settings.PROJECT_ROOT / "logs" / "midday_q_screen.log"

Stage = Literal["1430", "1450", "final"]
_STAGES: tuple[Stage, ...] = ("1430", "1450", "final")

_STAGE_AS_OF: dict[str, str] = {"1430": "14:30", "1450": "14:50"}
_STAGE_KEY: dict[str, str] = {
    "1430": "stage1_1430",
    "1450": "stage2_1450",
    "final": "final",
}
_GATE_STAGE_KEY: dict[str, str] = {
    "1430": "stage1_1430",
    "1450": "stage2_1450",
    "final": "final",
}

# ── 数据覆盖率闸门(2026-09-16 加) ────────────────────────────────────────
# 为什么要这个:原实现里"首判选出 0 只"有两种截然不同的成因,但落盘长得一模一样:
#   (a) 真实无信号 —— 票池数据齐全,只是今天没有票满足阈值 → 空是**正确结论**
#   (b) 数据故障   —— 快照只覆盖到票池的一小部分,根本没看全 → 空是**无效结论**
# 而 1450 复核用 confirm_from 把候选锁死在首判结果里(方案要求"两次都命中才买"),
# 于是 (b) 情形下首判的空集会**永久污染**复核:哪怕 1450 数据补全到 126/126,
# 候选范围仍是空集,数学上不可能选出任何票。
# 实例:2026-09-16 首判快照只有 18 只(intraday_snapshot 默认只跟踪已知标的,
# 与午盘 Q"扫固定焦点池"的需求不匹配),∩焦点池后仅 6 只 → 覆盖率 4.8% → 全天 0 只。
#
# 故:落盘记录覆盖率,低于门限时标 data_insufficient,让下游能区分 (a) / (b)。
MIN_UNIVERSE_COVERAGE = 0.6      # 首版门限(与 strategy.json"下午基准取样率下限"同值);
                                  # 低于此视为"没看全票池",其"空"结论不可继承


def _screen_path(date: str) -> Path:
    """产出路径 data/analysis/midday_q/screen_<date>.json。"""
    return settings.PROJECT_ROOT / "data" / "analysis" / "midday_q" / f"screen_{date}.json"


def _load_json_file(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("读文件失败 %s: %s", p, e)
        return None


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".screen-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


# ────────────────────────────── 上游读取 ──────────────────────────────

def _load_snapshot(date: str, slot: str) -> dict | None:
    """读 data/intraday/<date>/T<slot>.json(intraday_snapshot 落盘)。缺失 → None。"""
    from tools.pipeline.intraday_snapshot import snapshot_path
    return _load_json_file(snapshot_path(date, slot))


def _quotes_from_snapshot(snapshot: dict) -> dict[str, dict]:
    """snapshot.codes → {code: 报价字段}(gtimg_quote 已归一)。"""
    return dict(snapshot.get("codes") or {})


# ────────────────────────────── extras 预算(Q3 拉资金流) ──────────────────────────────

def _build_extras(date: str, as_of: str, quotes: dict[str, dict],
                    candidate_codes: list[str] | None,
                    need_fundflow: bool) -> dict:
    """为 dispatch 组装 extras:

    - t1_klines:{code: DataFrame} 从本地主档读 T-1 及以前日线(近 80 行,够算 MA60)
    - fundflow:  {code: DataFrame} 对候选池拉分时资金流(仅 need_fundflow=True)
    - listing_days / sector_of / sector_ranks:M2 阶段先返回空 dict(未来补)
    """
    extras: dict = {
        "listing_days": {},
        "sector_of": {},
        "sector_ranks": {},
        "t1_klines": {},
        "am_quote": {},
        "fundflow": {},
    }
    # 上午快照(Q1/Q2 AmPmRatio 用)
    am_snapshot = _load_snapshot(date, "1030") or _load_snapshot(date, "1100")
    if am_snapshot:
        extras["am_quote"]["1030"] = _quotes_from_snapshot(am_snapshot)

    # T-1 主档 kline(仅按 candidate_codes 或全 quotes.keys() 读,避免全 A 加载)
    codes_to_load = candidate_codes or list(quotes.keys())
    extras["t1_klines"] = _load_klines(codes_to_load, date)

    # Q3 需要:对候选池拉分时资金流(≤ MAX_BATCH_CODES)
    if need_fundflow and candidate_codes:
        extras["fundflow"] = _fetch_fundflow_for(candidate_codes, as_of)

    return extras


def _load_klines(codes: list[str], date: str) -> dict:
    """从 data/master/kline/<code>.parquet 读 T-1 及以前近 80 行日线(足够算 MA60)。

    - 缺 parquet → 该 code 跳过(下游 ma_stacked_bullish 会 False)
    - 大批量时惰性读单票,不做整体拼接
    """
    from tools.store import repo as store
    out: dict = {}
    for code in codes:
        try:
            df = store.get_master_kline(code)
        except Exception as e:
            logger.debug("kline 缺 %s: %s", code, e)
            continue
        if df is None or df.empty or "date" not in df.columns:
            continue
        # 只保留 T-1 及以前(防未来函数)
        df = df[df["date"].astype(str) < date].tail(80)
        if not df.empty:
            out[code] = df.reset_index(drop=True)
    return out


def _fetch_fundflow_for(codes: list[str], as_of: str) -> dict:
    """对候选池拉分时资金流(1min),失败者不入结果。

    粒度 1min:东财 2026-09-16 起下线 5min(rc=102),详见 fundflow_intraday docstring。
    """
    from tools.collectors import fundflow_intraday as FI
    # 硬拦候选池上限
    codes = codes[:FI.MAX_BATCH_CODES]
    if not codes:
        return {}
    try:
        got, errors = FI.collect(codes, freq=FI.FREQ_1MIN, as_of=as_of)
        if errors:
            logger.warning("fundflow_intraday 失败 %d 只(不阻断整批)", len(errors))
        return got
    except Exception as e:
        logger.error("fundflow_intraday 整批失败(降级 Q3 无数据):%s", e)
        return {}


# ────────────────────────────── 候选池初筛(减少 Q3 拉数量) ──────────────────────────────

def _pre_screen_candidates(quotes: dict[str, dict], top: int = 50) -> list[str]:
    """从全 quotes 按'当日振幅或成交额'挑前 top 只作 fundflow_intraday 拉取候选。

    简单规则:振幅×成交额 排前(高活跃 = Q3 主力最可能介入的票);缺任一字段 → 排后。
    """
    def score(q: dict) -> float:
        amp = q.get("amplitude") or 0.0
        amt = q.get("amount_wan") or 0.0
        try:
            return float(amp) * float(amt)
        except (TypeError, ValueError):
            return 0.0
    ranked = sorted(quotes.items(), key=lambda kv: score(kv[1]), reverse=True)
    return [c for c, _ in ranked[:top]]


# ────────────────────────────── run() ──────────────────────────────

def run(stage: Stage, *, date: str | None = None, force: bool = False,
        skip_fundflow: bool = False, full_universe: bool = False) -> int:
    """跑一段选股。

    参数:
        skip_fundflow:  True 时不触发 Q3 分时资金流采集(本地联调用,避免触网)
        full_universe:  True 时用主池 413 只;默认 False → focus 126 只(生产模式)
    """
    if stage not in _STAGES:
        logger.error("非法 stage %r", stage)
        return 1

    date = date or datetime.now().strftime("%Y-%m-%d")

    if not cal.is_trading_day(date):
        logger.info("跳过:%s 非 A 股交易日(stage=%s)", date, stage)
        return 0

    key = _STAGE_KEY[stage]
    screen = _load_json_file(_screen_path(date)) or {"date": date}
    if key in screen and not force:
        logger.info("跳过:screen.%s 已存在,不覆盖", key)
        return 0

    # 读 gate
    gate = _load_json_file(G.gate_path(date))
    if not gate:
        logger.error("上游 gate 缺失:%s", G.gate_path(date))
        return 1

    if stage == "final":
        s1 = screen.get("stage1_1430")
        s2 = screen.get("stage2_1450")
        gate_final = gate.get("final")
        if not gate_final or not s2:
            logger.error("final 阶段缺前置:gate.final=%s screen.stage2_1450=%s",
                         bool(gate_final), bool(s2))
            return 1
        # gate.final 空仓 → 强制清空 final(即使 stage2 有清单)
        if not gate_final.get("allowed_strategies"):
            final = {
                "gate_state": gate_final.get("state"),
                "position_pct": float(gate_final.get("position_pct", 0.0)),
                "selections": {}, "final_codes": [],
                "note": gate_final.get("note", "空仓"),
            }
        else:
            final = {**s2}
            final["note"] = "取 stage2 为 final"
            # 单点命中的降级标记必须透传到 final —— 否则只过了 1450 单点的票
            # 在 final 里长得跟"经两次确认"的票一模一样,下游无从分辨。
            if s2.get("confirm_degraded"):
                final["confirm_degraded"] = True
                final["confirm_degraded_reason"] = s2.get("confirm_degraded_reason", "")
                final["note"] = (
                    "⚠️ 取 stage2 为 final,但该段为单点命中(未经 1430 首判确认):"
                    f"{s2.get('confirm_degraded_reason', '')}"
                )
        screen["final"] = final
        screen["flipped"] = bool(gate.get("flipped", False))
        _write_atomic(_screen_path(date), screen)
        logger.info("落 screen.final:codes=%d flipped=%s",
                    len(final["final_codes"]), screen["flipped"])
        return 0

    # 1430 / 1450:先看 gate 该段允不允许出手
    gate_stage = gate.get(_GATE_STAGE_KEY[stage])
    if not gate_stage:
        logger.error("上游 gate.%s 缺失", _GATE_STAGE_KEY[stage])
        return 1

    allowed = list(gate_stage.get("allowed_strategies") or [])
    if not allowed:
        # 该段不允许出手 → 空清单直接落
        screen[key] = {
            "gate_state": gate_stage.get("state"),
            "position_pct": float(gate_stage.get("position_pct", 0.0)),
            "selections": {}, "final_codes": [],
            "note": f"gate.{stage} 不允许出手",
        }
        _write_atomic(_screen_path(date), screen)
        logger.info("落 screen.%s:gate.%s state=%s → 空", key, stage, gate_stage.get("state"))
        return 0

    # 读快照,拿报价
    snap = _load_snapshot(date, stage)
    if not snap:
        logger.error("上游 snapshot 缺失:T%s.json", stage)
        return 1
    quotes_raw = _quotes_from_snapshot(snap)
    if not quotes_raw:
        logger.error("snapshot codes 空 → 无候选")
        screen[key] = {"selections": {}, "final_codes": [], "note": "snapshot codes 空"}
        _write_atomic(_screen_path(date), screen)
        return 1

    # 用午盘 Q 票池收窄范围(默认 focus 126;--full-universe 切主池 413)
    try:
        universe_codes = set(UNIV.get_full_codes() if full_universe else UNIV.get_focus_codes())
    except FileNotFoundError as e:
        logger.error("午盘 Q 票池文件缺失,无法收窄范围:%s", e)
        return 1
    quotes = {c: q for c, q in quotes_raw.items() if c in universe_codes}
    coverage = (len(quotes) / len(universe_codes)) if universe_codes else 0.0
    data_insufficient = coverage < MIN_UNIVERSE_COVERAGE
    logger.info("票池过滤:snapshot %d 只 ∩ midday_q_universe(%s) %d 只 → 候选 %d 只(覆盖率 %.1f%%)",
                len(quotes_raw),
                "full" if full_universe else "focus",
                len(universe_codes), len(quotes), coverage * 100)
    if data_insufficient:
        logger.warning(
            "⚠️ 票池覆盖率 %.1f%% < 门限 %.0f%% —— 本段未看全票池,"
            "其'无入选'结论不可作为复核依据(标 data_insufficient)。"
            "常见成因:intraday_snapshot 未传 --codes(默认只跟踪已知标的,非扫焦点池)",
            coverage * 100, MIN_UNIVERSE_COVERAGE * 100)
    if not quotes:
        screen[key] = {"selections": {}, "final_codes": [],
                        "universe_coverage": round(coverage, 4),
                        "quotes_n": 0, "universe_n": len(universe_codes),
                        "data_insufficient": True,
                        "note": f"票池过滤后无候选(snapshot∩universe=空)"}
        _write_atomic(_screen_path(date), screen)
        return 0

    # confirm_from:1450 阶段从 stage1_1430 的入选里取(方案:两次都命中才买)
    #
    # ⚠️ 只在首判**数据充分**时才继承其结论。若首判 data_insufficient(没看全票池),
    # 它的"空"是无效结论而非"今天无信号",继承它会让 1450 候选恒为空集
    # (2026-09-16 实测:首判覆盖率 4.8% → 全天 0 只,即便 1450 已补到 126/126)。
    # 此时退回全票池重筛,并标 confirm_degraded —— **不冒充双点确认**,
    # 让下游/人知道这批票只过了单点(1450),没拿到方案要求的两次命中。
    confirm_from: dict[str, list[str]] | None = None
    confirm_degraded = False
    degraded_reason = ""
    if stage == "1450":
        s1 = screen.get("stage1_1430")
        if not s1:
            confirm_degraded = True
            degraded_reason = "首判段缺失(stage1_1430 未产出)"
        elif s1.get("data_insufficient"):
            confirm_degraded = True
            degraded_reason = (
                f"首判数据不足(覆盖率 {float(s1.get('universe_coverage') or 0) * 100:.1f}%"
                f" < 门限 {MIN_UNIVERSE_COVERAGE * 100:.0f}%),其结论不可继承"
            )
        elif not s1.get("selections"):
            # 首判数据充分但 selections 缺失/为空 dict(如 gate 当时不允许出手)
            confirm_degraded = True
            degraded_reason = f"首判无 selections 段({s1.get('note') or '原因未记录'})"
        else:
            # 首判数据充分且有 selections → 正常继承(空 list 也继承,那是真实"无信号")
            confirm_from = {
                k: [h["code"] for h in v] for k, v in s1["selections"].items()
            }
        if confirm_degraded:
            logger.warning("1450 复核退回全票池重筛(不继承首判):%s", degraded_reason)

    # 是否拉分时资金流(Q3 触发 + 首判阶段)
    need_ff = ("Q3" in allowed) and (stage == "1430") and not skip_fundflow
    candidate_codes = _pre_screen_candidates(quotes, top=50) if need_ff else None
    extras = _build_extras(date, _STAGE_AS_OF[stage], quotes, candidate_codes, need_ff)

    result = SC.dispatch(
        date, _STAGE_AS_OF[stage], quotes, gate_stage,
        top_n_per_strategy=5, confirm_from=confirm_from, extras=extras,
    )
    # 复核阶段 Q3 用首判的 fundflow 数据(如果 stage1 里落了)——M2 阶段简单实现:1450 不重拉,若首判候选池有 fundflow,复核可复用;若没有,复核 Q3 无数据
    screen[key] = {
        **result,
        "universe_coverage": round(coverage, 4),
        "quotes_n": len(quotes),
        "universe_n": len(universe_codes),
        "data_insufficient": data_insufficient,
    }
    if confirm_degraded:
        # 单点命中,未达方案"两次都命中"要求 → 显式留痕,不静默冒充双点确认
        screen[key]["confirm_degraded"] = True
        screen[key]["confirm_degraded_reason"] = degraded_reason
        screen[key]["note"] = (
            f"⚠️ 单点命中(仅 1450,未经首判确认):{degraded_reason}"
            + (f";{result['note']}" if result.get("note") else "")
        )
    screen["meta"] = {
        **(screen.get("meta") or {}),
        "script": "tools.pipeline.midday_q_screen",
        "script_version": SCRIPT_VERSION,
    }
    _write_atomic(_screen_path(date), screen)
    logger.info("落 screen.%s:strategies=%s codes=%d",
                key, list(result["selections"].keys()), len(result["final_codes"]))
    return 0


# ────────────────────────────── CLI ──────────────────────────────

def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="午盘 Q 选股节点(Q1/Q2/Q3 派单器)")
    ap.add_argument("--stage", required=True, choices=_STAGES)
    ap.add_argument("--date", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--skip-fundflow", action="store_true",
                    help="不触发 Q3 分时资金流采集(联调/测试用)")
    ap.add_argument("--full-universe", action="store_true",
                    help="用主池 413 只(默认 focus 126 只)")
    args = ap.parse_args(argv)

    _setup_logging()
    return run(args.stage, date=args.date, force=args.force,
                skip_fundflow=args.skip_fundflow,
                full_universe=args.full_universe)


if __name__ == "__main__":
    raise SystemExit(main())
