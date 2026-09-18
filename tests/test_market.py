"""market.py 单测(mock 各数据源,不触网)。

锁语义:代码前缀映射、多源 fallback、全源失败抛错(不静默返空)、
列名归一 + pct_chg 补算、落盘/读盘往返一致。
"""
import pandas as pd
import pytest

from tools.collectors import market
from tools.store import repo as store


def _sample_std():
    """标准英文列样本(腾讯源形态),乱序。"""
    return pd.DataFrame({
        "date": ["2026-01-03", "2026-01-02"],
        "open": [10.0, 9.5], "close": [10.5, 10.0],
        "high": [10.8, 9.9], "low": [9.4, 9.3],
        "volume": [1000.0, 800.0], "amount": [1e6, 8e5], "turnover": [0.08, 0.06],
    })


def test_market_prefix():
    assert market.market_prefix("600667") == "sh600667"
    assert market.market_prefix("688249") == "sh688249"
    assert market.market_prefix("000021") == "sz000021"
    assert market.market_prefix("300124") == "sz300124"
    assert market.market_prefix("002156") == "sz002156"


def test_normalize_sort_and_pctchg():
    """列归一、按日期升序、pct_chg 自算(首行 NaN)。"""
    df = market._normalize(_sample_std())
    assert list(df.columns) == market._STD_COLS
    assert df["date"].is_monotonic_increasing
    assert df.iloc[0]["close"] == 10.0            # 升序后 1-02 在前
    # 1-03 相对 1-02 涨 5%
    assert round(df.iloc[1]["pct_chg"], 2) == 5.0


def test_fetch_one_fallback(monkeypatch):
    """主源失败 → 自动切下一个源成功。"""
    def boom(*a, **k):
        raise ConnectionError("被墙")
    monkeypatch.setitem(market._FETCHERS, "tencent", boom)
    monkeypatch.setitem(market._FETCHERS, "sina", lambda *a, **k: _sample_std())
    df = market.fetch_one("000021", "20260101", "20260105", "qfq")
    assert len(df) == 2


def test_watchdog_slow_source_skipped_不拖死整轮(monkeypatch):
    """A1 单票看门狗:某源 read 挂起(远超硬超时仍不返回)→ 硬超时即跳过换下一个源,
    整轮采集不被这只票拖死(RC-2 根治,诊断 §6.1)。锁"超时即跳过、快速返回"语义。
    """
    import threading
    import time

    monkeypatch.setenv("FETCH_SRC_TIMEOUT", "0.3")   # 缩短硬超时,测试内秒级验证
    started = threading.Event()

    def hang(*a, **k):
        started.set()
        time.sleep(30)               # 模拟 sina/eastmoney read 无限挂起(远超 0.3s 上界)
        return _sample_std()

    monkeypatch.setitem(market._FETCHERS, "tencent", hang)
    monkeypatch.setitem(market._FETCHERS, "sina", lambda *a, **k: _sample_std())

    t0 = time.monotonic()
    df = market.fetch_one("000021", "20260101", "20260105", "qfq")
    elapsed = time.monotonic() - t0

    assert started.is_set()          # 挂起源确实被调用过
    assert len(df) == 2              # 命中 fallback sina,不返空
    assert elapsed < 5               # 关键:0.3s 硬超时即跳过,没被 30s sleep 拖死


def test_watchdog_timeout_from_env_default(monkeypatch):
    """看门狗超时可由 FETCH_SRC_TIMEOUT 覆盖,缺省 40s(覆盖 tencent 内部 15s×2 最坏串行)。"""
    monkeypatch.delenv("FETCH_SRC_TIMEOUT", raising=False)
    assert market._src_timeout() == 40.0
    monkeypatch.setenv("FETCH_SRC_TIMEOUT", "12.5")
    assert market._src_timeout() == 12.5


