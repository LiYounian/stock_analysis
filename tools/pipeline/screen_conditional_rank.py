"""策略11 · 指标条件化状态排序 全A Screener(草稿:end-to-end 需数据线在有全A主档+state_pool 的环境跑)。

复用:
  · tools.analysis.conditional_predict:get_pool_index(只读预建索引,**不建池**)
    + conditional_scenarios + direction_view(基线,不倾斜)
  · tools.strategy.conditional_rank.conditional_rank_screen:按基线上涨概率%降序 + 置信度 tiebreak,三 horizon TopK

数据依赖:①`state_pool.parquet` 须**已建好**(数据线在 remote_fetch 后 build_state_pool;本 screener 不建);
②只读 kline(不触基本面、不触发全A serialize)。构造最小 record `{"meta","prediction.指标条件化预测"}` 喂排序函数。

防未来函数:conditional_scenarios 的 as_of 取**当日末 bar 日期**(池样本仅 od_N ≤ as_of);kline 截至当日。

诚实定位:状态排序参考·非 alpha(回测聚合无显著超额;1日弱区分、5/10日近噪声)。**不用**激进版倾斜(direction_view signal=None)。
成本提示(数据线实测):单票 conditional_scenarios ~105ms(全A池百万级 np.percentile),全A ~12min/单进程;可安全多进程压到 ~3-4min。**绝不在 web 层实时算,只读预落盘 view。**

入口:`python -m tools.pipeline.screen_conditional_rank [--codes ...|--universe N] [--date D] [--no-fetch] [--top-k K]`
⚠️ 尚未接入 run_screen_all(不进每日跑),待用户点头后接入。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.analysis import conditional_predict as cpred
from tools.analysis import technical as ta
from tools.collectors import market
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store
from tools.strategy.conditional_rank import conditional_rank_screen

logger = logging.getLogger("pipeline.screen_conditional_rank")

_CFG = THRESHOLDS.get("指标条件化选股", {})
DEFAULT_TOP_K = int(_CFG.get("top_k", 10))
MIN_BARS = 60   # 需足量历史算 state_vector(MA60/BOLL20)+ conditional


def _conditional_block(kdf: pd.DataFrame, tech: dict, idx) -> dict | None:
    """单票基线条件化预测块(不倾斜);失败/异常 → None。与 predict._conditional_block 同口径(sentiment=None)。"""
    try:
        as_of = kdf["date"].iloc[-1]                      # 当日末 bar → 无未来函数
        cond = cpred.conditional_scenarios(kdf, tech, idx, as_of)
        dv = cpred.direction_view(cond)                  # 基线:signal=None,k 不生效
        return {k: {**dv.get(k, {}), **cond[k]} for k in cond}
    except Exception:                                    # noqa: BLE001
        return None


def run_conditional_rank_screen(codes: list[str], as_of: str | None = None,
                                fetch: bool = False,
                                top_k: int = DEFAULT_TOP_K) -> dict:
    """扫描 codes,逐票现算基线条件化预测 → 排序取 TopK,落 view「指标条件化状态排序」。返回 summary。

    fetch=False:只读本地缓存(全A e2e 由数据线跑,缺 K 线不补)。state_pool 缺失 → view 带 note、排行空(降级不崩)。
    """
    if as_of:
        store.set_active_date(as_of)

    # 只读预建索引(不建池);缺失则优雅降级
    try:
        idx = cpred.get_pool_index()
    except Exception as e:                               # noqa: BLE001
        logger.warning("state_pool 不可用(需数据线先 build_state_pool):%s", str(e)[:100])
        idx = None

    records: dict[str, dict] = {}
    scanned = 0
    skip_pre: dict[str, int] = {}

    def _skip(reason: str):
        skip_pre[reason] = skip_pre.get(reason, 0) + 1

    for code in codes:
        try:
            kdf = market.load_kline_recent(code)
        except FileNotFoundError:
            if fetch:
                try:
                    kdf = market.fetch_kline([code]).get(code)
                except Exception:                        # noqa: BLE001
                    kdf = None
            else:
                kdf = None
        except Exception:                                # noqa: BLE001
            kdf = None
        if kdf is None or len(kdf) < MIN_BARS or "close" not in kdf.columns:
            _skip("无K线或历史不足")
            continue
        tech = ta.compute(kdf)
        block = _conditional_block(kdf, tech, idx) if idx is not None else None
        if not block:
            _skip("条件化不可用(池缺失/异常)")
            continue
        scanned += 1
        amounts = kdf["amount"].tolist() if "amount" in kdf.columns else []
        valid_amt = [float(a) for a in amounts[-20:] if isinstance(a, (int, float)) and a == a]
        amount_wan = (sum(valid_amt) / len(valid_amt) / 1e4) if valid_amt else None
        records[str(code)] = {"meta": {"code": str(code)},
                              "snapshot": {"amount_wan": amount_wan},   # 流动性:破同状态格并列的 per-stock 次级键
                              "prediction": {"指标条件化预测": block}}

    out = conditional_rank_screen(records, top_k=top_k)

    view = {
        "as_of": as_of,
        "策略": "指标条件化状态排序(策略11·状态参考·非alpha)",
        "口径": out.get("口径"),
        "扫描数": len(codes),
        "有效样本": out.get("有效样本"),          # {horizon: 参与排序票数}
        "预筛跳过": skip_pre,                      # 无K线/历史不足/条件化不可用
        "排行跳过": out.get("跳过"),               # {horizon:{退回/数据不足/...}}
        "排行": out.get("排行"),                   # {horizon:[{code,上涨概率%,方向,置信度,...}]}
        "top_k": top_k,
        "参数": out.get("参数"),
        "复用": "conditional_predict.conditional_scenarios/direction_view(基线) + strategy.conditional_rank",
        "防未来函数": "conditional as_of=当日末bar日期;池样本仅 od_N≤as_of;kline 截至当日",
        "命名": ("策略11(状态参考,非已验证 alpha):回测聚合无显著超额、方向多中性、"
                 "1日弱区分/5-10日近噪声;仅按当日指标状态相似日的历史上涨概率排序,不作胜率/涨跌承诺"),
        "诚实标注": "⚠️ 状态排序参考,非已验证 alpha;仅作状态参考,不作胜率/涨跌承诺",
    }
    if idx is None:
        view["note"] = "state_pool 缺失:需数据线先 build_state_pool(全A主档);当前排行为空"
    p = store.put_view("指标条件化状态排序", view)
    logger.info("指标条件化状态排序:扫描 %d / 有效(1日) %s / → %s",
                len(codes), (out.get("有效样本") or {}).get("1日"), p)
    return view


def _offline_universe_codes(limit: int | None = None) -> list[str]:
    """离线枚举全A票池:主档代码(不触网)。"""
    codes = sorted(store.list_master_codes())
    return codes[:limit] if limit else codes


def _main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="策略11 指标条件化状态排序 全A扫描")
    ap.add_argument("--universe", type=int, metavar="N", help="全A票池前 N 只(不传=全量)")
    ap.add_argument("--codes", help="逗号分隔的指定代码(优先于 --universe)")
    ap.add_argument("--date", help="运行日期 YYYY-MM-DD(默认今天)")
    ap.add_argument("--no-fetch", action="store_true", help="只读本地缓存,不触网")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help=f"每持有期取前 K(默认 {DEFAULT_TOP_K})")
    a = ap.parse_args(argv)

    as_of = a.date or pd.Timestamp.today().strftime("%Y-%m-%d")
    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    elif a.no_fetch:
        codes = _offline_universe_codes(limit=a.universe)
    else:
        from tools.collectors import universe
        codes = universe.universe_codes(limit=a.universe)
    logger.info("策略11 扫描:%d 只(日期 %s,fetch=%s,top_k=%d)", len(codes), as_of, not a.no_fetch, a.top_k)
    v = run_conditional_rank_screen(codes, as_of=as_of, fetch=not a.no_fetch, top_k=a.top_k)
    logger.info("完成:1日有效 %s", (v.get("有效样本") or {}).get("1日"))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
