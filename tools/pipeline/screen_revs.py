"""REVS 四因子(阶段1 E/V/S)全 A 规则型 Screener —— 三维横截面合成选股(实盘)。

复用 `tools.strategy.revs.revs_screen`:每票预算 E盈利/V估值/S情绪 原始子因子 → 横截面
winsor+zscore→方向→维内均值→维间加权(缺维重归一)→ 取 TopK。设计:
docs/计划/2026-09-09_REVS四因子模型_设计与价值论证.md;回测:REVS四因子_阶段1回测报告_20260909.md。

数据依赖(仿 screen_deduct_quality,全 A、不限池):
  · S 情绪:kline 时序自算(动量N日收益 / 换手N日均值 / 波动率N日std)——纯量价,读尾部窗口(防未来)。
  · E 盈利:financial_report 三大表 → earnings_subfactors(periods, as_of):disclosure_date ≤ as_of 才可见
    的最新报告期的 归母净利增速/营收增速/ROE。
  · V 估值:fundamental 快照 → valuation_subfactors:PE_TTM/PB/总市值(市值分位在横截面里按当日秩算)。
  · snapshot(pct_chg/amount_wan/close/is_st):kline 最后一根 + 财报 name 判 ST。

⚠️ V 用**当日快照**供实盘选股是可以的(现有 fundamental 缓存即最新);但 V 的**历史回测面板**尚缺
   (百度历史序列被采集层丢弃,回填归数据线),故回测报告只验了 E+S。实盘三维可跑,历史 V 待 1b。

防未来函数:S 只读序列尾部;E 只取已披露报告期;实盘 as_of=当日。次新(kline 根数不足)管线侧剔。
缺 E/V/S 某维的票交策略侧"缺维重归一";present 维度 < min_dims 的票不选。诚实降级。

命名:候选(@strategy「REVS四因子」);回测未清晰达标 → **前向观测/候选,未授面板编号、未接每日编排**。⚠️ 非投资建议。

入口:`python -m tools.pipeline.screen_revs [--codes ...|--universe N] [--date D] [--no-fetch] [--top-k K]`。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.collectors import financial as fin
from tools.collectors import fundamental as fd
from tools.collectors import market
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store
from tools.strategy.revs import (
    earnings_subfactors,
    momentum_factor,
    revs_screen,
    turnover_mean,
    valuation_subfactors,
    volatility_factor,
)

logger = logging.getLogger("pipeline.screen_revs")

_CFG = THRESHOLDS.get("REVS四因子", {})
DEFAULT_TOP_K = int(_CFG.get("top_k", 20))
_S_CFG = _CFG.get("S情绪", {})
MOM_N = int(_S_CFG.get("动量窗口", 20))
VOL_N = int(_S_CFG.get("波动窗口", 20))
TURN_N = int(_S_CFG.get("换手窗口", 20))
MIN_LISTING_DAYS = int((_CFG.get("流动性") or {}).get("次新_上市最少天数", 120))


def min_history() -> int:
    """打分所需最少日线根数:次新门槛 与 S 因子最长窗口+1 的较大者。"""
    return max(MIN_LISTING_DAYS, max(MOM_N, VOL_N, TURN_N) + 1)


def _load_kline(code: str, fetch: bool):
    try:
        kdf = market.load_kline_recent(code)
    except (FileNotFoundError, Exception):               # noqa: BLE001
        kdf = None
    if (kdf is None or len(kdf) == 0) and fetch:
        try:
            kdf = market.fetch_kline([code]).get(code)
        except Exception:                                # noqa: BLE001
            kdf = None
    return kdf


def _is_st(name: str | None) -> bool:
    """名称含 ST/*ST/退 → ST 类(剔除;当前快照近似口径,与扣非质量一致)。"""
    if not name:
        return False
    u = str(name).upper()
    return "ST" in u or "退" in name


def _sentiment_subfactors(kdf) -> dict | None:
    """从 kline 时序算 S 情绪原始子因子 {动量, 换手, 波动率}(尾部窗口,防未来)。"""
    if kdf is None or len(kdf) == 0 or "close" not in kdf.columns:
        return None
    closes = kdf["close"].astype(float).tolist()
    turnovers = kdf["turnover"].tolist() if "turnover" in kdf.columns else []
    mom = momentum_factor(closes, n=MOM_N)
    turn = turnover_mean(turnovers, n=TURN_N)
    vol = volatility_factor(closes, n=VOL_N)
    out = {"动量": mom, "换手": turn, "波动率": vol}
    return out if any(v is not None for v in out.values()) else None


def _recent_amount_wan(kdf, n: int = TURN_N) -> float | None:
    """近 n 日成交额均值(万元):amount 缺(近端常 NaN)则用 close×volume 兜底。

    主档近端 amount/turnover 常整片 NaN(盘后闭环回退腾讯 volume-only),单取末行会误判低流动性;
    故对窗口内每根取 amount(有效)否则 close×volume,求有效均值 → /1e4。全窗无有效 → None。
    """
    if kdf is None or len(kdf) == 0 or "close" not in kdf.columns:
        return None
    tail = kdf.tail(n)
    close = tail["close"].astype(float)
    amount = tail["amount"] if "amount" in tail.columns else None
    volume = tail["volume"] if "volume" in tail.columns else None
    vals = []
    for i in range(len(tail)):
        a = float(amount.iloc[i]) if (amount is not None and pd.notna(amount.iloc[i])) else None
        if a is None and volume is not None and pd.notna(volume.iloc[i]):
            a = float(close.iloc[i]) * float(volume.iloc[i])
        if a is not None and a == a:            # 非 NaN
            vals.append(a)
    return (sum(vals) / len(vals)) / 1e4 if vals else None


def _snapshot_from_kline(kdf) -> dict | None:
    """kline 最后一根 → snapshot{pct_chg, amount_wan(近窗均值), close}。无 kline → None(停牌)。"""
    if kdf is None or len(kdf) == 0:
        return None
    last = kdf.iloc[-1]

    def _g(col):
        if col in kdf.columns and pd.notna(last[col]):
            return float(last[col])
        return None

    return {"pct_chg": _g("pct_chg"), "close": _g("close"),
            "amount_wan": _recent_amount_wan(kdf)}


def _build_record(code: str, as_of: str, fetch: bool) -> tuple[dict | None, str | None]:
    """拼装最小 record;返回 (record | None, 跳过原因 | None)。

    record = {meta{code,name}, snapshot{pct_chg,amount_wan,close,is_st},
              REVS{E:{...},V:{...},S:{...}}}。次新在此剔;缺某维交策略侧缺维重归一。
    """
    kdf = _load_kline(code, fetch)
    if kdf is None or len(kdf) == 0:
        return None, "无K线"
    if len(kdf) < min_history():
        return None, "次新(历史不足)"
    snap = _snapshot_from_kline(kdf)
    s_sub = _sentiment_subfactors(kdf)

    # E 盈利(财报 PIT)+ name(判 ST)
    name = None
    e_sub = None
    try:
        raw_fin = fin.load_financial(code)
        name = (raw_fin or {}).get("name")
        e_sub = earnings_subfactors((raw_fin or {}).get("periods") or {}, as_of=as_of)
    except FileNotFoundError:
        pass
    except Exception:                                    # noqa: BLE001
        pass

    # V 估值(当日快照)
    v_sub = None
    try:
        fund = fd.load_fundamental(code)
    except FileNotFoundError:
        fund = None
    except Exception:                                    # noqa: BLE001
        fund = None
    if fund is None and fetch:
        try:
            fund = (fd.fetch_fundamental([code]) or {}).get(code)
        except Exception:                                # noqa: BLE001
            fund = None
    if fund:
        v_sub = valuation_subfactors(fund)

    if snap is not None:
        snap["is_st"] = _is_st(name)
    rec = {
        "meta": {"code": code, "name": name, "n_bars": len(kdf)},
        "snapshot": snap,
        "REVS": {"E": e_sub, "V": v_sub, "S": s_sub},
    }
    return rec, None


def run_revs_screen(codes: list[str], as_of: str | None = None, fetch: bool = True,
                    top_k: int = DEFAULT_TOP_K) -> dict:
    """扫 codes(全 A)→ 建 record(S自算/E财报PIT/V快照)→ 调策略「REVS四因子」→ 落 view。返回 view。

    fetch=True:缺 kline/财报/估值 skip-if-cached 补采;False 只读缓存(离线复算,不触网)。
    ⚠️ V 的历史面板缺(回填归数据线),本管线 V 用当日快照;回测仅验 E+S。诚实降级。
    """
    if as_of:
        store.set_active_date(as_of)
    as_of = as_of or pd.Timestamp.today().strftime("%Y-%m-%d")

    # 财报三大表 skip-if-cached 补采(报告期新鲜度;缺则策略侧缺维重归一)
    if fetch and codes:
        try:
            fin.fetch_financial_missing_or_stale(codes, as_of=as_of)
        except Exception as e:                           # noqa: BLE001
            logger.warning("财报三大表补采失败(降级逐票判): %s", e)

    records: dict[str, dict] = {}
    skip_pre: dict[str, int] = {}
    for code in codes:
        rec, reason = _build_record(code, as_of=as_of, fetch=fetch)
        if rec is None:
            skip_pre[reason] = skip_pre.get(reason, 0) + 1
            continue
        records[code] = rec

    result = revs_screen(records, top_k=top_k)

    skip_all = dict(skip_pre)
    for k, v in (result.get("跳过") or {}).items():
        skip_all[k] = skip_all.get(k, 0) + v

    picks = set(result.get("codes") or [])
    selected = [{
        "code": d["code"],
        "name": (records.get(d["code"], {}).get("meta") or {}).get("name"),
        "组合": ["REVS四因子"],
        "明细": d,
    } for d in result.get("因子明细", []) if d["code"] in picks]

    # V 覆盖率(实盘诊断:多少入选票真拿到了估值维)
    v_present = sum(1 for c in picks
                    if (records.get(c, {}).get("REVS") or {}).get("V") is not None)

    view = {
        "as_of": as_of,
        "策略": "REVS四因子(候选·E/V/S前向观测)",
        "present": len(selected) > 0,
        "口径": ("全 A 三维横截面合成:E盈利(归母净利增速/营收增速/ROE)+ V估值(PE/PB/市值分位·负向)+ "
                 "S情绪(动量/换手/波动率·反转低换手低波),各子因子 winsor+zscore→方向→维内均值,维间 "
                 "%s 加权(缺维重归一),取 Top%d;可交易性门=剔代码头/ST/停牌/涨跌停/低流动性。⚠️非投资建议。"
                 % (result.get("维度权重"), top_k)),
        "扫描数": len(codes),
        "有效样本": result.get("有效样本", len(records)),
        "打分票数": result.get("打分票数"),
        "跳过": skip_all,
        "入选数": len(selected),
        "top_k": top_k,
        "维度权重": result.get("维度权重"),
        "参数": result.get("参数"),
        "入选清单": selected,
        "V估值覆盖": f"{v_present}/{len(picks)}",
        "复用": "tools.strategy.revs.revs_screen(records)",
        "防未来函数": ("S 只读 kline 尾部;E 只取 disclosure_date≤as_of 报告期;次新按 kline 根数(<%d 剔)"
                     % min_history()),
        "命名": ("候选/前向观测,未授面板编号、未接每日编排;回测(E+S)见 "
                 "docs/计划/REVS四因子_阶段1回测报告_20260909.md;V 历史面板待数据线回填后 1b 补验。"),
    }
    if result.get("note"):
        view["note"] = result["note"]
    p = store.put_view("REVS四因子", view)
    logger.info("REVS四因子:扫描 %d / 有效 %d / 打分 %s / 入选 %d / V覆盖 %s → %s",
                len(codes), view["有效样本"], view.get("打分票数"),
                view["入选数"], view["V估值覆盖"], p)
    return view


def _main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="REVS四因子(候选)全A三维横截面选股")
    ap.add_argument("--universe", type=int, metavar="N", help="全A票池前 N 只(不传=全量)")
    ap.add_argument("--codes", help="逗号分隔的指定代码(优先于 --universe)")
    ap.add_argument("--date", help="运行日期 YYYY-MM-DD(默认今天)")
    ap.add_argument("--no-fetch", action="store_true", help="只读本地缓存,不触网")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help=f"取前 K 只(默认 {DEFAULT_TOP_K})")
    a = ap.parse_args(argv)

    as_of = a.date or pd.Timestamp.today().strftime("%Y-%m-%d")
    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    else:
        from tools.collectors import universe
        codes = universe.universe_codes(limit=a.universe)
    logger.info("REVS四因子 扫描:%d 只(日期 %s,fetch=%s,top_k=%d)",
                len(codes), as_of, not a.no_fetch, a.top_k)
    v = run_revs_screen(codes, as_of=as_of, fetch=not a.no_fetch, top_k=a.top_k)
    logger.info("完成:入选 %d / 有效 %d", v["入选数"], v["有效样本"])
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