def test_fetch_one_all_fail_raises(monkeypatch):
    """全源失败必须抛错,不返回空 df(约法第 5 条)。"""
    def boom(*a, **k):
        raise ConnectionError("挂了")
    for s in market._FETCHERS:
        monkeypatch.setitem(market._FETCHERS, s, boom)
    with pytest.raises(ConnectionError):
        market.fetch_one("000021", "20260101", "20260105", "qfq")


def test_fetch_kline_meta_records_fallback_source(monkeypatch, tmp_path):
    """主源失败 → 落盘 meta.source 记的是实际命中的 fallback 源(sina)。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)

    def boom(*a, **k):
        raise ConnectionError("被墙")
    monkeypatch.setitem(market._FETCHERS, "tencent", boom)
    monkeypatch.setitem(market._FETCHERS, "sina", lambda *a, **k: _sample_std())

    market.fetch_kline(["000021"], start="20260101", end="20260105")
    assert store.get_raw_meta("kline", "000021")["source"] == "sina"


def test_fetch_and_load_roundtrip(monkeypatch, tmp_path):
    """落盘 → 读盘往返一致(经 store);缓存缺失抛错;meta 记命中源。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    # 隔离滚动主档:load_kline 现在优先读主档,若不隔离会读到真实 data/master/(本地拉取灌过)→ 返回 333 根而非本测的 2 根。
    monkeypatch.setattr(store, "_MASTER_DIR", tmp_path / "master")
    monkeypatch.setitem(market._FETCHERS, "tencent", lambda *a, **k: _sample_std())

    out = market.fetch_kline(["000021"], start="20260101", end="20260105")
    assert "000021" in out
    loaded = market.load_kline("000021")
    assert len(loaded) == 2
    assert loaded["date"].is_monotonic_increasing

    # 落盘经 store,旁写 meta 记实际命中源(此处主源腾讯命中)
    m = store.get_raw_meta("kline", "000021")
    assert m["source"] == "tencent"

    with pytest.raises(FileNotFoundError):
        market.load_kline("999999")


# ———— 新增:腾讯 fqkline 端点(_fetch_tencent 改版)————
class _FakeResp:
    def __init__(self, payload):
        self._p = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self._p


def _fq_payload(sym, rows):
    """构造 fqkline 响应:data[sym]['qfqday'] = [[date,open,close,high,low,vol手], ...]。"""
    return {"data": {sym: {"qfqday": rows}}}


def test_fetch_tencent_parses_and_scales_volume(monkeypatch):
    """fqkline 端点:每行 [date,open,close,high,low,vol手] → 归一列 + volume×100(手→股);
    成交额/换手率缺 → _normalize 补 NA;含当日最新 bar。"""
    rows = [
        ["2026-08-07", "919.87", "919.87", "925.0", "900.0", "500000"],
        ["2026-08-10", "917.99", "864.58", "919.80", "835.00", "525921"],
        ["2026-08-11", "880.14", "886.96", "898.48", "850.40", "339737"],
    ]
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(_fq_payload("sz300308", rows)))
    df = market._fetch_tencent("300308", "20250329", "20260811", "qfq")
    assert list(df.columns[:5]) == ["date", "open", "close", "high", "low"]
    # 单位:手→股 ×100
    last = df.iloc[-1]
    assert last["date"] == "2026-08-11" and float(last["volume"]) == 339737 * 100
    # 经 _normalize 后:标准列齐、amount/turnover 为 NA、pct_chg 补算
    norm = market._normalize(df)
    assert list(norm.columns) == market._STD_COLS
    assert pd.isna(norm.iloc[-1]["amount"]) and pd.isna(norm.iloc[-1]["turnover"])
    assert str(norm.iloc[-1]["date"].date()) == "2026-08-11"   # 含当日最新


