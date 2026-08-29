"""筹码分布采集(本地推演,无新增数据源)。

借鉴 a-stock-data §4.6 CYQ:不额外拉接口,纯用主档 K线的
OHLC + 换手率(turnover)推演每日「成本分布」,派生一组择时/选股因子:
  - 获利比例    最新收盘价上方(成本 ≤ 收盘)的筹码占比,0~1。越高越多人浮盈,抛压潜在越大。
  - 平均成本    全体筹码的加权平均持仓成本(元)。
  - 成本区间    90%/70% 筹码所处的价格上下沿(元)。
  - 集中度      90% 筹码集中度 =(高沿-低沿)/(高沿+低沿),越小越集中(筹码越锁定)。

算法(三角分布 + 换手衰减,业内通行的「衰减型」筹码模型):
  逐日把当日成交视为**新换手筹码**,按三角形分布铺在 [low, high] 区间(峰在当日均价);
  历史筹码整体乘 (1-换手率) 衰减(等比例「被换手掉」),二者叠加即当日成本分布。
  首日以「全部流通盘」为初始存量播种(不能从零起步,否则前段占比虚高)——见 a-stock-data 注记。

数据前提:换手率来自主档 K线的 `turnover` 列(baostock 源含;腾讯裸 K线该列为空)。
  主档缺换手率(腾讯源)且为 A股 → **fetch_chip 自动用一次 baostock 会话补换手率**(换手率与
  复权口径无关,按交易日对齐即可),使筹码不依赖主档来源。港股无换手口径 → 跳过(不静默伪造)。
落盘:走 store 层(kind="chip",json 快照:最新一日派生值),旁记 meta.source="local(kline)"。
契约对齐 fundflow/fundamental 的 summarize 风格(供 factor.py 取数)。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from tools.collectors import market
from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.chip")

_SOURCE = "local(kline)"      # 本地推演,不触网
_GRID = 400                   # 价格轴离散格数(窗口高低点之间),400 格对多数票精度足够
_WINDOW = 250                 # 参与推演的近史根数(约一年;更久的存量已被衰减稀释,无需全历史)


def _avg_price(row: pd.Series) -> float:
    """当日均价:优先成交额/成交量(VWAP),缺失回退 (高+低+收)/3。"""
    amt, vol = row.get("amount"), row.get("volume")
    if pd.notna(amt) and pd.notna(vol) and vol:
        vwap = float(amt) / float(vol)
        # baostock volume 单位为股、amount 为元,VWAP 落在 [low, high] 内才可信,否则回退
        if row["low"] <= vwap <= row["high"]:
            return vwap
    return (float(row["high"]) + float(row["low"]) + float(row["close"])) / 3.0


def compute_distribution(df: pd.DataFrame, window: int = _WINDOW) -> tuple[np.ndarray, np.ndarray] | None:
    """从 K线推演最新一日的筹码成本分布。

    返回 (prices, chips):prices 为价格格点,chips 为对应筹码质量(已归一,和≈1)。
    换手率整列缺失/窗口过短 → None(上层降级,不伪造)。
    """
    if df is None or len(df) < 20 or "turnover" not in df.columns:
        return None
    df = df.tail(window).reset_index(drop=True)
    turn = pd.to_numeric(df["turnover"], errors="coerce")
    if turn.notna().sum() < len(df) * 0.5:        # 半数以上换手缺失 → 不可信
        return None

    lo, hi = float(df["low"].min()), float(df["high"].max())
    if not (hi > lo):
        return None
    prices = np.linspace(lo, hi, _GRID)
    chips = np.zeros(_GRID, dtype=float)
    seeded = False

    for idx, row in df.iterrows():
        h, l = float(row["high"]), float(row["low"])
        if not (h >= l):
            continue
        peak = _avg_price(row)
        # 当日换手率(小数);缺失按 0 处理(等价当日无新增筹码,仅结转)
        t = turn.loc[idx]
        tr = 0.0 if pd.isna(t) else min(max(float(t) / 100.0, 0.0), 1.0)

        # 当日新增筹码在 [l, h] 上的三角形权重(峰在均价 peak)
        mask = (prices >= l) & (prices <= h)
        w = np.zeros(_GRID, dtype=float)
        if mask.any():
            seg = prices[mask]
            left = np.clip((seg - l) / max(peak - l, 1e-9), 0, None) * (seg <= peak)
            right = np.clip((h - seg) / max(h - peak, 1e-9), 0, None) * (seg > peak)
            tri = left + right
            if tri.sum() > 0:
                w[mask] = tri / tri.sum()

        if not seeded:
            chips = w.copy()          # 首日:全部流通盘按当日分布播种(存量=1)
            seeded = True
            continue
        chips = chips * (1.0 - tr) + w * tr   # 历史衰减 + 当日新增

    s = chips.sum()
    if s <= 0:
        return None
    return prices, chips / s


def _cost_range(prices: np.ndarray, chips: np.ndarray, ratio: float) -> tuple[float, float]:
    """含 ratio(如 0.9)筹码的价格上下沿:按价格排序取中间 ratio 质量的最低/最高价。"""
    order = np.argsort(prices)
    p, c = prices[order], chips[order]
    cdf = np.cumsum(c)
    lo_q, hi_q = (1 - ratio) / 2, 1 - (1 - ratio) / 2
    lo = float(p[np.searchsorted(cdf, lo_q)])
    hi = float(p[min(np.searchsorted(cdf, hi_q), len(p) - 1)])
    return lo, hi


def summarize(df: pd.DataFrame) -> dict:
    """派生最新一日筹码因子。无法推演 → 全 None(缺失,交上层降级)。"""
    null = {"获利比例": None, "平均成本": None, "成本区间下沿": None,
            "成本区间上沿": None, "集中度90": None, "现价": None}
    dist = compute_distribution(df)
    if dist is None:
        return null
    prices, chips = dist
    close = float(df["close"].iloc[-1])
    win = float(chips[prices <= close].sum())              # 获利盘(成本≤现价)
    avg_cost = float(np.sum(prices * chips))
    lo90, hi90 = _cost_range(prices, chips, 0.90)
    conc = (hi90 - lo90) / (hi90 + lo90) if (hi90 + lo90) else None
    return {
        "获利比例": round(win, 4),
        "平均成本": round(avg_cost, 3),
        "成本区间下沿": round(lo90, 3),
        "成本区间上沿": round(hi90, 3),
        "集中度90": round(conc, 4) if conc is not None else None,
        "现价": round(close, 3),
    }


def fetch_one(code: str) -> dict:
    """读单票主档 K线并推演筹码因子(不触网)。K线缺失抛 FileNotFoundError。

    仅纯本地路径:主档无换手率(如腾讯源)→ 返回全 None。批量入口 fetch_chip
    会对这类票走 baostock 换手率兜底,单独调用本函数不触网、不兜底。
    """
    df = market.load_kline(code)
    return summarize(df)


def _backfill_turnover(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """用 baostock 的换手率序列补齐主档缺失的 turnover(按日期对齐)。**须在 baostock 会话内调用。**

    换手率与复权口径无关,按交易日 map 即可;只取尾部 _WINDOW 天(推演所需)减小拉取量。
    """
    from tools.collectors import baostock_src as bsrc
    tail = df.tail(_WINDOW)
    start = pd.to_datetime(tail["date"]).min().strftime("%Y-%m-%d")
    end = pd.to_datetime(tail["date"]).max().strftime("%Y-%m-%d")
    bdf = bsrc.fetch_one(code, start, end, "qfq")            # 含 turnover
    tmap = dict(zip(pd.to_datetime(bdf["date"]), pd.to_numeric(bdf["turnover"], errors="coerce")))
    out = df.copy()
    out["turnover"] = pd.to_datetime(out["date"]).map(tmap)
    return out


def _has_turnover(df: pd.DataFrame) -> bool:
    """主档是否已带可用换手率(半数以上非空)。"""
    return (df is not None and "turnover" in getattr(df, "columns", [])
            and pd.to_numeric(df["turnover"], errors="coerce").notna().sum() >= len(df) * 0.5)


def fetch_chip(codes: list[str]) -> dict[str, dict]:
    """批量推演筹码分布并落盘。

    两段式:①主档已含换手率(如 baostock 源)→ 纯本地推演,无网络;
    ②主档缺换手率(如腾讯源)且为 A股 → 收集起来,开一次 baostock 会话补换手率再推演。
    港股无换手口径 → 跳过。单票 K线缺失/兜底后仍不可用 → 记 log 跳过,不中断整批。
    """
    from tools.config import stock_pool

    settings.ensure_dirs()
    out: dict[str, dict] = {}
    failed: list[str] = []
    need_bs: list[tuple[str, pd.DataFrame]] = []      # 待 baostock 补换手率的 (code, df)
    n = len(codes)
    # —— 第一段:本地优先 ——
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] 筹码 %s 推演...", i, n, code)
        try:
            df = market.load_kline(code)
        except FileNotFoundError:
            failed.append(code)
            logger.error("筹码 %s 失败: 无本地 K线(先采 K线)", code)
            continue
        if _has_turnover(df):
            _emit(code, summarize(df), out, failed)
        elif stock_pool.is_hk(code):
            failed.append(code)
            logger.warning("筹码 %s:港股无换手口径,跳过", code)
        else:
            need_bs.append((code, df))                # 腾讯源等,留待 baostock 补换手率

    # —— 第二段:一次会话补换手率 ——
    if need_bs:
        logger.info("筹码:%d 只主档缺换手率,走 baostock 兜底补齐...", len(need_bs))
        try:
            from tools.collectors.baostock_src import session
            with session():
                for code, df in need_bs:
                    try:
                        _emit(code, summarize(_backfill_turnover(df, code)), out, failed)
                    except Exception as e:            # 单票兜底失败只跳过该票
                        failed.append(code)
                        logger.warning("筹码 %s baostock 兜底失败: %s", code, e)
        except Exception as e:                        # 会话整体失败 → 这批全降级
            failed.extend(c for c, _ in need_bs)
            logger.warning("baostock 会话失败,筹码兜底整体降级: %s", e)

    if failed:
        logger.warning("筹码推演失败/跳过(%d): %s", len(failed), failed)
    return out


def _emit(code: str, rec: dict, out: dict, failed: list) -> None:
    """落盘一条筹码结果;换手率不可用(全 None)→ 记 failed 不写空快照。"""
    if rec.get("获利比例") is None:
        failed.append(code)
        logger.warning("筹码 %s:换手率不可用,跳过", code)
        return
    store.put_raw("chip", code, rec, meta={"source": _SOURCE})
    out[code] = rec
    logger.info("筹码 %s:获利比例 %.1f%% 平均成本 %.2f",
                code, rec["获利比例"] * 100, rec["平均成本"])


def summarize_asof(df: pd.DataFrame, as_of: str) -> dict:
    """按 as_of point-in-time 推演筹码因子:**只用日期 ≤ as_of 的 K线 bar**。

    去历史重建前视偏差(回填 panel / 多因子回测):筹码纯本地推演,天然可按任一
    as_of 用「当时及之前」的 bar 重算 → 未来 bar 绝不参与。无 date 列(无法截断)
    则退化为对整段 df 推演(与 summarize 一致,当日路径 df 尾部即今日,无差)。
    """
    if df is None or "date" not in getattr(df, "columns", []):
        return summarize(df)
    d = pd.to_datetime(as_of)
    sub = df[pd.to_datetime(df["date"]) <= d]
    return summarize(sub)


def load_chip(code: str, as_of: str | None = None) -> dict:
    """读单票筹码因子。

    - as_of=None(当日/存在性检查路径):读本地缓存快照(store.get_raw,当日 latest)。
    - as_of 指定(历史重建/回测路径):**point-in-time 重算**——读主档全历史 K线,
      仅用 ≤as_of 的 bar 推演(见 summarize_asof),杜绝把未来值写进历史。
      ≤as_of 数据不足/缺换手率而无法推演 → 抛 FileNotFoundError(交上层降级为 None,
      使 provenance.chip 如实为 False,不伪造「有筹码」)。
    缺 K线/缓存一律抛 FileNotFoundError(与其余 load_* 一致)。
    """
    if as_of is None:
        return store.get_raw("chip", code)
    df = market.load_kline(code)                 # 主档全历史(含 date/turnover)
    rec = summarize_asof(df, as_of)
    if rec.get("获利比例") is None:
        raise FileNotFoundError(
            f"{code} as_of={as_of} 无法 point-in-time 推演筹码(≤as_of 缺换手率/数据不足)")
    return rec
