"""全市场收盘口径节点单测(`tools.pipeline.market_breadth`)。

⚠️ 文件名说明:`tests/test_market_breadth.py` 早已被**历史广度聚合器**
(`tools.analysis.market_forecast.breadth`)的单测占用,故本节点的单测放这里,别混。

锁语义(为什么这么改 → 断言在锁什么):
  · **单一真源**:节点的 mean_pct 必须等于 features/proxy 用的那个 mean_pct,且两侧都是
    `breadth.cross_section_stats` 算出来的(用 monkeypatch 哨兵证明"没有自己再算一份");
  · 中位数 / 净广度 / 分位 / 涨跌停 的计算正确;
  · 幂等不覆盖、`--force` 覆盖、非交易日跳过;
  · 取样率不足 → **显式** degraded(不悄悄给不可信均值);
  · 单票失败只进 errors、全挂不落文件且退 1;
  · **防未来函数**:`--date` 指向历史日 → 强制走本地 K线,绝不用实时报价。
全部 mock 网络。
"""
import json
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from tools.analysis.market_forecast import breadth as B
from tools.analysis.market_forecast import features as F
from tools.pipeline import market_breadth as M


# ————————————————————————— 夹具:确定性宇宙 + 断网 —————————————————————————
#: pct 精心取值:含涨(2)/跌(2)/平(1),便于手算 median/净广度/分位
UNIVERSE_PCT = {
    "600000": 2.00,     # 涨
    "000001": 0.50,     # 涨
    "300750": 0.00,     # 平
    "688981": -1.50,    # 跌
    "600519": -3.00,    # 跌
}