def test_fetch_tencent_filters_by_start(monkeypatch):
    """按 start 裁剪:早于 start 的 bar 丢弃(端点固定拉最近 N 根,需裁到请求区间)。"""
    rows = [
        ["2025-01-02", "10", "10.5", "10.8", "9.9", "100"],
        ["2026-08-10", "11", "11.2", "11.5", "10.8", "200"],
        ["2026-08-11", "11.2", "11.4", "11.6", "11.0", "300"],
    ]
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(_fq_payload("sz300001", rows)))
    df = market._fetch_tencent("300001", "20260801", "20260811", "qfq")
    assert list(df["date"]) == ["2026-08-10", "2026-08-11"]   # 2025 那根被裁掉


# ————————————————————————————————————————————————
# 北交所 920 段路由(2026-09-03)
#
# 为什么有这组断言:北交所现行代码段是 920xxx(主档 338 只 = 6.1% 票池,且主档里北交所**全部**在这一段,
# 没有 43/83/87 段)。若按"9 开头→沪市"路由,这 338 只会**整段静默取不到数据**——实测 gtimg
# `sh920002` 返回空、`bj920002` 正常。而 900xxx 是沪市B股仍归沪市,所以判据必须是前缀长度精确匹配、
# 不能只看首位。这个 bug 曾让全市场收盘口径节点的票池比历史 compute_breadth 少 6%,导致"同一个
# 等权函数、两个不同票池"——等权基准仍是两条互相漂移的序列。
# ————————————————————————————————————————————————

def test_北交所920段路由到bj_而900段沪B仍归sh():
    from tools.collectors import gtimg_quote
    from tools.collectors import financial

    # ① 实时报价前缀
    assert gtimg_quote.market_prefix("920002") == "bj", "920 段是北交所,判成 sh 会整段取不到数"
    assert gtimg_quote.market_prefix("900901") == "sh", "900 段是沪市B股,不能被 920 规则带走"
    assert gtimg_quote.market_prefix("430047") == "bj"
    assert gtimg_quote.market_prefix("831010") == "bj"
    assert gtimg_quote.market_prefix("600000") == "sh"
    assert gtimg_quote.market_prefix("300001") == "sz"

    # ② K线/新浪腾讯带前缀符号
    assert market.market_prefix("920002") == "bj920002"
    assert market.market_prefix("900901") == "sh900901"
    assert market.market_prefix("600000") == "sh600000"

    # ③ 东财财报接口符号(北交所必须 BJ,否则落到 SZ 兜底)
    assert financial._em_symbol("920002") == "BJ920002"
    assert financial._em_symbol("430047") == "BJ430047"
    assert financial._em_symbol("600000") == "SH600000"
    assert financial._em_symbol("000001") == "SZ000001"


# ———————————— P1-1/P3-1:腾讯批量 spot 源 + 量额归一断言 ————————————
def _fake_quotes(codes):
    """gtimg_quote.fetch_quotes 形态(**已归一**):close=10 元、volume=1e5 股、amount=100 万元
    → amount/(close×volume股)=1e6/(10×1e5)=1.0(量额自洽)。gtimg_quote 层已把"手→股"、
    科创板保持"股",故此处 fetch_spot_all_tencent 不再 ×100。"""
    return {c: {"name": "T" + c, "price": 10.0, "prev_close": 9.5, "open": 9.6,
                "high": 10.8, "low": 9.4, "volume": 100000.0, "amount_wan": 100.0,
                "pct_chg": 5.0, "change": 0.5, "vol_ratio": 1.2, "turnover": 3.5,
                "amplitude": 2.0, "quote_time": "20260909150000"} for c in codes}


def test_fetch_spot_all_tencent_单位归一(monkeypatch):
    """腾讯快照 → 主档口径:volume 手→×100 股、amount 万元→×1e4 元、turnover 百分数原样。"""
    codes = [f"{600000 + i:06d}" for i in range(25)]
    monkeypatch.setattr("tools.collectors.gtimg_quote.fetch_quotes", lambda cs: _fake_quotes(cs))
    df = market.fetch_spot_all_tencent(codes)
    assert set(["code", "open", "high", "low", "close", "volume", "amount",
                "turnover", "pct_chg"]).issubset(df.columns)
    row = df.set_index("code").loc["600000"]
    assert row["close"] == 10.0                 # 收盘后现价=收盘价
    assert row["volume"] == 100000.0            # gtimg_quote 已归一「股」,spot 不再 ×100
    assert row["amount"] == 100.0 * 1e4         # 万元 → 元
    assert row["turnover"] == 3.5               # 百分数原样(gtimg_quote 登记 PERCENT)


