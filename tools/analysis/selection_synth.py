"""S5 · 选股合成程序化 —— 把「利好板块 × 消息催化 × 策略合议 × 财报 × 形态 × 盘面」
合成为《今日选股》,用 **DeepSeek 程序化产出**(取代此前统筹交互式的合成)。

设计见 docs/计划/2026-09-16_消息板块选股_程序化流水线_设计.md(S5 段)+
docs/计划/2026-09-16_选股侧消费板块利好标签_接口设计.md(进池口径/防偷看)。
⚠️ 测试环境研究模拟,非投资建议。KPI = 绝对收益
(Model A:D 选 → D+1 回踩限价入场 → D+1 收盘绝对为正 → D+2 卖出)。

流程(每步明确输入/输出,程序驱动、非 Claude 交互):
  1. 读输入  = sector_focus「消息驱动」块(利好板块+龙头/跟涨候选,板块消息面评价)
              + 角色关系表(role_codes)拿**中军**(板块大资金主力,消息块未含)。
  2. 策略面  = tools.pipeline.screen_council(--codes 候选)拿综合分/方向/财报红旗/龙虎榜否决。
  3. 形态面  = screen_forward_common.load_klines + 本模块 pattern_metrics
              (当日/5/10/20 日涨跌、均线多头、距 20/60 高、量比、获利盘代理、涨停不可买)。
  4. 盘面    = sector_focus 顶层 regime(风险偏好/宏观净方向/宏观情景)。
  5. 合成    = **DeepSeek 分析师**(SELECTION_SYNTH_INSTRUCTION,rubric 文字优先)逐板块
              判每票 档(推荐/观察/剔除)+建议分(0-10)+理由+入场(回踩限价)/止损+风险
              + 板块级规避提示。
  6. 落产物  = data/analysis/<date>/今日选股_<date>.md(同手工版结构)+ .json(结构化)。

防未来/防偷看(硬):as-of(只用 ≤date 数据);入场=回踩限价(不追高开);判据固定、
**绝不对既往赢家调参**;只用给定数据、禁编造、禁用未来信息。

复用不重造:screen_council(--codes)、screen_forward_common.load_klines、
news_catalyst.role_codes(中军)、chip.summarize(获利盘)、board_verdict 的 client.extract 范式、
dataroot.ensure_data_root(worktree 数据根)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("analysis.selection_synth")

SYNTH_VERSION = "s5-2026-09-16"

# 采集角色:龙头/跟涨取自消息驱动块(消息催化候选);中军取自角色关系表(板块大资金主力)。
ROLE_LEADER = "龙头"
ROLE_CORE = "中军"
ROLE_FOLLOW = "跟涨"

# 涨停线(与 nextday_kernel/Doc3 §7.2 同口径):当日收盘≈涨停 → 次日无法回踩限价买入。
LIMIT_MAIN = 0.10        # 主板 60/00/001/002/003
LIMIT_GEM = 0.20         # 创业板 300 / 科创板 688
LIMIT_TOL = 0.005        # 当日涨幅 ≥ limit-0.005 判涨停不可买

MIN_BARS = 60            # 形态/均线所需最少 K 线


# ════════════════════ 输入读取(sector_focus 消息驱动 + regime) ════════════════════
def _sector_focus_path(date: str, data_root: Optional[Path] = None) -> Path:
    from tools.analysis.market_forecast import dataroot
    root = data_root or dataroot.ensure_data_root()
    return dataroot.analysis_dir(root) / date / "sector_focus.json"


def load_sector_focus(date: str, *, data_root: Optional[Path] = None) -> dict:
    """读当日 sector_focus.json(消息驱动块 + 顶层 regime)。缺失抛 FileNotFoundError。"""
    p = _sector_focus_path(date, data_root)
    if not p.exists():
        raise FileNotFoundError(f"sector_focus 缺失:{p}")
    return json.loads(p.read_text(encoding="utf-8"))


def load_regime(focus: dict) -> dict:
    """从 sector_focus 顶层抽盘面 regime(风险偏好/宏观净方向/宏观情景 + 依据)。"""
    rp = focus.get("风险偏好") or {}
    return {
        "风险偏好": rp.get("风险偏好") or focus.get("风险偏好"),
        "宏观净方向": focus.get("宏观净方向") or rp.get("宏观净方向"),
        "宏观情景": focus.get("宏观情景") or rp.get("宏观情景"),
        "广度档": rp.get("广度档"),
        "hs300方向": rp.get("hs300方向"),
        "净广度": rp.get("净广度"),
        "涨停": rp.get("涨停"),
        "跌停": rp.get("跌停"),
        "依据": rp.get("依据"),
    }


def load_message_boards(focus: dict, *, as_of: Optional[str] = None) -> list[dict]:
    """取消息驱动块的利好板块列表(带 as-of 防偷看守卫)。"""
    md = focus.get("消息驱动") or {}
    blk_as_of = str(md.get("as_of") or "")
    if as_of and blk_as_of and blk_as_of > as_of:
        logger.warning("消息驱动 as_of=%s 晚于决策时刻 %s,防偷看忽略", blk_as_of, as_of)
        return []
    return list(md.get("利好板块") or [])


def board_core_codes(date: str, board: str) -> list[dict]:
    """从角色关系表取该板块**中军**(消息驱动块只含龙头/跟涨,中军=板块大资金主力另取)。"""
    from tools.analysis.sector_forecast.news_catalyst import role_codes
    rc = role_codes(date, boards=[board], roles=(ROLE_CORE,))
    return [{"code": d["code"], "name": d.get("name", ""), "role": ROLE_CORE}
            for d in rc.get(board, [])]


def gather_candidates(date: str, board_block: dict) -> list[dict]:
    """把单个利好板块的候选拍平成带角色标签的列表:龙头(消息块)+中军(角色表)+跟涨(消息块)。

    去重按 code(同一票只保留首个角色,优先级 龙头>中军>跟涨)。带消息块的个股元数据(已动/联动依据)。
    """
    board = board_block.get("board") or board_block.get("板块") or ""
    out: list[dict] = []
    seen: set[str] = set()

    def _add(code: str, name: str, role: str, extra: dict) -> None:
        if not code or code in seen:
            return
        seen.add(code)
        out.append({"code": code, "name": name, "role": role, "board": board, **extra})

    for d in board_block.get("龙头候选") or []:
        _add(d.get("code"), d.get("name", ""), ROLE_LEADER, {"已动": d.get("已动")})
    for d in board_core_codes(date, board):
        _add(d["code"], d["name"], ROLE_CORE, {})
    for d in board_block.get("跟涨候选") or []:
        _add(d.get("code"), d.get("name", ""), ROLE_FOLLOW,
             {"联动依据": d.get("联动依据")})
    return out


# ════════════════════ 形态面(load_klines + pattern_metrics) ════════════════════
def limit_pct(code: str) -> float:
    return LIMIT_GEM if code.startswith("300") or code.startswith("688") else LIMIT_MAIN


def _asof_slice(df: pd.DataFrame, date: str) -> pd.DataFrame:
    """防未来:只保留 date 当日及之前的 K 线。"""
    d = pd.Timestamp(date)
    return df[pd.to_datetime(df["date"]) <= d].reset_index(drop=True)


def pattern_metrics(df: pd.DataFrame, code: str, *, date: str) -> dict:
    """单票形态指标(as-of date)。K 线不足 → {数据不足:True}。

    字段:现价/ma5/ma20/当日涨跌%/ret5/ret10/ret20/均线多头/距20高%/距60高%/量比/
         获利盘(chip 获利比例·可 None)/位置pos60(距高代理)/涨停不可买/ATR%。
    """
    df = _asof_slice(df, date)
    if df is None or len(df) < MIN_BARS:
        return {"数据不足": True, "n": 0 if df is None else len(df)}
    c = df["close"].to_numpy(float)
    h = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    close = float(c[-1])
    ma5 = float(np.mean(c[-5:]))
    ma10 = float(np.mean(c[-10:]))
    ma20 = float(np.mean(c[-20:]))
    ma60 = float(np.mean(c[-60:]))

    def _ret(n: int) -> Optional[float]:
        return round(close / c[-1 - n] - 1.0, 4) if len(c) > n else None

    hi20, hi60 = float(np.max(h[-20:])), float(np.max(h[-60:]))
    lo60 = float(np.min(lo[-60:]))
    vol = df["volume"].to_numpy(float)
    vr = float(vol[-1] / np.mean(vol[-6:-1])) if len(vol) >= 6 and np.mean(vol[-6:-1]) else None
    # ATR14%
    prev_c = c[:-1]
    tr = np.maximum.reduce([
        h[1:] - lo[1:], np.abs(h[1:] - prev_c), np.abs(lo[1:] - prev_c)])
    atr = float(np.mean(tr[-14:])) if len(tr) >= 14 else None
    # 获利盘(chip 推演;主档无换手 → None,交 LLM 用 pos60 代理)
    win_rate = None
    try:
        from tools.collectors import chip
        summ = chip.summarize(df)
        win_rate = summ.get("获利比例")
    except Exception:                                     # noqa: BLE001
        win_rate = None
    pos60 = round((close - lo60) / (hi60 - lo60), 4) if hi60 > lo60 else None
    return {
        "现价": round(close, 3),
        "ma5": round(ma5, 3), "ma20": round(ma20, 3), "ma60": round(ma60, 3),
        "当日涨跌": round(close / float(c[-2]) - 1.0, 4) if len(c) >= 2 else None,
        "ret5": _ret(5), "ret10": _ret(10), "ret20": _ret(20),
        "均线多头": bool(ma5 > ma10 > ma20 and close >= ma5),
        "距20高": round(close / hi20 - 1.0, 4) if hi20 else None,
        "距60高": round(close / hi60 - 1.0, 4) if hi60 else None,
        "量比": round(vr, 2) if vr is not None else None,
        "获利盘": round(win_rate, 4) if win_rate is not None else None,
        "位置pos60": pos60,
        "涨停不可买": bool(len(c) >= 2 and (close / float(c[-2]) - 1.0) >= limit_pct(code) - LIMIT_TOL),
        "ATR%": round(atr / close * 100, 2) if atr and close else None,
    }


# ════════════════════ 策略面(screen_council --codes) ════════════════════
def run_council(codes: list[str], as_of: str) -> dict[str, dict]:
    """对候选 codes 跑策略0多专家合议 → {code: {综合分,综合方向,财报红旗数,龙虎榜否决,财报风险}}。

    persist=False(不覆盖闭环已落 view)。历史不足/无信号的票不入 top → map 无该 code(下游标数据不足)。
    """
    from tools.pipeline.screen_council import run_council_screen
    view = run_council_screen(list(dict.fromkeys(codes)), as_of=as_of,
                              fetch=False, top_n=len(codes) + 5, persist=False)
    out: dict[str, dict] = {}
    for row in view.get("top") or []:
        fr = row.get("财报风险") or {}
        lhb = ((fr.get("各轴") or {}).get("龙虎榜") or {})
        out[row["code"]] = {
            "综合分": row.get("综合分"),
            "综合方向": row.get("综合方向"),
            "财报红旗数": int(fr.get("高危数") or 0),
            "龙虎榜否决": bool(lhb.get("应用")),
            "财报风险": fr or None,
        }
    return out


# ════════════════════ DeepSeek 分析师合成(提示词 + schema) ════════════════════
# 每票输出档:文字优先(仿 board_verdict rubric),建议分 0-10 供排序。
SELECTION_SCHEMA = {
    "个股": ("list,每票一个 dict:{code, name, 档:推荐|观察|剔除, 建议分:0-10 数值, "
            "理由:一句话≤40字, 入场:回踩限价文字(如『~ma5 125.9 回踩限价』), "
            "止损:文字(如『ma20 121.5』), 风险:一句话}"),
    "规避提示": "一句话:本板块级需规避/降级的情形(消息面转弱/资金流出/高位拥挤等),无则写『无』",
}


def SELECTION_SYNTH_INSTRUCTION(sw: str, regime: dict) -> str:
    """选股分析师合成提示词(rubric 文字优先·硬纪律写死)。sw=利好板块名,regime=盘面 dict。"""
    reg = (f"风险偏好={regime.get('风险偏好')}·宏观净方向={regime.get('宏观净方向')}"
           f"·宏观情景={regime.get('宏观情景')}·广度档={regime.get('广度档')}")
    return (
        f"你是**选股分析师**。给你「{sw}」板块(消息面利好)内候选票的**三维数据**"
        f"(消息催化 × 策略council × 形态)+ 当前盘面 regime,请按**整体选股策略**逐票判"
        "选/不选,并给建议分(0-10)。\n\n"
        f"【盘面 regime】{reg}\n\n"
        "【每票给你的数据】code/name/角色(龙头/中军/跟涨) + 消息催化(板块消息面评价+个股是否已动) + "
        "策略council(综合分/综合方向/财报红旗数/龙虎榜否决) + 形态(距20高/距60高/获利盘/位置pos60/量比/"
        "均线多头/当日涨跌/涨停不可买/ATR%)。\n\n"
        "【硬纪律(务必遵守)】\n"
        "① **催化优先 × 不追高**:在强利好板块内选**催化驱动的强势票**,一律**回踩限价入场**"
        "(如 ~ma5 回踩),**不追涨停/不追高开**;入场价写回踩支撑(ma5/ma20)文字。\n"
        "② **龙头/中军为主线,跟涨(补涨先锋)只作『联动观察』不进主选**:跟涨角色最高只给『观察』档。\n"
        "③ **反选剔除(命中任一即『剔除』档、建议分≤2)**:财报高危红旗(财报红旗数≥1)/龙虎榜否决/"
        "涨停不可买/极高位高抛压(获利盘≥0.95 或 位置pos60≥0.95 且距高接近0)。\n"
        "④ **普跌/中性 regime 不因超买系统性回避动量**:有硬催化的强势票照常可入选,不因中性盘面一刀切避。\n"
        "⑤ **只用给定数据,禁止编造,禁止使用给定日期之后的未来信息**;建议分越高越看好,"
        "推荐档一般≥6.5、观察档 3~6.5、剔除档≤2。\n\n"
        "【建议分构成(供你心算,不必输出公式)】消息强弱(强/中/弱) + 策略方向(看多/中性/看空) + "
        "财报(干净/红旗) + 形态(回踩健康/涨停不可买/极高位)。综合成 0-10。\n"
        "输出严格 JSON,个股顺序同输入。"
    )


def _stock_llm_payload(cand: dict, council: dict, form: dict, board_ctx: dict) -> dict:
    """喂给 LLM 的单票精简数据(只给判据、不给结论)。"""
    return {
        "code": cand["code"], "name": cand.get("name", ""), "角色": cand.get("role"),
        "消息催化": {
            "板块消息面": board_ctx.get("tag"), "板块强弱": board_ctx.get("强弱"),
            "板块持续性": board_ctx.get("持续性"), "个股已动": cand.get("已动"),
            "联动依据": cand.get("联动依据"),
        },
        "策略council": {
            "综合分": council.get("综合分"), "综合方向": council.get("综合方向"),
            "财报红旗数": council.get("财报红旗数"), "龙虎榜否决": council.get("龙虎榜否决"),
        } if council else {"数据不足": True},
        "形态": form if not form.get("数据不足") else {"数据不足": True},
    }


# 极高位高抛压阈值(反选④):获利盘或位置分位 ≥ 此值且贴近高点。
_极高位_获利 = 0.95
_极高位_pos = 0.95
_贴高_距 = -0.02        # 距 20/60 高 ≥ -2%(几乎在高点)


def hard_veto_reason(role: str, council: Optional[dict], form: Optional[dict]) -> Optional[str]:
    """反选纪律硬闸(程序保证·不依赖 LLM):命中即必须『剔除』。判据固定、绝不对既往赢家调参。

    命中任一:①财报高危红旗≥1 ②龙虎榜否决 ③涨停不可买 ④极高位高抛压(获利盘/位置≥0.95 且贴高)。
    返回命中原因(用于理由留痕);未命中 → None。
    """
    council = council or {}
    form = form or {}
    if int(council.get("财报红旗数") or 0) >= 1:
        return f"财报高危红旗×{council['财报红旗数']}"
    if council.get("龙虎榜否决"):
        return "龙虎榜净买否决"
    if form.get("涨停不可买"):
        return "涨停不可买(无法回踩限价入场)"
    wr, pos = form.get("获利盘"), form.get("位置pos60")
    d20, d60 = form.get("距20高"), form.get("距60高")
    贴高 = (isinstance(d20, (int, float)) and d20 >= _贴高_距) or \
           (isinstance(d60, (int, float)) and d60 >= _贴高_距)
    极高 = (isinstance(wr, (int, float)) and wr >= _极高位_获利) or \
           (isinstance(pos, (int, float)) and pos >= _极高位_pos)
    if 极高 and 贴高:
        return "极高位高抛压(获利盘/位置≥0.95 且贴近高点)"
    return None


def apply_hard_discipline(stock: dict, role: str, council: Optional[dict],
                          form: Optional[dict]) -> dict:
    """LLM 合成后的硬纪律回扣(defense-in-depth):

    · 反选命中 → 强制 档=剔除、建议分≤2(不管 LLM 给了什么)。
    · 跟涨角色 → 最高只到『观察』(龙头/中军为主线,跟涨只联动观察)。
    语义锁在 tests,防未来 prompt/代码重写无意删规则。
    """
    reason = hard_veto_reason(role, council, form)
    if reason:
        stock["档"] = "剔除"
        sc = stock.get("建议分")
        stock["建议分"] = min(sc, 2.0) if isinstance(sc, (int, float)) else 1.0
        base = stock.get("理由") or ""
        if reason not in base:
            stock["理由"] = f"[反选:{reason}]" + (f" {base}" if base else "")
        stock["硬纪律命中"] = reason
    elif role == ROLE_FOLLOW and stock.get("档") == "推荐":
        stock["档"] = "观察"                              # 跟涨不进主选,降为联动观察
        stock["理由"] = "[跟涨联动观察·不进主选] " + (stock.get("理由") or "")
    return stock


def synthesize_board(sw: str, board_ctx: dict, candidates: list[dict],
                     council_map: dict, form_map: dict, regime: dict, *,
                     client=None) -> dict:
    """单板块合成:组三维 payload → DeepSeek client.extract(原生绕缓存)→ 每票档/分/理由 + 规避提示。

    LLM 失败 → 降级(每票档=观察·建议分=None·标 LLM失败),不崩;上游据此诚实标注。
    """
    from tools.llm import client as lc
    client = client or lc.get_client()
    payloads = [
        _stock_llm_payload(c, council_map.get(c["code"], {}), form_map.get(c["code"], {}),
                           board_ctx)
        for c in candidates
    ]
    text = json.dumps({"板块": sw, "候选": payloads}, ensure_ascii=False)
    try:
        r = client.extract(text, SELECTION_SCHEMA,
                           instruction=SELECTION_SYNTH_INSTRUCTION(sw, regime))
    except Exception as e:                                # noqa: BLE001
        logger.warning("板块 %s 合成 LLM 失败:%s", sw, e)
        r = {"个股": [{"code": c["code"], "name": c.get("name", ""), "档": "观察",
                      "建议分": None, "理由": "LLM合成失败降级", "入场": None,
                      "止损": None, "风险": "研判缺失,需人工"} for c in candidates],
             "规避提示": f"LLM失败:{str(e)[:40]}"}
    # 回挂原始三维数据(留痕、人工可核)
    by_code = {s.get("code"): s for s in (r.get("个股") or [])}
    merged = []
    for c in candidates:
        s = by_code.get(c["code"], {"code": c["code"], "name": c.get("name", ""),
                                     "档": "观察", "建议分": None, "理由": "LLM未返回"})
        s["角色"] = c.get("role")
        s["council"] = council_map.get(c["code"])
        s["形态"] = form_map.get(c["code"])
        apply_hard_discipline(s, c.get("role"), s["council"], s["形态"])
        merged.append(s)
    merged.sort(key=lambda s: (s.get("建议分") if isinstance(s.get("建议分"), (int, float))
                               else -1), reverse=True)
    return {"board": sw, "板块消息面": board_ctx, "个股": merged,
            "规避提示": r.get("规避提示")}


# ════════════════════ 编排 + 产物落盘 ════════════════════
def synthesize(date: str, *, data_root: Optional[Path] = None, client=None,
               boards: Optional[list[str]] = None) -> dict:
    """S5 主编排:读输入 → 逐板块(council+形态+DeepSeek 合成)→ 结构化结果 dict。"""
    from tools.analysis.market_forecast import dataroot
    root = data_root or dataroot.ensure_data_root()
    focus = load_sector_focus(date, data_root=root)
    regime = load_regime(focus)
    good_boards = load_message_boards(focus, as_of=date)
    if boards:
        good_boards = [b for b in good_boards
                       if (b.get("board") or b.get("板块")) in boards]

    board_results = []
    for blk in good_boards:
        sw = blk.get("board") or blk.get("板块") or ""
        cands = gather_candidates(date, blk)
        if not cands:
            continue
        codes = [c["code"] for c in cands]
        council_map = run_council(codes, as_of=date)
        # 形态:load_klines(复用)+ as-of pattern_metrics
        from tools.backtest.screen_forward_common import load_klines
        klines = load_klines(codes, min_bars=MIN_BARS)
        form_map = {code: pattern_metrics(df, code, date=date)
                    for code, df in klines.items()}
        for code in codes:                                # 无 K 线的票也留占位
            form_map.setdefault(code, {"数据不足": True})
        board_ctx = {
            "tag": blk.get("tag"), "强弱": blk.get("强弱"),
            "关键事件": blk.get("关键事件"), "持续性": blk.get("持续性"),
            "时效": blk.get("时效"), "可靠性综述": blk.get("可靠性综述"),
            "依据": blk.get("依据"),
        }
        board_results.append(
            synthesize_board(sw, board_ctx, cands, council_map, form_map, regime,
                             client=client))

    return {
        "date": date, "version": SYNTH_VERSION,
        "as_of": (focus.get("消息驱动") or {}).get("as_of") or date,
        "regime": regime,
        "板块": board_results,
        "口径": ("S5 程序化合成:消息驱动块(龙头/跟涨)+角色表(中军)→ screen_council 策略面 + "
                "形态(as-of)→ DeepSeek 分析师(SELECTION_SYNTH_INSTRUCTION,rubric 文字优先)"
                "逐票 档/建议分/回踩限价入场 + 板块规避。"),
        "防未来": "as-of(只用≤date K线+披露≤date财报+龙虎榜list_date<date);回踩限价入场不追高开;判据固定不对既往赢家调参。",
        "免责": "⚠️ 测试环境研究模拟,非投资建议。",
    }


# ---- 渲染 ----
_档_ORDER = {"推荐": 0, "观察": 1, "剔除": 2}


def _fmt_pct(v) -> str:
    return f"{v * 100:+.1f}%" if isinstance(v, (int, float)) else "-"


def _fmt_form(form: Optional[dict]) -> str:
    if not form or form.get("数据不足"):
        return "数据不足"
    parts = [f"距20高{_fmt_pct(form.get('距20高'))}"]
    wr = form.get("获利盘")
    if wr is not None:
        parts.append(f"获利{wr * 100:.0f}%")
    elif form.get("位置pos60") is not None:
        parts.append(f"pos60={form['位置pos60']:.2f}")
    if form.get("量比") is not None:
        parts.append(f"量比{form['量比']}")
    if form.get("均线多头"):
        parts.append("多头")
    if form.get("涨停不可买"):
        parts.append("涨停不可买")
    return "/".join(parts)


def render_md(result: dict) -> str:
    date = result["date"]
    reg = result.get("regime") or {}
    L: list[str] = []
    L.append(f"# 今日选股 · {date}（消息面 × 策略面 × 财报 三维合成 · 程序化 S5）\n")
    L.append("> ⚠️ 测试环境研究模拟，非投资建议。KPI = 绝对收益"
             "（Model A：D 选 → D+1 回踩限价入场 → D+1 收盘绝对为正 → D+2 卖出线）。")
    L.append(f"> 产出方式：程序化 `tools.analysis.selection_synth`（版本 {result.get('version')}）"
             "，DeepSeek 分析师合成，非 Claude 交互。")
    L.append(f"> as_of={result.get('as_of')}；{result.get('防未来')}\n")

    L.append("## 一、整体盘面（regime）\n")
    L.append(f"- **风险偏好**：{reg.get('风险偏好')}（广度档 {reg.get('广度档')}、"
             f"净广度 {reg.get('净广度')}、涨停 {reg.get('涨停')}/跌停 {reg.get('跌停')}、"
             f"hs300 {reg.get('hs300方向')}）。")
    L.append(f"- **宏观**：净方向 {reg.get('宏观净方向')}、情景 {reg.get('宏观情景')}。")
    L.append(f"- **口径**：{result.get('口径')}\n")

    L.append("## 二、板块消息面评价\n")
    L.append("| 板块 | 评价 | 强弱 | 持续性 | 规避提示 |")
    L.append("|---|---|---|---|---|")
    for b in result.get("板块") or []:
        ctx = b.get("板块消息面") or {}
        L.append(f"| **{b['board']}** | {ctx.get('tag')} | {ctx.get('强弱')} | "
                 f"{(ctx.get('持续性') or '')[:40]} | {b.get('规避提示') or '-'} |")
    L.append("")

    L.append("## 三、选股（三维合成：消息面 × 策略council × 财报 × 形态）\n")
    L.append("| 板块 | 角色 | 代码 | 名称 | 消息 | 策略分/方向 | 财报/龙虎 | 形态 | 建议分 | 档 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    picks = []
    for b in result.get("板块") or []:
        ctx = b.get("板块消息面") or {}
        for s in b.get("个股") or []:
            cc = s.get("council") or {}
            fr = f"红旗{cc.get('财报红旗数', 0)}" if cc else "-"
            if cc and cc.get("龙虎榜否决"):
                fr += "·龙虎否决"
            score = s.get("建议分")
            score_s = f"**{score}**" if isinstance(score, (int, float)) else "-"
            档 = s.get("档") or "-"
            档_s = f"**{档}**" if 档 in ("推荐", "剔除") else 档
            L.append(f"| {b['board']} | {s.get('角色')} | {s['code']} | {s.get('name')} | "
                     f"{ctx.get('强弱')} | {cc.get('综合分') if cc else '-'} "
                     f"{cc.get('综合方向') if cc else ''} | {fr} | {_fmt_form(s.get('形态'))} | "
                     f"{score_s} | {档_s} |")
            if 档 == "推荐":
                picks.append((b["board"], s))
    L.append("")

    L.append("## 四、主选深度（推荐 · 回踩限价入场）\n")
    if picks:
        L.append("| 板块 代码 名称 | 理由(催化) | 入场(回踩限价·不追高) | 止损 | 风险 |")
        L.append("|---|---|---|---|---|")
        for board, s in sorted(picks, key=lambda x: -(x[1].get("建议分") or 0)):
            L.append(f"| {board} {s['code']} {s.get('name')} | {s.get('理由') or '-'} | "
                     f"{s.get('入场') or '-'} | {s.get('止损') or '-'} | {s.get('风险') or '-'} |")
    else:
        L.append("_今日无『推荐』档（宁缺毋滥/降仓/只留有硬催化的）。_")
    L.append("")

    L.append("## 五、反选剔除（数据背书）\n")
    any_cut = False
    for b in result.get("板块") or []:
        for s in b.get("个股") or []:
            if s.get("档") == "剔除":
                any_cut = True
                L.append(f"- **{b['board']} {s['code']} {s.get('name')}**："
                         f"{s.get('理由') or ''}（风险：{s.get('风险') or '-'}）")
    if not any_cut:
        L.append("_无剔除。_")
    L.append("")

    L.append("## 六、规避策略（板块级）\n")
    for b in result.get("板块") or []:
        tip = b.get("规避提示")
        if tip and tip != "无":
            L.append(f"- **{b['board']}**：{tip}")
    L.append("- **无利好日兜底**：若无高确信利好板块，宁缺毋滥/降仓/只留有硬催化的，不硬凑。\n")

    L.append("## 七、龙头 / 中军 / 跟涨 —— 口径说明\n")
    L.append("- **龙头/跟涨** 取自 sector_focus「消息驱动」块（消息催化候选，跟涨=补涨先锋只作联动观察）；"
             "**中军** 取自角色关系表（板块大资金主力，`role_codes`）。")
    L.append("- **龙头/中军为主线，跟涨只联动观察**（不抢名额、优先级低于有自身催化的票）。")
    L.append(f"\n---\n*{result.get('免责')} 数据来源：sector_focus 消息驱动块 + 角色关系表 + "
             "screen_council 策略0合议 + as-of 形态。*")
    return "\n".join(L)


def run(date: str, *, data_root: Optional[Path] = None, client=None,
        boards: Optional[list[str]] = None, write: bool = True) -> dict:
    """S5 端到端:合成 → 落 今日选股_<date>.md + .json(data/analysis/<date>/)。返回结果 dict。"""
    from tools.analysis.market_forecast import dataroot
    root = data_root or dataroot.ensure_data_root()
    result = synthesize(date, data_root=root, client=client, boards=boards)
    if write:
        out_dir = dataroot.analysis_dir(root) / date
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / f"今日选股_{date}.md"
        json_path = out_dir / f"今日选股_{date}.json"
        md_path.write_text(render_md(result), encoding="utf-8")
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        result["_产物"] = {"md": str(md_path), "json": str(json_path)}
        logger.info("S5 选股合成落盘:%s + %s", md_path, json_path)
    return result