def _quote(code: str, pct: float, *, sealed_up=False, sealed_down=False) -> dict:
    """按 gtimg 字段口径造一条报价;sealed_* 用于涨跌停判定(close≈high / close≈low)。"""
    prev = 10.0
    price = prev * (1 + pct / 100.0)
    high = price if sealed_up else price * 1.02
    low = price if sealed_down else price * 0.98
    return {"code": code, "name": code, "price": price, "prev_close": prev,
            "open": prev, "high": high, "low": low, "volume": 1000.0,
            "amount_wan": 100.0, "pct_chg": pct, "change": price - prev,
            "vol_ratio": 1.0, "turnover": 1.0, "amplitude": 1.0,
            "quote_time": "20260903150300"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把节点接到 tmp 产出目录 + 假票池 + 假行情源(网络彻底断开)。"""
    monkeypatch.setattr(M, "OUT_ROOT", tmp_path / "breadth")
    monkeypatch.setattr(M.cal, "is_trading_day", lambda d=None, **k: True)

    from tools.store import repo as store
    monkeypatch.setattr(store, "list_master_codes", lambda: sorted(UNIVERSE_PCT))

    state = {"symbols_calls": 0}

    def fake_fetch_symbols(symbols):
        state["symbols_calls"] += 1
        return {s[2:]: _quote(s[2:], UNIVERSE_PCT[s[2:]]) for s in symbols
                if s[2:] in UNIVERSE_PCT}

    monkeypatch.setattr(M.gtimg_quote, "fetch_symbols", fake_fetch_symbols)
    monkeypatch.setattr(M, "fetch_indices",
                        lambda idx=None: ({"000300": {"code": "000300", "alias": "沪深300",
                                                      "price": 4000.0, "pct_chg": 0.10}}, []))
    return state


def _read(tmp_path, date="2026-09-03"):
    return json.loads((tmp_path / "breadth" / f"{date}.json").read_text(encoding="utf-8"))


# ————————————————————————— 1. 单一真源(最重要) —————————————————————————
def _mk_kline(dates, pct):
    close = [10.0]
    for p in pct[1:]:
        close.append(close[-1] * (1 + p / 100.0))
    return pd.DataFrame({"date": pd.to_datetime(dates), "open": close, "high": close,
                         "low": close, "close": close, "volume": [1000.0] * len(dates),
                         "pct_chg": pct})


def test_mean_pct_equals_features_proxy_definition(monkeypatch):
    """节点的 mean_pct == 历史广度的 mean_pct == proxy(全A等权)当日涨幅。

    这条最重要:它锁死"全A等权只有一个定义"。若哪天有人在节点里另写一份均值(比如按成交额
    加权、或漏掉平盘票的分母),本断言立刻失败——大盘预测的 β 基准和盘尾 α 记分的基准就
    不会再各自漂移。
    """
    dates = ["2026-09-02", "2026-09-03"]
    universe = {c: _mk_kline(dates, [0.0, p]) for c, p in UNIVERSE_PCT.items()}
    from tools.store import repo as store
    monkeypatch.setattr(store, "list_master_codes", lambda: list(universe))
    monkeypatch.setattr(store, "get_master_kline", lambda c: universe[c])
    monkeypatch.setattr("tools.analysis.market_forecast.dataroot.ensure_data_root",
                        lambda *a, **k: None)

    hist = B.compute_breadth(codes=list(universe))
    d = pd.Timestamp("2026-09-03")
    hist_mean = float(hist.loc[d, "mean_pct"])

    quotes = {c: _quote(c, p) for c, p in UNIVERSE_PCT.items()}
    agg = M.aggregate(quotes, len(quotes))

    assert agg["mean_pct"] == pytest.approx(hist_mean, abs=1e-12)
    # 并且它就是 proxy 代理指数的当日涨幅(features.build_proxy_index 的 pct_chg 列)
    proxy = F.build_proxy_index(hist)
    proxy_pct = float(proxy.loc[proxy["date"] == d, "pct_chg"].iloc[0])
    assert agg["mean_pct"] == pytest.approx(proxy_pct, abs=1e-12)
    # 也等于手算的等权平均(把定义写死在测试里,防两侧一起被改错)
    assert agg["mean_pct"] == pytest.approx(np.mean(list(UNIVERSE_PCT.values())), abs=1e-12)


def test_node_delegates_mean_to_cross_section_stats(env, monkeypatch):
    """哨兵:节点必须**调用** breadth.cross_section_stats,而不是自己算一遍。"""
    sentinel = {"total": 5, "mean_pct": -42.0, "median_pct": -1.0, "adv": 1, "dec": 3,
                "flat": 1, "net_adv": -0.4, "quantiles": {"P10": -9.0}}
    monkeypatch.setattr(B, "cross_section_stats", lambda *a, **k: sentinel)
    agg = M.aggregate({c: _quote(c, p) for c, p in UNIVERSE_PCT.items()}, 5)
    assert agg["mean_pct"] == -42.0 and agg["net_breadth"] == -0.4
    assert agg["pct_quantiles"] == {"P10": -9.0}


def test_history_side_also_delegates(monkeypatch):
    """哨兵(另一侧):历史广度聚合器也必须调同一函数,否则单一真源只是口号。"""
    dates = ["2026-09-02", "2026-09-03"]
    universe = {c: _mk_kline(dates, [0.0, p]) for c, p in UNIVERSE_PCT.items()}
    from tools.store import repo as store
    monkeypatch.setattr(store, "list_master_codes", lambda: list(universe))
    monkeypatch.setattr(store, "get_master_kline", lambda c: universe[c])
    monkeypatch.setattr("tools.analysis.market_forecast.dataroot.ensure_data_root",
                        lambda *a, **k: None)
    monkeypatch.setattr(B, "cross_section_stats",
                        lambda *a, **k: {"total": 5, "mean_pct": -42.0, "median_pct": -7.0,
                                         "adv": 0, "dec": 0, "flat": 0, "net_adv": 0.0,
                                         "quantiles": {}})
    hist = B.compute_breadth(codes=list(universe))
    assert float(hist["mean_pct"].iloc[-1]) == -42.0
    assert float(hist["median_pct"].iloc[-1]) == -7.0


# ————————————————————————— 2. 聚合口径正确 —————————————————————————
def test_aggregates_median_breadth_quantiles(env, tmp_path):
    assert M.run("1505", date="2026-09-03") == 0
    p = _read(tmp_path)
    assert p["universe_n"] == 5 and p["sampled_n"] == 5 and p["missing_n"] == 0
    assert p["mean_pct"] == pytest.approx((2.0 + 0.5 + 0.0 - 1.5 - 3.0) / 5)
    assert p["median_pct"] == pytest.approx(0.0)          # median(2,.5,0,-1.5,-3)
    assert (p["up_count"], p["down_count"], p["flat_count"]) == (2, 2, 1)
    assert p["net_breadth"] == pytest.approx((2 - 2) / 5)
    vals = sorted(UNIVERSE_PCT.values())
    for q, key in ((0.10, "P10"), (0.25, "P25"), (0.75, "P75"), (0.90, "P90")):
        assert p["pct_quantiles"][key] == pytest.approx(float(np.quantile(vals, q)))
    # 契约字段齐全 + 口径出处写在 meta 里(下游/复盘据此确认同源)
    for k in ("date", "slot", "captured_at", "nominal_at", "drift_seconds", "universe_n",
              "missing_n", "mean_pct", "median_pct", "up_count", "down_count", "flat_count",
              "net_breadth", "pct_quantiles", "limit_up_n", "limit_down_n", "indices",
              "errors", "meta"):
        assert k in p, k
    assert p["meta"]["proxy_definition"] == B.CROSS_SECTION_SOURCE
    assert p["nominal_at"] and p["drift_seconds"] is not None   # 1505 是 HHMM → 有名义时刻


def test_slot_non_hhmm_has_no_nominal(env, tmp_path):
    assert M.run("testrun", date="2026-09-03") == 0
    p = _read(tmp_path)
    assert p["slot"] == "testrun" and p["nominal_at"] is None and p["drift_seconds"] is None


def test_limit_counts_use_shared_heuristic(env, monkeypatch, tmp_path):
    """涨停/跌停走 breadth.is_limit_hit:封板 + 板块限价带;创业板 +10% 封板**不算**涨停。"""
    quotes = {
        "600000": _quote("600000", 10.0, sealed_up=True),    # 主板 +10% 封板 → 涨停
        "300750": _quote("300750", 10.0, sealed_up=True),    # 创业板 +10% 封板 → 非涨停
        "688981": _quote("688981", 20.0, sealed_up=True),    # 科创板 +20% 封板 → 涨停
        "000001": _quote("000001", -10.0, sealed_down=True),  # 主板 −10% 封死 → 跌停
    }
    lu, ld, rule = M.count_limits(quotes)
    assert (lu, ld) == (2, 1)
    assert "封板" in rule and "breadth.is_limit_hit" in rule


# ————————————————————————— 3. 幂等 / force / 非交易日 —————————————————————————
def test_idempotent_no_overwrite(env, tmp_path):
    assert M.run("1505", date="2026-09-03") == 0
    first = _read(tmp_path)["captured_at"]
    calls = env["symbols_calls"]
    assert M.run("1505", date="2026-09-03") == 0          # 第二次:跳过
    assert _read(tmp_path)["captured_at"] == first
    assert env["symbols_calls"] == calls                  # 没再触网


def test_force_overwrites(env, tmp_path):
    assert M.run("1505", date="2026-09-03") == 0
    calls = env["symbols_calls"]
    assert M.run("1505", date="2026-09-03", force=True) == 0
    assert env["symbols_calls"] > calls                   # 重抓了


def test_non_trading_day_skips(env, tmp_path, monkeypatch):
    monkeypatch.setattr(M.cal, "is_trading_day", lambda d=None, **k: False)
    assert M.run("1505", date="2026-09-05") == 0
    assert not (tmp_path / "breadth" / "2026-09-05.json").exists()
    assert env["symbols_calls"] == 0


# ————————————————————————— 4. 显式降级 / 容错 —————————————————————————
def test_low_coverage_marks_degraded_explicitly(env, tmp_path, monkeypatch):
    """只取到 2/5(40%)→ 必须 degraded=true 且给出理由,而不是悄悄给一个不可信的均值。"""
    monkeypatch.setattr(M.gtimg_quote, "fetch_symbols",
                        lambda symbols: {"600000": _quote("600000", 2.0),
                                         "000001": _quote("000001", 0.5)})
    assert M.run("1505", date="2026-09-03") == 0
    p = _read(tmp_path)
    assert p["sampled_n"] == 2 and p["missing_n"] == 3
    assert p["coverage"] == pytest.approx(0.4)
    assert p["degraded"] is True
    assert any("取样率" in r for r in p["degrade_reasons"])
    assert len([e for e in p["errors"] if e["scope"] == "code"]) == 3


def test_single_code_failure_only_goes_to_errors(env, tmp_path, monkeypatch):
    """单票缺失(停牌)只进 errors,其余票照常聚合、退 0(取样率 4/5=80%,这里把阈值放到 50%
    以隔离"单票容错"这一条语义;取样率不足的降级另有专测)。"""
    missing = "600519"
    monkeypatch.setattr(M.gtimg_quote, "fetch_symbols",
                        lambda symbols: {s[2:]: _quote(s[2:], UNIVERSE_PCT[s[2:]])
                                         for s in symbols if s[2:] != missing})
    assert M.run("1505", date="2026-09-03", min_coverage=0.5) == 0
    p = _read(tmp_path)
    assert p["sampled_n"] == 4 and p["degraded"] is False
    errs = [e for e in p["errors"] if e["scope"] == "code"]
    assert [e["code"] for e in errs] == [missing]
    # 分母 = 实际取到的只数(与历史聚合 listed 口径一致)
    assert p["mean_pct"] == pytest.approx(
        np.mean([v for c, v in UNIVERSE_PCT.items() if c != missing]))


def test_all_failed_writes_nothing_and_exits_1(env, tmp_path, monkeypatch):
    def boom(symbols):
        raise ConnectionError("源方挂了")
    monkeypatch.setattr(M.gtimg_quote, "fetch_symbols", boom)
    assert M.run("1505", date="2026-09-03") == 1
    assert not (tmp_path / "breadth" / "2026-09-03.json").exists()


def test_pre_close_capture_is_flagged(env, tmp_path, monkeypatch):
    """抓取时点早于 15:00 → 显式标"这是盘中截面不是收盘口径"(防把盘中当收盘用)。"""
    agg = {"universe_n": 5, "sampled_n": 5, "missing_n": 0, "coverage": 1.0,
           "pct_derived_n": 0}
    at = datetime(2026, 9, 3, 11, 0).astimezone()
    reasons = M._degrade_reasons(agg, at, "realtime", 0.9)
    assert any("早于收盘" in r for r in reasons)
    at2 = datetime(2026, 9, 3, 15, 5).astimezone()
    assert M._degrade_reasons(agg, at2, "realtime", 0.9) == []


# ————————————————————————— 5. 防未来函数 —————————————————————————
def test_history_backfill_never_uses_realtime_quotes(env, tmp_path, monkeypatch):
    """`--date` 指向历史日 → 走本地 K线(mode=kline),**一次都不许**碰实时报价。"""
    dates = ["2026-08-31", "2026-09-01"]
    universe = {c: _mk_kline(dates, [0.0, p]) for c, p in UNIVERSE_PCT.items()}
    from tools.store import repo as store
    monkeypatch.setattr(store, "get_master_kline", lambda c: universe[c])
    monkeypatch.setattr(M, "_index_from_kline", lambda d: ({}, []))

    assert M.run("1505", date="2026-09-01") == 0
    p = _read(tmp_path, "2026-09-01")
    assert env["symbols_calls"] == 0                       # 没有拿今天的实时价冒充那天
    assert p["meta"]["mode"] == "kline" and p["meta"]["source"] == M.SOURCE_KLINE
    assert p["mean_pct"] == pytest.approx(np.mean(list(UNIVERSE_PCT.values())))
    # captured_at 如实是"跑这次补跑"的时刻(不伪装成那天收盘);date 才是口径日
    assert p["captured_at"][:10] == datetime.now().strftime("%Y-%m-%d")
    assert p["date"] == "2026-09-01"


def test_kline_backfill_missing_day_goes_to_errors(env, monkeypatch):
    """补跑日某票无 K线行(停牌/未上市)→ 只进 errors,不静默补 0。"""
    dates = ["2026-08-31", "2026-09-01"]
    universe = {c: _mk_kline(dates, [0.0, p]) for c, p in UNIVERSE_PCT.items()}
    from tools.store import repo as store
    monkeypatch.setattr(store, "get_master_kline", lambda c: universe[c])
    quotes, errors = M.fetch_universe_kline(sorted(UNIVERSE_PCT), "2026-09-02")
    assert quotes == {} and len(errors) == len(UNIVERSE_PCT)
    assert all("该日无K线" in e["reason"] for e in errors)


# ————————————————————————— 6. 票池口径 —————————————————————————
def test_票池默认含北交所_与历史compute_breadth完全一致(monkeypatch):
    """**默认必须含北交所**——这是"单一真源"的必要条件。

    同一个 `cross_section_stats` 函数若喂两个不同票池,等权基准仍是两条互相漂移的序列,
    α 记分与大盘预测就对不上账。历史侧 `compute_breadth` 的票池含北交所,本节点必须一致。
    实测差异不是小数:09-03 含北交所 mean_pct **-0.422%** vs 排除 **-0.305%**,差 0.117pp,
    足以系统性影响每只票的 α 记分(北交所 338 只 = 6.1% 票池)。
    首版曾默认排除,理由是 gtimg 前缀推断把 920xxx 判成 sh 取不到数——**那是 bug 不是口径**,
    已在 `gtimg_quote.market_prefix` 修掉(见 tests/test_market.py 的 920 段路由锁)。
    """
    from tools.store import repo as store
    monkeypatch.setattr(store, "list_master_codes",
                        lambda: ["600000", "300750", "920002", "830799"])
    codes, meta = M.resolve_universe()
    assert len(codes) == 4 and meta["include_bj"] is True and meta["n"] == 4, \
        "默认排除北交所会让 α 基准与历史 proxy 差约 0.1pp 量级"
    assert meta["bj_n"] == 2
    # 北交所必须走 bj 前缀才能取到数据
    assert "bj920002" in M._symbols_for(codes)
    # --exclude-bj 仅保留给"对账历史上排除北交所口径的旧数字"这一用途
    codes2, meta2 = M.resolve_universe(include_bj=False)
    assert codes2 == ["600000", "300750"] and meta2["include_bj"] is False