def test_spot_量额断言_捕获volume未归股(monkeypatch):
    """P3-1:volume 误留"手"(小 100×)→ amount/(close×volume) 中位≈100 → 硬断言必抛。"""
    n = 25
    bad = pd.DataFrame({
        "code": [f"{i:06d}" for i in range(n)],
        "open": [9.6] * n, "high": [10.8] * n, "low": [9.4] * n, "close": [10.0] * n,
        "volume": [1000.0] * n,       # 误当"手"未 ×100
        "amount": [1e6] * n,          # 元(正确)
        "turnover": [3.5] * n, "pct_chg": [5.0] * n,
    })
    with pytest.raises(ValueError, match="量额口径异常"):
        market._assert_spot_amount_volume(bad, "test", hard=True)
    # 正确口径(volume 股)→ 不抛;soft 模式即使错也不抛(不阻断兜底)
    good = bad.copy(); good["volume"] = 1e5
    market._assert_spot_amount_volume(good, "test", hard=True)
    market._assert_spot_amount_volume(bad, "test", hard=False)   # 仅告警,不抛


# ————————————————————————————————————————————————
# H2:科创板 688/689 成交量 100× 高估修复
#   - gtimg 采集层量额自证归一(手/股按板块与 amount 反推)
#   - fqkline 端点按板块条件化 ×100
#   - 量额断言分板块,少数派(688 占 ~11%)不再被全帧中位数掩盖
# ————————————————————————————————————————————————
def _gtimg_line(code, price, vol_raw, amt_wan, *, name="测试"):
    """构造一条 gtimg `v_xxx="..."` 原始行(≥88 段,字段下标同 parse_line)。"""
    parts = ["0"] * 88
    parts[1] = name
    parts[2] = code
    parts[3] = str(price)          # 现价
    parts[4] = str(price)          # 昨收
    parts[5] = str(price)          # 今开
    parts[6] = str(vol_raw)        # 成交量(源方:688/689=股、其余=手)
    parts[30] = "20260917150000"   # 行情时刻
    parts[31] = "0"; parts[32] = "1.0"
    parts[33] = str(price); parts[34] = str(price)
    parts[37] = str(amt_wan)       # 成交额(万元)
    parts[38] = "2.0"; parts[43] = "1.0"; parts[49] = "1.0"
    return f'v_x{code}="' + "~".join(parts) + '";'


def test_gtimg_parse_line_量额自证_非科创板手转股():
    """非科创板:源方 volume 是"手",amount 自证比值≈0.01 → ×100 归一到"股"。"""
    from tools.collectors import gtimg_quote
    # 600519:price 1266.98,原始 volume 17554 手,amount 221734 万元
    line = _gtimg_line("600519", 1266.98, 17554, 221734.0)
    code, q = gtimg_quote.parse_line(line)
    assert code == "600519"
    assert q["volume_raw"] == 17554                          # 源方原值留痕
    assert q["volume"] == pytest.approx(17554 * 100)         # 手→股
    # 归一后量额自洽:amount/(close×volume) ≈ 1
    assert q["volume"] * q["price"] / (q["amount_wan"] * 1e4) == pytest.approx(1.0, abs=0.05)


