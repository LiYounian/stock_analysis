"""S5 · 选股合成程序化(**双路并集**版)——策略线 ∪ 板块消息线,用 DeepSeek 程序化产出《今日选股》。

设计见 docs/计划/2026-09-16_消息板块选股_程序化流水线_设计.md(S5 段)+
docs/计划/2026-09-17_选股双路并集整改_设计.md(双路并集/来源标注/双依据提示词)。
⚠️ 测试环境研究模拟,非投资建议。KPI = 绝对收益
(Model A:D 选 → D+1 回踩限价入场 → D+1 收盘绝对为正 → D+2 卖出)。

━━ 病根整改:此前 S5 只做"消息面判利好板块→只在利好板块内选票",策略选出的强票若板块
   未被判利好就整条旁路,选股被板块绑架、过度极端。本版拉回平衡为**双路并集**:

  (a) 策略线(主体·全A·不被板块闸门):跑生产策略选股拿候选——至少
      tools.pipeline.screen_council(策略0·多专家合议·全A·含财报/龙虎)top-N;有余力再并
      screen_momentum/screen_strong/screen_trend_template 的 top 票。这些是"原策略直接选出
      的强票",**不管板块是否利好都保留**。
  (b) 板块消息线(催化/规避维):读 sector_focus「消息驱动」利好板块——用策略(council/形态)
      在利好板块内挑可买强票(催化加成);规避板块池内的票打**规避/降级**。板块消息**不再当
      唯一闸门**,而是"催化加权 + 规避"维。
  (c) 综合并集:最终候选 = 策略线票 ∪ 板块消息线票。**每票标来源** 来源∈{策略直选,板块催化,两者兼有}。

提示词(SELECTION_SYNTH_INSTRUCTION)**同时给"策略借鉴"和"板块借鉴"并说原因**:每票喂
①策略面(命中哪些策略/council综合分+方向/财报红旗/龙虎)②板块面(所属板块消息利好/利空/催化/强弱)
③形态;要求 LLM 输出 档/建议分/理由,**理由里分别点明"策略依据"与"板块依据"**。

防未来/防偷看(硬):量价/财报 as-of(只用 ≤date 数据);入场=回踩限价(不追高开);判据固定、
**绝不对既往赢家调参**;只用给定数据、禁编造、禁用未来信息。(新闻时效由 S3 allow_future 口径管,
live 选股走"最大可得新闻",不影响本模块量价/财报 as-of。)

复用不重造:screen_council(全A universe / --codes)、screen_momentum/strong/trend_template、
screen_forward_common.load_klines、news_catalyst.role_codes(中军)、chip.summarize(获利盘)、
board_verdict 的 client.extract 范式、dataroot.ensure_data_root(worktree 数据根)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("analysis.selection_synth")

SYNTH_VERSION = "s5-dualpath-2026-09-17"

# 采集角色:龙头/跟涨取自消息驱动块(消息催化候选);中军取自角色关系表(板块大资金主力)。
ROLE_LEADER = "龙头"
ROLE_CORE = "中军"
ROLE_FOLLOW = "跟涨"
ROLE_STRATEGY = "策略"          # 策略线选出、不隶属某利好板块的强票(全A council/动量等)

# 来源标注(双路并集):策略线直选 / 板块消息线催化 / 两路都命中。
SRC_STRATEGY = "策略直选"
SRC_BOARD = "板块催化"
SRC_BOTH = "两者兼有"

STRATEGY_TOP_N = 12            # 策略线(全A council)取 top-N 进并集
EXTRA_TOP_K = 8               # 附加策略(动量/强势/趋势模板)各取 top-K 合入"策略命中"

# 涨停线(与 nextday_kernel/Doc3 §7.2 同口径):当日收盘≈涨停 → 次日无法回踩限价买入。
LIMIT_MAIN = 0.10        # 主板 60/00/001/002/003
LIMIT_GEM = 0.20         # 创业板 300 / 科创板 688
LIMIT_TOL = 0.005        # 当日涨幅 ≥ limit-0.005 判涨停不可买

MIN_BARS = 60            # 形态/均线所需最少 K 线


# ════════════════════ 输入读取(sector_focus 消息驱动 + regime + 规避板块池) ════════════════════
def _sector_focus_path(date: str, data_root: Optional[Path] = None) -> Path:
    from tools.analysis.market_forecast import dataroot
    root = data_root or dataroot.ensure_data_root()
    return dataroot.analysis_dir(root) / date / "sector_focus.json"


def load_sector_focus(date: str, *, data_root: Optional[Path] = None) -> dict:
    """读当日 sector_focus.json(消息驱动块 + 顶层 regime + 规避板块池)。缺失抛 FileNotFoundError。"""
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


def load_avoid_boards(focus: dict) -> dict[str, str]:
    """取规避板块池 → {板块名: 规避理由}。板块消息线据此对隶属票打"规避/降级"(非闸门·仅降级维)。"""
    out: dict[str, str] = {}
    for r in (focus.get("规避板块池") or []):
        sw = r.get("板块") or r.get("board")
        if sw:
            out[sw] = r.get("规避理由") or r.get("理由") or "板块消息面转弱/拥挤过热"
    return out


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


# ---- 名称 / 行业→申万一级 ----
_NAMES: Optional[dict] = None


def _name_of(code: str) -> str:
    """从 config/code_name.json 取股票名(缺 → 空串,不阻断)。"""
    global _NAMES
    if _NAMES is None:
        try:
            from tools.config import settings
            _NAMES = json.loads((settings.PROJECT_ROOT / "config" / "code_name.json")
                                .read_text(encoding="utf-8"))
        except Exception:                                 # noqa: BLE001
            _NAMES = {}
    return _NAMES.get(code) or ""


def sw_of(code: str, industry: Optional[str]) -> Optional[str]:
    """个股 → 申万一级板块名(优先 council 给的行业名映射;失败 → None)。供规避板块池匹配。"""
    if not industry:
        return None
    try:
        from tools.analysis import industry_map
        return industry_map.to_sw(industry) or industry
    except Exception:                                     # noqa: BLE001
        return industry


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
    lo20 = float(np.min(lo[-20:]))            # 近20日最低价 → 回踩前低入场/止损锚点
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
        "ma10": round(ma10, 3),
        "前低": round(lo20, 3),
        "当日high": round(float(h[-1]), 3),      # 强势旁路突破锚:D 日最高价(D+1 突破即确认)
        "当日low": round(float(lo[-1]), 3),       # 旁路止损锚:跌破启动日低点即走
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


# ════════════════════ 策略面(screen_council --codes / 全A universe + 附加策略) ════════════════════
def _council_rows_to_map(rows: list[dict]) -> dict[str, dict]:
    """把 council view 的 top 行统一成 {code: {综合分,综合方向,财报红旗数,龙虎榜否决,财报风险,行业}}。"""
    out: dict[str, dict] = {}
    for row in rows or []:
        fr = row.get("财报风险") or {}
        lhb = ((fr.get("各轴") or {}).get("龙虎榜") or {})
        out[row["code"]] = {
            "综合分": row.get("综合分"),
            "综合方向": row.get("综合方向"),
            "财报红旗数": int(fr.get("高危数") or 0),
            "龙虎榜否决": bool(lhb.get("应用")),
            "财报风险": fr or None,
            "行业": row.get("行业"),
        }
    return out


def run_council(codes: list[str], as_of: str) -> dict[str, dict]:
    """对候选 codes 跑策略0多专家合议 → {code: {综合分,综合方向,财报红旗数,龙虎榜否决,财报风险,行业}}。

    persist=False(不覆盖闭环已落 view)。历史不足/无信号的票不入 top → map 无该 code(下游标数据不足)。
    """
    from tools.pipeline.screen_council import run_council_screen
    view = run_council_screen(list(dict.fromkeys(codes)), as_of=as_of,
                              fetch=False, top_n=len(codes) + 5, persist=False)
    return _council_rows_to_map(view.get("top") or [])


def run_council_universe(as_of: str, *, top_n: int = STRATEGY_TOP_N,
                         universe_limit: Optional[int] = None,
                         fetch: bool = False) -> dict[str, dict]:
    """**策略线主体**:全A(离线缓存票池)跑策略0合议 → 取 top_n → {code: council 字段}(含 行业)。

    不被板块闸门约束——这些是"原策略直接选出的强票"。fetch=False 用本地缓存 K 线(不触网·as_of 控)。
    """
    from tools.pipeline.screen_council import run_council_screen, _offline_universe_codes
    codes = _offline_universe_codes(limit=universe_limit)
    if not codes:
        logger.warning("策略线:离线全A票池为空(先补缓存 K 线),策略线跳过")
        return {}
    view = run_council_screen(codes, as_of=as_of, fetch=fetch, top_n=top_n, persist=False)
    m = _council_rows_to_map(view.get("top") or [])
    logger.info("策略线 council(全A%d) → top%d:%s", len(codes), len(m), "、".join(m.keys()))
    return m


# 附加策略:各取 top-K,只记"策略命中"名(供并集加权 + 提示词策略借鉴),council 仍是主排序面。
_EXTRA_STRATEGIES = (
    ("动量", "tools.pipeline.screen_momentum", "run_momentum_screen"),
    ("强势S05", "tools.pipeline.screen_strong", "run_strong_screen"),
    ("趋势模板", "tools.pipeline.screen_trend_template", "run_trend_template_screen"),
)


def run_extra_strategies(as_of: str, *, top_k: int = EXTRA_TOP_K,
                         universe_limit: Optional[int] = None) -> dict[str, list[str]]:
    """best-effort 跑附加策略(动量/强势/趋势模板)各取 top-K → {code: [命中策略名]}。

    单策略失败/接口不符 → 记 warning 跳过,不阻断双路并集(council 是硬底线)。
    """
    from tools.pipeline.screen_council import _offline_universe_codes
    import importlib
    codes = _offline_universe_codes(limit=universe_limit)
    hits: dict[str, list[str]] = {}
    if not codes:
        return hits
    for name, mod_path, fn_name in _EXTRA_STRATEGIES:
        try:
            mod = importlib.import_module(mod_path)
            fn = getattr(mod, fn_name, None)
            if fn is None:
                continue
            view = fn(codes, as_of=as_of, fetch=False)
            top = (view or {}).get("top") or []
            for row in top[:top_k]:
                code = row.get("code") or row.get("symbol")
                if code:
                    hits.setdefault(code, []).append(name)
        except Exception as e:                            # noqa: BLE001
            logger.warning("附加策略 %s 跳过:%s", name, str(e)[:120])
    return hits


# ════════════════════ 入场/止损 程序回填(堵『大模型写数字』漏洞) ════════════════════
# LLM 只据形态从下列枚举选一个『入场方式』(不产任何价位数字);挂单价/止损价/不追高上限一律程序回填。
ENTRY_METHODS = ("回踩MA5", "回踩MA20", "回踩前低", "突破确认", "缩量企稳", "突破新高确认")
_入场方式_默认 = "回踩MA5"           # LLM 缺/乱填 → 兜底回踩MA5(最保守·站上5日线才买)
_旁路入场方式 = "突破新高确认"        # 强势不回踩旁路专属:挂 D 日 high、受控追(见 fill_entry_exit / §3.2)
_旁路_CAP_MULT = 1.03                # §3.2 追高硬顶倍数(D日high×此值);回测 §4.5 网格 {1.02,1.03} OOS 选

# ── 强势不回踩旁路 · 够格判据阈值(§2·全部写死·预注册·绝不对既往赢家调参)──
_旁路_量比_下 = 1.0                   # G4 温和放量下界(量比≥1 放量确认)
_旁路_量比_上 = 2.5                   # G4 上界(排除爆量·爆量常见于一日游/见顶)
_旁路_pos60_下 = 0.50                # G5 低位起步下界(确是启动·非刚触底)
_旁路_pos60_上 = 0.85                # G5 上界(离顶尚有空间·严于通用0.95 veto)
_旁路_距60高_上 = -0.08              # G6 现价距60日高≥8%空间(与G5双保险)
_旁路_专属pos60闸 = 0.90             # §3.2 旁路专属硬闸:pos60≥此值直接踢出旁路(严于通用0.95)


def _r3(v) -> Optional[float]:
    return round(float(v), 3) if isinstance(v, (int, float)) else None


def fill_entry_exit(stock: dict, form: Optional[dict]) -> dict:
    """**程序回填**入场/止损价位(核心·堵『大模型抄写数字』转写风险)。

    据 `stock['入场方式']`(枚举) + 程序已算好的 `form`(ma5/ma20/前低/现价),算出三个数值写回 stock:
      · 挂单价(entry) / 止损价(stop) / 不追高上限(cap)
    映射(初版·方案 §3.1;判据固定,不对既往赢家调参):
      · 回踩MA5   → 挂单=ma5;   止损=ma20;      不追高=现价
      · 回踩MA20  → 挂单=ma20;  止损=前低;      不追高=现价
      · 回踩前低  → 挂单=前低;  止损=前低×0.98; 不追高=现价
      · 突破确认/缩量企稳 → 挂单=现价; 止损=ma20; 不追高=现价×1.02
      · 突破新高确认(强势旁路)→ 挂单=clip(当日high, 下界现价, 上界cap); 止损=max(ma5, 当日low);
        cap=min(当日high×_旁路_CAP_MULT, 现价×(1+涨停线-0.02))。**这是唯一允许挂单>现价的方式**
        (受控追高:不越 cap;回踩三方式的 entry=min(entry,现价) 铁律原样保留、不受影响)。

    **LLM 若在文字里仍写了数字 → 程序一律忽略,以本回填为准**(defense-in-depth,仿 hard_veto)。
    form 数据不足 → 三价位=None + 标『数据不足·人工确认』,绝不编。
    """
    form = form or {}
    method = stock.get("入场方式")
    if method not in ENTRY_METHODS:
        method = _入场方式_默认               # 缺/非法枚举 → 兜底,并回写留痕
        stock["入场方式"] = method
    if form.get("数据不足") or not form:
        stock["挂单价"] = stock["止损价"] = stock["不追高上限"] = None
        stock["价位说明"] = "数据不足·人工确认"
        return stock
    ma5, ma20 = form.get("ma5"), form.get("ma20")
    前低, 现价 = form.get("前低"), form.get("现价")
    entry = stop = cap = None
    if method == "回踩MA5":
        entry, stop, cap = ma5, ma20, 现价
    elif method == "回踩MA20":
        entry, stop, cap = ma20, 前低, 现价
    elif method == "回踩前低":
        entry = 前低
        stop = 前低 * 0.98 if isinstance(前低, (int, float)) else None
        cap = 现价
    elif method == _旁路入场方式:            # 突破新高确认(强势旁路·§3.2 受控追高)
        当日high, 当日low = form.get("当日high"), form.get("当日low")
        if not isinstance(当日high, (int, float)) or not isinstance(现价, (int, float)):
            stock["挂单价"] = stock["止损价"] = stock["不追高上限"] = None
            stock["价位说明"] = "旁路数据不足(缺当日high/现价)·人工确认"
            return stock
        lim = limit_pct(stock.get("code") or "")
        近涨停线 = 现价 * (1.0 + lim - 0.02)   # 追高不越"次日近涨停"线(与涨停不可买留 2% 缓冲)
        cap = min(当日high * _旁路_CAP_MULT, 近涨停线)
        entry = min(max(当日high, 现价), cap)   # clip(当日high, 下界现价, 上界cap):允许 entry>现价但≤cap
        # 止损:跌破 ma5 或跌破启动日低点即走(取二者较高=更紧);单调性夹逼保证 stop<entry
        cands = [v for v in (ma5, 当日low) if isinstance(v, (int, float))]
        stop = max(cands) if cands else None
    else:  # 突破确认 / 缩量企稳:回踩确认位=现价挂单,给 2% 追高容忍上限
        entry = 现价
        stop = ma20
        cap = 现价 * 1.02 if isinstance(现价, (int, float)) else None
    # 单调性夹逼:防均线空头/现价跌破均线时出现"止损≥买点"或"红线<买点"的反常卡。
    # 铁律:回踩限价绝不挂到现价之上(不追);止损恒在买点下方;红线(不追高上限)恒不低于买点。
    if isinstance(entry, (int, float)) and isinstance(现价, (int, float)) \
            and method in ("回踩MA5", "回踩MA20", "回踩前低"):
        entry = min(entry, 现价)                       # 回踩限价不挂到现价之上
    if isinstance(entry, (int, float)):
        if isinstance(stop, (int, float)) and stop >= entry:
            stop = entry * 0.98                        # 止损恒低于买点
        if isinstance(cap, (int, float)):
            cap = max(cap, entry)                      # 红线恒不低于买点
        else:
            cap = entry
    stock["挂单价"] = _r3(entry)
    stock["止损价"] = _r3(stop)
    stock["不追高上限"] = _r3(cap)
    stock.pop("价位说明", None)
    return stock


# ════════════════════ 强势不回踩旁路 · 够格判据 + 专属硬闸(§2/§3.2) ════════════════════
ROLE_BYPASS_OK = (ROLE_LEADER, ROLE_CORE, ROLE_STRATEGY)   # G3 可走旁路角色(跟涨排除)


def bypass_hard_gate(form: Optional[dict]) -> Optional[str]:
    """§3.2 旁路专属硬闸(比通用 hard_veto 更严):命中即**踢出旁路**(可回落普通回踩通道)。

    当前只含"位置更严"闸:pos60 ≥ 0.90(通用 veto 是 0.95,旁路留更大安全垫防站岗)。
    返回命中原因(留痕);未命中 → None。追高硬顶 cap 在 fill_entry_exit 落实,不在此。
    """
    form = form or {}
    pos = form.get("位置pos60")
    if isinstance(pos, (int, float)) and pos >= _旁路_专属pos60闸:
        return f"旁路位置闸:pos60={pos:.2f}≥{_旁路_专属pos60闸}(严于通用veto·防站岗)"
    return None


def bypass_eligible(cand: dict, form: Optional[dict], board_ctx: Optional[dict] = None,
                    *, require_mainline: bool = True) -> Optional[str]:
    """§2 够格判据:一票是否够格走"强势不回踩旁路"。够格 → 返回命中说明字符串;不够格 → None。

    **纯函数·判据全部写死·可复现·预注册不对既往赢家调参**。逐条 AND(任一不满足即 None):
      G1 均线多头  G2 主线利好板块且强弱=强(require_mainline=False 时跳过·退化口径)
      G3 角色∈{龙头/中军/策略}  G4 量比∈[1.0,2.5]  G5 pos60∈[0.50,0.85]
      G6 距60高≤-8%  G7 当日红盘且未近涨停  G8 非涨停不可买
    注:本函数只判"够格",不含 §3.1 通用 hard_veto / §3.2 专属闸(那两道在下游另行叠加,正交)。
    """
    form = form or {}
    if form.get("数据不足"):
        return None
    # G1 趋势:均线多头排列
    if not form.get("均线多头"):
        return None
    # G3 角色:龙头/中军/策略(跟涨不走旁路)
    role = cand.get("role")
    if role not in ROLE_BYPASS_OK:
        return None
    # G2 主线/风口:属消息利好板块且强弱=强(退化口径 require_mainline=False 时跳过)
    if require_mainline:
        ctx = board_ctx or cand.get("board_ctx") or {}
        strong = str(ctx.get("强弱") or "")
        if not cand.get("board") or "强" not in strong:
            return None
    # G4 温和放量
    vr = form.get("量比")
    if not isinstance(vr, (int, float)) or not (_旁路_量比_下 <= vr <= _旁路_量比_上):
        return None
    # G5 低位起步 pos60∈[0.50,0.85]
    pos = form.get("位置pos60")
    if not isinstance(pos, (int, float)) or not (_旁路_pos60_下 <= pos <= _旁路_pos60_上):
        return None
    # G6 离顶有空间:距60高≤-8%
    d60 = form.get("距60高")
    if not isinstance(d60, (int, float)) or d60 > _旁路_距60高_上:
        return None
    # G7 当日红盘且未近涨停(温和上涨·不追暴涨/连板当天)
    chg = form.get("当日涨跌")
    lim = limit_pct(cand.get("code") or "")
    if not isinstance(chg, (int, float)) or not (0.0 < chg < lim - 0.02):
        return None
    # G8 非涨停不可买
    if form.get("涨停不可买"):
        return None
    return (f"旁路够格:多头+{'主线强' if require_mainline else '主线代理'}+{role}"
            f"+量比{vr:.2f}+pos60={pos:.2f}+距60高{d60 * 100:.1f}%+当日{chg * 100:+.1f}%")


def maybe_route_bypass(stock: dict, cand: dict, form: Optional[dict],
                       *, require_mainline: bool = True) -> Optional[str]:
    """(opt-in)把够格的强势票**路由到旁路入场方式**:够格 + 未命中通用 hard_veto + 未命中 §3.2 专属闸
    → 覆盖 `stock['入场方式']=突破新高确认` 并留痕 `stock['旁路命中']`。返回命中说明或 None(不改)。

    **默认不启用**(synthesize enable_bypass=False):旁路 live 采用须先经 §5 回测跑赢基线。本函数供
    回测/启用后调用。硬闸命中(极高位/涨停/财报/龙虎 或 pos60≥0.90)→ 不路由,该票走原回踩通道。
    """
    reason = bypass_eligible(cand, form, cand.get("board_ctx"), require_mainline=require_mainline)
    if not reason:
        return None
    if hard_veto_reason(cand.get("role"), stock.get("council"), form):
        return None                                   # 通用硬闸命中 → 不走旁路(交回踩/剔除)
    if bypass_hard_gate(form):
        return None                                   # §3.2 专属位置闸命中 → 不走旁路
    stock["入场方式"] = _旁路入场方式
    stock["旁路命中"] = reason
    return reason


# ════════════════════ DeepSeek 分析师合成(双依据提示词 + schema) ════════════════════
# 每票输出档:文字优先(仿 board_verdict rubric),建议分 0-10 供排序。
SELECTION_SCHEMA = {
    "个股": ("list,每票一个 dict:{code, name, 档:推荐|观察|剔除(据【建议分档位定义】给), "
            "建议分:0-10 数值(与档位定义一致), "
            "理由:一句话≤50字·**必须分别点明『策略依据』与『板块依据』**"
            "(如『策略:council看多+动量命中;板块:电子利好·强催化』), "
            "入场方式:据个股形态从 {回踩MA5, 回踩MA20, 回踩前低, 突破确认, 缩量企稳} **只选一个类型**"
            "(**绝不要写任何价位数字**——挂单价/止损价/不追高上限全部由程序按均线/前低回填), "
            "风险:一句话}"),
    "规避提示": "一句话:本组需规避/降级的情形(板块消息转弱/资金流出/高位拥挤等),无则写『无』",
}


def SELECTION_SYNTH_INSTRUCTION(group_name: str, regime: dict, *,
                                board_tag: Optional[str] = None) -> str:
    """选股分析师**双依据**合成提示词(rubric 文字优先·硬纪律写死)。

    group_name=组名(利好板块名 / 规避板块名 / "策略直选");
    board_tag∈{"利好","规避",None}:决定板块借鉴的语气(催化加成 / 规避降级 / 无板块仅策略)。
    """
    reg = (f"风险偏好={regime.get('风险偏好')}·宏观净方向={regime.get('宏观净方向')}"
           f"·宏观情景={regime.get('宏观情景')}·广度档={regime.get('广度档')}")
    if board_tag == "利好":
        board_line = (f"本组个股属**消息利好板块「{group_name}」**——板块借鉴=催化加成(利好+强催化→"
                      "同等策略面下可抬分),但**不是唯一依据**。")
    elif board_tag == "规避":
        board_line = (f"本组个股属**规避板块「{group_name}」(板块消息转弱/拥挤过热)**——板块借鉴=规避/降级"
                      "(除非策略面极强且形态健康,否则压到观察及以下,理由须点明板块利空)。")
    else:
        board_line = ("本组为**策略直选票(不隶属任何消息利好板块)**——**板块借鉴中性**:板块无消息催化"
                      "不代表看空,一律**以策略面为主**判选/不选,理由的『板块依据』写明"
                      "『无板块消息催化·纯策略直选』或所属板块的中性/利空状态。")
    return (
        f"你是**选股分析师**。给你「{group_name}」组候选票的**多维数据**"
        "(策略面 × 板块消息面 × 形态)+ 当前盘面 regime,请按**整体选股策略**逐票判"
        "选/不选,并给建议分(0-10)。\n\n"
        f"【盘面 regime】{reg}\n\n"
        f"【本组板块属性】{board_line}\n\n"
        "【每票给你的数据】code/name/角色(龙头/中军/跟涨/策略) + 来源(策略直选/板块催化/两者兼有) + "
        "策略面(命中哪些策略/council综合分+综合方向/财报红旗数/龙虎榜否决) + "
        "板块面(所属板块/板块消息面利好或利空/强弱/持续性/个股是否已动/规避原因) + "
        "形态(距20高/距60高/获利盘/位置pos60/量比/均线多头/当日涨跌/涨停不可买/ATR%)。\n\n"
        "【策略分值口径(这些是**程序/策略算出的客观指标·不是你打的分**,请按此口径解读,勿臆测)】\n"
        "· council综合分 ∈[-1,1]:>0.3 看多 / [-0.3,0.3] 中性 / <-0.3 看空,数值越高越看多(多专家合议)。\n"
        "· 财报红旗数:高危财报瑕疵计数,≥1 即有高危瑕疵(硬纪律直接剔除)。龙虎榜否决=资金微结构净卖否决。\n"
        "· 获利盘 ∈[0,1]:成本≤现价的筹码占比,越高浮盈盘越重、上方抛压越大(≥0.95 极高位)。\n"
        "· 位置pos60 ∈[0,1]:现价在近60日高低区间的分位,越接近1 越贴近区间高点(≥0.95 且贴高=极高位)。\n"
        "· 距20高/距60高:现价相对近20/60日最高价的百分比,负值=低于高点(越负越远离高点/越深调整)。\n"
        "· 量比:当日量 / 近5日均量,>1 放量、<1 缩量。均线多头=ma5>ma10>ma20 且站上 ma5。ATR%=波动幅度。\n\n"
        "【建议分档位定义(0-10·你据此给『档』,别裸给分——先按定义定档再给对应分)】\n"
        "· 8-10=强推:硬催化(利好板块+强催化)× 策略看多 × 财报净 × 形态健康,罕见,宁缺毋滥。\n"
        "· 6.5-8=推荐:策略面偏多 或 强催化,且财报净、形态健康(回踩到位/突破确认),可主选建仓。\n"
        "· 5-6.5=中性观察:方向或形态存在瑕疵(如均线未多头/位置略高/量能一般),看而不急、等更好点位。\n"
        "· 3-5=偏弱观察:策略偏空 或 形态明显弱(深度空头/远离均线/缩量),仅联动/备选,不建仓。\n"
        "· 0-2=剔除:硬纪律命中(财报红旗/龙虎否决/涨停不可买/极高位)或策略与形态双弱。\n\n"
        "【硬纪律(务必遵守)】\n"
        "① **双依据必写**:每票理由**分别写明『策略依据』(命中哪些策略/council 方向)与『板块依据』"
        "(所属板块消息利好/利空/催化/中性,及为何加成或降级或中性)**。两条都要出现。\n"
        "② **策略线不被板块旁路**:策略直选的强票即使无板块催化,只要策略面强+形态健康,照常可给推荐;"
        "**不因『板块没被判利好』就一刀切剔除或降级**。\n"
        "③ **催化优先 × 不追高 × 入场只选类型(不产数字)**:选催化/策略驱动的强势票;入场**据个股形态给合适方式**"
        "——从 {回踩MA5, 回踩MA20, 回踩前低, 突破确认, 缩量企稳} 选一个『入场方式』"
        "(**只给类型·绝不写价位数字**,挂单价/止损价/不追高上限由程序按均线/前低回填),"
        "但**一律不追涨停/不追高开**。\n"
        "④ **龙头/中军/策略为主线,跟涨(补涨先锋)只作『联动观察』不进主选**:跟涨角色最高只给『观察』档。\n"
        "⑤ **反选剔除(命中任一即『剔除』档、建议分≤2)**:财报高危红旗(财报红旗数≥1)/龙虎榜否决/"
        "涨停不可买/极高位高抛压(获利盘≥0.95 或 位置pos60≥0.95 且距高接近0)。\n"
        "⑥ **规避板块内票**:除非策略面极强+形态健康,否则压到观察及以下,理由点明板块利空/拥挤。\n"
        "⑦ **只用给定数据,禁止编造,禁止使用给定日期之后的未来信息**。\n\n"
        "输出严格 JSON,个股顺序同输入。"
    )


def _source_label(srcs: set) -> str:
    """来源集合 → 标注文字。"""
    if SRC_STRATEGY in srcs and SRC_BOARD in srcs:
        return SRC_BOTH
    if SRC_STRATEGY in srcs:
        return SRC_STRATEGY
    return SRC_BOARD


def _stock_llm_payload(cand: dict, council: dict, form: dict) -> dict:
    """喂给 LLM 的单票精简数据(只给判据、不给结论)——**策略面 + 板块面 双维**。"""
    board = cand.get("board")
    board_ctx = cand.get("board_ctx") or {}
    strat_hits = list(dict.fromkeys(cand.get("策略命中") or []))
    return {
        "code": cand["code"], "name": cand.get("name", ""), "角色": cand.get("role"),
        "来源": _source_label(cand.get("来源") or set()),
        "策略面": {
            "命中策略": strat_hits or (["council合议"] if council else []),
            "council综合分": council.get("综合分"), "council综合方向": council.get("综合方向"),
            "财报红旗数": council.get("财报红旗数"), "龙虎榜否决": council.get("龙虎榜否决"),
        } if council else {"命中策略": strat_hits, "数据不足": True},
        "板块面": {
            "所属板块": board or "无(策略直选)",
            "板块消息面": board_ctx.get("tag") or ("规避" if cand.get("规避") else "无消息催化"),
            "板块强弱": board_ctx.get("强弱"), "板块持续性": board_ctx.get("持续性"),
            "个股已动": cand.get("已动"), "联动依据": cand.get("联动依据"),
            "规避原因": cand.get("规避"),
        },
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
    · 跟涨角色 → 最高只到『观察』(龙头/中军/策略为主线,跟涨只联动观察)。
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


def apply_avoid_downgrade(stock: dict, avoid_reason: Optional[str]) -> dict:
    """板块消息线规避维:隶属规避板块池的票**降级**(推荐→观察),留痕板块利空原因。

    非硬闸(不强制剔除)——极强策略票仍可留观察;与 hard_veto(强制剔除)正交。
    """
    if not avoid_reason:
        return stock
    if stock.get("档") == "推荐":
        stock["档"] = "观察"
    base = stock.get("理由") or ""
    tag = f"[板块规避:{avoid_reason}]"
    if "板块规避" not in base:
        stock["理由"] = f"{tag} {base}".strip()
    stock["板块规避命中"] = avoid_reason
    return stock


def synthesize_group(group_name: str, board_ctx: Optional[dict], candidates: list[dict],
                     council_map: dict, form_map: dict, regime: dict, *,
                     board_tag: Optional[str] = None, client=None,
                     enable_bypass: bool = False) -> dict:
    """单组合成(利好板块 / 规避板块 / 策略直选):组多维 payload → DeepSeek client.extract →
    每票档/分/理由(双依据)+ 硬纪律回扣 + 规避降级 + 来源标注。

    LLM 失败 → 降级(每票档=观察·建议分=None·标 LLM失败),不崩;上游据此诚实标注。
    """
    from tools.llm import client as lc
    client = client or lc.get_client()
    payloads = [
        _stock_llm_payload(c, council_map.get(c["code"], {}), form_map.get(c["code"], {}))
        for c in candidates
    ]
    text = json.dumps({"组": group_name, "候选": payloads}, ensure_ascii=False)
    try:
        r = client.extract(text, SELECTION_SCHEMA,
                           instruction=SELECTION_SYNTH_INSTRUCTION(group_name, regime,
                                                                   board_tag=board_tag))
    except Exception as e:                                # noqa: BLE001
        logger.warning("组 %s 合成 LLM 失败:%s", group_name, e)
        r = {"个股": [{"code": c["code"], "name": c.get("name", ""), "档": "观察",
                      "建议分": None, "理由": "LLM合成失败降级", "入场方式": None,
                      "风险": "研判缺失,需人工"} for c in candidates],
             "规避提示": f"LLM失败:{str(e)[:40]}"}
    # 回挂原始多维数据(留痕、人工可核)+ 硬纪律 + 规避降级 + 来源标注
    by_code = {s.get("code"): s for s in (r.get("个股") or [])}
    merged = []
    for c in candidates:
        s = by_code.get(c["code"], {"code": c["code"], "name": c.get("name", ""),
                                     "档": "观察", "建议分": None, "理由": "LLM未返回"})
        s["角色"] = c.get("role")
        s["来源"] = _source_label(c.get("来源") or set())
        s["策略命中"] = list(dict.fromkeys(c.get("策略命中") or []))
        s["board"] = c.get("board")
        s["council"] = council_map.get(c["code"])
        s["形态"] = form_map.get(c["code"])
        apply_hard_discipline(s, c.get("role"), s["council"], s["形态"])
        apply_avoid_downgrade(s, c.get("规避"))
        # (opt-in)强势不回踩旁路:够格 + 未命中硬闸 → 覆盖入场方式为"突破新高确认"(默认关闭·待回测放行)
        if enable_bypass:
            maybe_route_bypass(s, c, s["形态"])
        # 入场/止损价位一律程序回填(LLM 只给『入场方式』枚举;文字里若含数字被忽略,以回填为准)
        fill_entry_exit(s, s["形态"])
        merged.append(s)
    merged.sort(key=lambda s: (s.get("建议分") if isinstance(s.get("建议分"), (int, float))
                               else -1), reverse=True)
    return {"board": group_name, "板块消息面": board_ctx, "board_tag": board_tag,
            "个股": merged, "规避提示": r.get("规避提示")}


# ════════════════════ 双路并集编排 + 产物落盘 ════════════════════
def _merge_candidate(registry: dict[str, dict], code: str, *, name: str = "",
                     role: Optional[str] = None, board: Optional[str] = None,
                     board_ctx: Optional[dict] = None, src: str,
                     strat_hits: Optional[list[str]] = None,
                     已动=None, 联动依据=None, 规避: Optional[str] = None) -> None:
    """把一票并入并集 registry(按 code 去重合并;板块上下文/角色优先保留非空;来源集合累加)。"""
    cur = registry.get(code)
    if cur is None:
        cur = {"code": code, "name": name or _name_of(code), "role": role,
               "board": board, "board_ctx": board_ctx, "来源": set(),
               "策略命中": [], "已动": 已动, "联动依据": 联动依据, "规避": 规避}
        registry[code] = cur
    cur["来源"].add(src)
    if strat_hits:
        cur["策略命中"] = list(dict.fromkeys((cur.get("策略命中") or []) + strat_hits))
    if name and not cur.get("name"):
        cur["name"] = name
    # 板块上下文优先保留(板块催化线信息比策略线角色更具体)
    if board and not cur.get("board"):
        cur["board"] = board
    if board_ctx and not cur.get("board_ctx"):
        cur["board_ctx"] = board_ctx
    if role and (cur.get("role") in (None, ROLE_STRATEGY)):
        cur["role"] = role
    for k, v in (("已动", 已动), ("联动依据", 联动依据), ("规避", 规避)):
        if v is not None and cur.get(k) is None:
            cur[k] = v


def synthesize(date: str, *, data_root: Optional[Path] = None, client=None,
               boards: Optional[list[str]] = None, strategy_top_n: int = STRATEGY_TOP_N,
               universe_limit: Optional[int] = None, extra_screens: bool = False,
               enable_bypass: bool = False) -> dict:
    """S5 主编排(**双路并集**):策略线(全A council top-N + 附加策略)∪ 板块消息线(利好催化 + 规避降级)
    → 按组(利好板块 / 策略直选 / 规避)DeepSeek 双依据合成 → 结构化结果 dict。
    """
    from tools.analysis.market_forecast import dataroot
    root = data_root or dataroot.ensure_data_root()
    focus = load_sector_focus(date, data_root=root)
    regime = load_regime(focus)
    good_boards = load_message_boards(focus, as_of=date)
    avoid_map = load_avoid_boards(focus)
    if boards:
        good_boards = [b for b in good_boards
                       if (b.get("board") or b.get("板块")) in boards]

    registry: dict[str, dict] = {}

    # ── (a) 策略线(主体·全A·不被板块闸门)──────────────────────────────
    strat_council = run_council_universe(date, top_n=strategy_top_n,
                                         universe_limit=universe_limit)
    extra_hits = run_extra_strategies(date, universe_limit=universe_limit) if extra_screens else {}
    for code in strat_council:
        _merge_candidate(registry, code, name=_name_of(code), role=ROLE_STRATEGY,
                         src=SRC_STRATEGY, strat_hits=["council合议"])
    for code, names in extra_hits.items():
        # 附加策略命中:并入(可能是新票,也可能给已有票加"策略命中")
        _merge_candidate(registry, code, name=_name_of(code), role=ROLE_STRATEGY,
                         src=SRC_STRATEGY, strat_hits=names)

    # ── (b) 板块消息线(利好板块催化候选)────────────────────────────────
    board_ctx_by_name: dict[str, dict] = {}
    for blk in good_boards:
        sw = blk.get("board") or blk.get("板块") or ""
        board_ctx = {
            "tag": blk.get("tag"), "强弱": blk.get("强弱"),
            "关键事件": blk.get("关键事件"), "持续性": blk.get("持续性"),
            "时效": blk.get("时效"), "可靠性综述": blk.get("可靠性综述"),
            "依据": blk.get("依据"),
        }
        board_ctx_by_name[sw] = board_ctx
        for c in gather_candidates(date, blk):
            _merge_candidate(registry, c["code"], name=c.get("name", ""), role=c.get("role"),
                             board=sw, board_ctx=board_ctx, src=SRC_BOARD,
                             已动=c.get("已动"), 联动依据=c.get("联动依据"))

    if not registry:
        logger.warning("双路并集:策略线+板块线均无候选(date=%s)", date)

    all_codes = list(registry.keys())

    # ── 策略面(council):策略线已有;板块催化票/附加票缺 council 的补跑一次 ──
    council_map = dict(strat_council)
    missing = [c for c in all_codes if c not in council_map]
    if missing:
        council_map.update(run_council(missing, as_of=date))

    # ── 形态面(load_klines + as-of pattern_metrics)────────────────────
    from tools.backtest.screen_forward_common import load_klines
    klines = load_klines(all_codes, min_bars=MIN_BARS) if all_codes else {}
    form_map = {code: pattern_metrics(df, code, date=date) for code, df in klines.items()}
    for code in all_codes:
        form_map.setdefault(code, {"数据不足": True})

    # ── (b续) 规避板块池:标注隶属规避板块的票(按 board 或 council 行业→申万一级)────
    for code, cand in registry.items():
        sw = cand.get("board") or sw_of(code, (council_map.get(code) or {}).get("行业"))
        if sw and sw in avoid_map:
            cand["规避"] = avoid_map[sw]
            if not cand.get("board"):
                cand["board"] = sw
                cand["board_ctx"] = {"tag": "利空/规避", "强弱": "弱",
                                     "持续性": avoid_map[sw]}

    # ── (c) 综合并集:按组(利好板块 / 规避板块 / 策略直选)DeepSeek 双依据合成 ──
    #   分组键:隶属利好板块 → 该板块组;隶属规避板块 → "规避·<板块>"组;否则 → "策略直选"组。
    groups: dict[str, dict] = {}   # key -> {name, tag, ctx, cands}
    for code, cand in registry.items():
        board = cand.get("board")
        if board and board in board_ctx_by_name:
            key, name, tag, ctx = board, board, "利好", board_ctx_by_name[board]
        elif cand.get("规避"):
            key = f"规避·{board or '未知'}"
            name, tag, ctx = (board or "规避板块"), "规避", cand.get("board_ctx")
        else:
            key, name, tag, ctx = "策略直选", "策略直选", None, None
        g = groups.setdefault(key, {"name": name, "tag": tag, "ctx": ctx, "cands": []})
        g["cands"].append(cand)

    board_results = []
    # 组顺序:利好板块 → 策略直选 → 规避,稳定可读
    def _order(k: str) -> tuple:
        g = groups[k]
        return ({"利好": 0, None: 1, "规避": 2}.get(g["tag"], 1), g["name"])
    for key in sorted(groups.keys(), key=_order):
        g = groups[key]
        board_results.append(
            synthesize_group(g["name"], g["ctx"], g["cands"], council_map, form_map,
                             regime, board_tag=g["tag"], client=client,
                             enable_bypass=enable_bypass))

    return {
        "date": date, "version": SYNTH_VERSION,
        "as_of": (focus.get("消息驱动") or {}).get("as_of") or date,
        "regime": regime,
        "板块": board_results,
        "双路统计": {
            "策略线票数": sum(1 for c in registry.values() if SRC_STRATEGY in c["来源"]),
            "板块催化票数": sum(1 for c in registry.values() if SRC_BOARD in c["来源"]),
            "两者兼有": sum(1 for c in registry.values()
                          if SRC_STRATEGY in c["来源"] and SRC_BOARD in c["来源"]),
            "并集总数": len(registry),
            "规避降级票数": sum(1 for c in registry.values() if c.get("规避")),
        },
        "口径": ("S5 双路并集:(a)策略线=全A screen_council top-N(+附加策略,不被板块闸门)"
                "∪ (b)板块消息线=利好板块催化候选(龙头/中军/跟涨)+规避板块池降级 → 每票标来源"
                "(策略直选/板块催化/两者兼有)→ DeepSeek 双依据分析师(策略借鉴+板块借鉴)"
                "逐票 档/建议分/回踩限价入场;硬纪律程序兜底。"),
        "防未来": ("量价/财报 as-of(只用≤date K线+披露≤date财报+龙虎榜list_date<date);"
                  "回踩限价入场不追高开;判据固定不对既往赢家调参。新闻走 S3 live 口径(最大可得·非防未来)。"),
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


# 两层闸门脚注(方案 §3.3):价位层程序静态挂单闸门(本轮落地) + 时机层实时盘中(归日内线·暂未接入)。
_闸门脚注 = (
    "**入场闸门分两层**:(1)**价位层**=均线/前低算出的静态挂单闸门,**开盘前已定、当日可用不滞后**"
    "(今日照挂限价买+不追高即可);(2)**时机层**=当日盘中「此刻扣不扣扳机」,需实时分时信号"
    "(现价站上均线+量比>1+不追涨停/封单确认),**归日内线,本轮暂未接入——先按价位层挂单+不追高操作**。"
)


def _signal_str(form: Optional[dict]) -> str:
    """把形态指标拼成一句通俗『信号』(昨涨跌·量比·距高·获利·多头)。数据不足 → 提示。"""
    if not form or form.get("数据不足"):
        return "形态数据不足·人工确认"
    parts: list[str] = []
    if form.get("当日涨跌") is not None:
        parts.append(f"昨{_fmt_pct(form['当日涨跌'])}")
    if form.get("量比") is not None:
        parts.append(f"量比{form['量比']}")
    if form.get("距20高") is not None:
        parts.append(f"距20高{_fmt_pct(form['距20高'])}")
    wr = form.get("获利盘")
    if wr is not None:
        parts.append(f"获利{wr * 100:.0f}%")
    elif form.get("位置pos60") is not None:
        parts.append(f"pos60={form['位置pos60']:.2f}")
    if form.get("均线多头"):
        parts.append("均线多头")
    if form.get("涨停不可买"):
        parts.append("涨停不可买")
    return "·".join(parts) or "-"


def _operate_card(board: str, s: dict) -> list[str]:
    """单票**通俗操作卡**(方案 §3.2):买点(元·区间·入场方式)/止损(元)/红线(不追高·元)/信号/理由。

    价位全部来自程序回填字段(挂单价/止损价/不追高上限);None → 『数据不足·人工确认』,不编。
    """
    form = s.get("形态") or {}
    name = s.get("name") or ""
    档 = s.get("档") or "-"
    score = s.get("建议分")
    score_s = f"{score}" if isinstance(score, (int, float)) else "-"
    方式 = s.get("入场方式") or "-"
    entry, stop, cap = s.get("挂单价"), s.get("止损价"), s.get("不追高上限")
    lines = [f"- **{name} {s['code']}** ｜ {档} {score_s} ｜ 来源：{s.get('来源') or '-'} ｜ 组：{board}"]
    if isinstance(entry, (int, float)):
        lines.append(f"  - 买点：{entry:.2f}–{entry * 1.005:.2f} 元 挂限价买（{方式}，不追高）")
    else:
        lines.append(f"  - 买点：数据不足·人工确认（{方式}）")
    if isinstance(stop, (int, float)):
        lines.append(f"  - 止损：跌破 {stop:.2f} 元 走")
    else:
        lines.append("  - 止损：数据不足·人工确认")
    if isinstance(cap, (int, float)):
        lines.append(f"  - 红线：高于 {cap:.2f} 元 = 追高别买")
    else:
        lines.append("  - 红线：数据不足·人工确认")
    lines.append(f"  - 信号：{_signal_str(form)}")
    lines.append(f"  - 理由：{s.get('理由') or '-'}")
    return lines


def render_md(result: dict) -> str:
    date = result["date"]
    reg = result.get("regime") or {}
    ds = result.get("双路统计") or {}
    L: list[str] = []
    L.append(f"# 今日选股 · {date}（双路并集：策略线 ∪ 板块消息线 · 程序化 S5）\n")
    L.append("> ⚠️ 测试环境研究模拟，非投资建议。KPI = 绝对收益"
             "（Model A：D 选 → D+1 回踩限价入场 → D+1 收盘绝对为正 → D+2 卖出线）。")
    L.append(f"> 产出方式：程序化 `tools.analysis.selection_synth`（版本 {result.get('version')}）"
             "，DeepSeek 双依据分析师合成，非 Claude 交互。")
    L.append(f"> as_of={result.get('as_of')}；{result.get('防未来')}\n")

    L.append("## 一、整体盘面（regime）\n")
    L.append(f"- **风险偏好**：{reg.get('风险偏好')}（广度档 {reg.get('广度档')}、"
             f"净广度 {reg.get('净广度')}、涨停 {reg.get('涨停')}/跌停 {reg.get('跌停')}、"
             f"hs300 {reg.get('hs300方向')}）。")
    L.append(f"- **宏观**：净方向 {reg.get('宏观净方向')}、情景 {reg.get('宏观情景')}。")
    L.append(f"- **双路并集统计**：策略线 {ds.get('策略线票数')} 票 ∪ 板块催化 {ds.get('板块催化票数')} 票"
             f"（两者兼有 {ds.get('两者兼有')}）→ 并集 {ds.get('并集总数')} 票；"
             f"规避降级 {ds.get('规避降级票数')} 票。")
    L.append(f"- **口径**：{result.get('口径')}\n")

    L.append("## 二、板块消息面评价\n")
    L.append("| 组 | 属性 | 评价 | 强弱 | 持续性 | 规避提示 |")
    L.append("|---|---|---|---|---|---|")
    for b in result.get("板块") or []:
        ctx = b.get("板块消息面") or {}
        L.append(f"| **{b['board']}** | {b.get('board_tag') or '策略直选'} | {ctx.get('tag') or '-'} | "
                 f"{ctx.get('强弱') or '-'} | {(ctx.get('持续性') or '')[:40]} | {b.get('规避提示') or '-'} |")
    L.append("")

    L.append("## 三、选股（双路并集：策略面 × 板块消息面 × 形态）\n")
    L.append("| 组 | 来源 | 角色 | 代码 | 名称 | 命中策略 | 消息 | 策略分/方向 | 财报/龙虎 | 形态 | 建议分 | 档 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
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
            hits = "/".join(s.get("策略命中") or []) or "-"
            L.append(f"| {b['board']} | {s.get('来源') or '-'} | {s.get('角色')} | {s['code']} | "
                     f"{s.get('name')} | {hits} | {ctx.get('强弱') or '-'} | "
                     f"{cc.get('综合分') if cc else '-'} {cc.get('综合方向') if cc else ''} | {fr} | "
                     f"{_fmt_form(s.get('形态'))} | {score_s} | {档_s} |")
            if 档 == "推荐":
                picks.append((b["board"], s))
    L.append("")

    L.append("## 四、主选操作卡（推荐 · 程序回填价位 · 不追高）\n")
    L.append(f"> {_闸门脚注}")
    L.append("> 价位（买点/止损/红线）均为**程序按均线/前低回填的数值**（大模型只选『入场方式』类型、不产数字）。\n")
    if picks:
        for board, s in sorted(picks, key=lambda x: -(x[1].get("建议分") or 0)):
            L.extend(_operate_card(board, s))
            风险 = s.get("风险")
            if 风险 and 风险 != "-":
                L.append(f"  - 风险：{风险}")
            L.append("")
    else:
        L.append("_今日无『推荐』档（宁缺毋滥/降仓/只留有硬催化或强策略的）。_")
    L.append("")

    L.append("## 五、反选剔除 / 规避降级（数据背书）\n")
    any_cut = False
    for b in result.get("板块") or []:
        for s in b.get("个股") or []:
            if s.get("档") == "剔除" or s.get("板块规避命中"):
                any_cut = True
                tags = []
                if s.get("硬纪律命中"):
                    tags.append(f"反选:{s['硬纪律命中']}")
                if s.get("板块规避命中"):
                    tags.append(f"板块规避:{s['板块规避命中']}")
                L.append(f"- **{b['board']} {s['code']} {s.get('name')}**（{s.get('档')}）："
                         f"{s.get('理由') or ''}"
                         + (f"（{'/'.join(tags)}）" if tags else ""))
    if not any_cut:
        L.append("_无剔除/规避降级。_")
    L.append("")

    L.append("## 六、规避策略（组级）\n")
    for b in result.get("板块") or []:
        tip = b.get("规避提示")
        if tip and tip != "无":
            L.append(f"- **{b['board']}**：{tip}")
    L.append("- **无利好日兜底**：若无高确信利好板块，策略线仍照常直选强票；宁缺毋滥/降仓，不硬凑。\n")

    L.append("## 七、来源 / 角色 —— 口径说明\n")
    L.append("- **来源**：`策略直选`=全A screen_council(+附加策略)选出、不隶属消息利好板块的强票（不被板块闸门旁路）；"
             "`板块催化`=消息利好板块内的催化候选（龙头/中军/跟涨）；`两者兼有`=两路都命中（信号叠加）。")
    L.append("- **角色**：龙头/中军取自消息驱动块+角色关系表；跟涨=补涨先锋只作联动观察；"
             "策略=策略线直选（无板块角色）。")
    L.append("- **龙头/中军/策略为主线，跟涨只联动观察**；规避板块内票降级（除非策略面极强+形态健康）。")
    L.append(f"- **入场闸门**：{_闸门脚注}")
    L.append("- **价位来源**：买点/止损/红线=程序据均线(ma5/ma20)/前低回填（大模型只据形态选『入场方式』类型、"
             "不产任何价位数字；文字里若含数字一律以程序回填为准）。")
    L.append(f"\n---\n*{result.get('免责')} 数据来源：sector_focus 消息驱动块+规避板块池 + 角色关系表 + "
             "screen_council 全A策略0合议(+附加策略) + as-of 形态。*")
    return "\n".join(L)


def run(date: str, *, data_root: Optional[Path] = None, client=None,
        boards: Optional[list[str]] = None, write: bool = True,
        strategy_top_n: int = STRATEGY_TOP_N, universe_limit: Optional[int] = None,
        extra_screens: bool = False, enable_bypass: bool = False) -> dict:
    """S5 端到端:双路并集合成 → 落 今日选股_<date>.md + .json(data/analysis/<date>/)。返回结果 dict。"""
    from tools.analysis.market_forecast import dataroot
    root = data_root or dataroot.ensure_data_root()
    result = synthesize(date, data_root=root, client=client, boards=boards,
                        strategy_top_n=strategy_top_n, universe_limit=universe_limit,
                        extra_screens=extra_screens, enable_bypass=enable_bypass)
    if write:
        out_dir = dataroot.analysis_dir(root) / date
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / f"今日选股_{date}.md"
        json_path = out_dir / f"今日选股_{date}.json"
        md_path.write_text(render_md(result), encoding="utf-8")
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        result["_产物"] = {"md": str(md_path), "json": str(json_path)}
        logger.info("S5 双路并集选股落盘:%s + %s", md_path, json_path)
    return result