def test_gtimg_parse_line_量额自证_科创板保持股():
    """科创板 688:源方 volume 已是"股",amount 自证比值≈1 → 不再 ×100(H2 修复核心)。"""
    from tools.collectors import gtimg_quote
    # 688981:price 115.53,原始 volume 10696563 股,amount 123867 万元
    line = _gtimg_line("688981", 115.53, 10696563, 123867.0)
    code, q = gtimg_quote.parse_line(line)
    assert code == "688981"
    assert q["volume_raw"] == 10696563
    assert q["volume"] == pytest.approx(10696563)            # 保持"股",不 ×100
    assert q["volume"] * q["price"] / (q["amount_wan"] * 1e4) == pytest.approx(1.0, abs=0.05)


def test_gtimg_parse_line_自证不了回落代码段():
    """amount 缺失(fqkline 回退行常态)→ 自证失败,回落代码段规则:688 保持股、非688 ×100。"""
    from tools.collectors import gtimg_quote
    _, q_star = gtimg_quote.parse_line(_gtimg_line("688795", 395.58, 1199251, ""))
    _, q_main = gtimg_quote.parse_line(_gtimg_line("600519", 1266.98, 17554, ""))
    # amount_wan 为 "" → _num→None → 无法自证 → 代码段兜底
    assert q_star["volume"] == pytest.approx(1199251)        # 科创板:股,不 ×100
    assert q_main["volume"] == pytest.approx(17554 * 100)    # 非科创板:手→股


def test_fetch_tencent_star_不再scale成交量(monkeypatch):
    """fqkline 端点:科创板 688/689 源方已是"股",不再 ×100(对照非科创板仍 ×100)。"""
    rows = [["2026-09-16", "110.0", "115.53", "116.0", "109.0", "10696563"]]
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(_fq_payload("sh688981", rows)))
    df = market._fetch_tencent("688981", "20260901", "20260916", "qfq")
    assert float(df.iloc[-1]["volume"]) == pytest.approx(10696563)    # 科创板:股,×1
    # 对照:非科创板仍 ×100
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(_fq_payload("sh600519", rows)))
    df2 = market._fetch_tencent("600519", "20260901", "20260916", "qfq")
    assert float(df2.iloc[-1]["volume"]) == pytest.approx(10696563 * 100)


def test_量额断言_分板块_捕获科创板少数派100倍(monkeypatch):
    """H2 盲区回归:688 只占约 11%,若只看全帧中位数,这组 100× 错会被多数派中位数(≈1)掩盖。
    分板块断言后,科创板组自己的中位数偏离 → 必被捕获(hard 抛)。"""
    n_main, n_star = 200, 25          # 星占 ~11%,且每组 ≥20 满足样本下限
    codes = [f"{600000 + i:06d}" for i in range(n_main)] + [f"{688000 + i:06d}" for i in range(n_star)]
    close = [10.0] * (n_main + n_star)
    # 非科创板正确(volume 股,ratio≈1);科创板 volume 少 100×(误留"手"→ ratio≈100)
    volume = [1e5] * n_main + [1000.0] * n_star
    amount = [1e6] * (n_main + n_star)
    df = pd.DataFrame({"code": codes, "open": close, "high": close, "low": close,
                       "close": close, "volume": volume, "amount": amount,
                       "turnover": [2.0] * (n_main + n_star), "pct_chg": [1.0] * (n_main + n_star)})
    # 全帧中位数(旧判据)在这份数据上 ≈1(多数派主导)→ 不会报;新判据分板块 → 科创板组必报
    import statistics
    ratio_all = [a / (c * v) for a, c, v in zip(amount, close, volume)]
    assert 0.5 <= statistics.median(ratio_all) <= 2.0, "构造前提:全帧中位数正常(旧判据会漏)"
    with pytest.raises(ValueError, match="科创板"):
        market._assert_spot_amount_volume(df, "test_star", hard=True)
    # 全部正确(科创板也归股)→ 不抛
    df_ok = df.copy(); df_ok.loc[df_ok["code"].str.startswith("688"), "volume"] = 1e5
    market._assert_spot_amount_volume(df_ok, "test_star", hard=True)
