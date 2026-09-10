"""P0 主档语义加固锁语义单测(诊断与设计见 docs/计划/P0_主档语义加固_诊断与设计.md)。

三组锁,一一对应三条静默污染主档的口子:
  · P0-1(风险 F)· as_of≠today 时主档 bar.date == active_date(与 raw 同一逻辑日),
    不再落到 Timestamp.today();sync_master 开跑即 set_active_date(as_of) 全链路统一。
  · P0-2(风险 G)· 盘中写入的当日 bar 在主档 meta 带 provisional 标记;收盘正式 bar 覆盖后清除。
  · P0-3(风险 D)· 未复权价对昨收 qfq 跳变超阈值 **且** 除权日历命中 → 强制全量 backfill;
    正常波动日(仅一条命中)不触发,宁可漏判走陈旧度兜底,绝不误触发。

不触网:market.* / store.* / 除权日历 全部 monkeypatch;主档目录指向 tmp_path。
"""
import pandas as pd

from tools.collectors import market, master_sync
from tools.store import repo as store

_TODAY = "2026-09-10"    # 周四,交易日


def _master_to_tmp(monkeypatch, tmp_path):
    """主档目录指向 tmp_path,真实 store 读写(不触网、不落生产 data/)。"""
    monkeypatch.setattr(store, "_MASTER_DIR", tmp_path / "master")


def _bar(date, close, *, o=None, h=None, l=None, vol=1000):
    o = close if o is None else o
    h = close if h is None else h
    l = close if l is None else l
    return {"date": pd.Timestamp(date), "open": o, "high": h, "low": l,
            "close": close, "volume": vol, "amount": vol * close,
            "turnover": 1.0, "pct_chg": 0.0}


def _spot_row(code, close, **extra):
    row = {"code": code, "open": close, "high": close, "low": close,
           "close": close, "volume": 1000, "amount": 1000 * close,
           "turnover": 1.0, "pct_chg": 0.0}
    row.update(extra)
    return row


# ———————————— P0-1 · 主档写入接 active_date(风险 F)————————————
def test_p0_1_master_bar_date_follows_active_date_not_today(monkeypatch, tmp_path):
    """as_of ≠ 今天(历史补算)时,update_master_from_spot 缺省 date 必须取 active_date(),
    主档新 bar 落 active_date,而非 Timestamp.today()。"""
    _master_to_tmp(monkeypatch, tmp_path)
    code = "000001"
    store.put_master_kline(code, pd.DataFrame([_bar("2026-09-08", 10.0)]), meta={"source": "seed"})
    as_of = "2026-09-09"                     # 历史补算日,非今天
    store.set_active_date(as_of)
    spot = pd.DataFrame([_spot_row(code, 10.5)])
    # date 不显式传 → 走 active_date() 缺省口子
    market.update_master_from_spot(codes=[code], spot=spot, source="test")
    m = store.get_master_kline(code)
    assert pd.to_datetime(m["date"]).max().normalize() == pd.Timestamp(as_of), \
        "主档新 bar 未落 active_date(仍用了 today)"
    store.set_active_date(None)


def test_p0_1_sync_master_sets_active_date_for_raw_alignment(monkeypatch, tmp_path):
    """sync_master(as_of=X) 开跑即 store.set_active_date(X),使主档写入与 raw 采集同一逻辑日。
    断言:调用后 active_date()==as_of,且传给 update_master_from_spot 的 date==as_of。"""
    _master_to_tmp(monkeypatch, tmp_path)
    codes = ["000001", "000002"]
    store.set_active_date(None)
    monkeypatch.setattr(store, "list_master_codes", lambda: list(codes))
    monkeypatch.setattr(store, "get_master_kline_meta",
                        lambda c: {"last_date": "2026-09-09"} if c in codes else None)
    from tools.config import settings
    monkeypatch.setattr(settings, "TUSHARE_ENABLED", False)
    monkeypatch.setattr(market, "fetch_spot_all_tencent",
                        lambda cs: pd.DataFrame([_spot_row(c, 10.0) for c in cs]))
    seen = {}

    def fake_update(codes=None, date=None, spot=None, source=None, **k):
        seen["date"] = date
        return {"ok": len(codes), "skipped": 0}

    monkeypatch.setattr(market, "update_master_from_spot", fake_update)
    master_sync.sync_master(codes, as_of=_TODAY)
    assert store.active_date() == _TODAY, "sync_master 未 set_active_date(as_of)"
    assert seen["date"] == _TODAY, "update_master_from_spot 未收到 as_of 作 date"
    store.set_active_date(None)


# ———————————— P0-3 · 除权事件驱动强制 backfill(风险 D)————————————
def test_p0_3_jump_condition_pure():
    """条件A(跳变判定,纯函数):送转级跳变 True;普通涨跌停 False;缺基线 False。"""
    assert master_sync._exright_jump_exceeds(10.0, 5.0) is True     # 10送10 → 腰斩,断层
    assert master_sync._exright_jump_exceeds(10.0, 6.6) is True     # -34% 送转级
    assert master_sync._exright_jump_exceeds(10.0, 9.0) is False    # -10% 跌停,未超阈值
    assert master_sync._exright_jump_exceeds(10.0, 11.0) is False   # +10% 涨停,未超阈值
    assert master_sync._exright_jump_exceeds(None, 5.0) is False    # 无昨收基线
    assert master_sync._exright_jump_exceeds(0.0, 5.0) is False     # 非正数
    assert master_sync._exright_jump_exceeds(10.0, None) is False


def test_p0_3_meta_carries_last_close(monkeypatch, tmp_path):
    """主档 meta 落 last_close(前复权口径),供除权 pre-scan 读 json 即得昨收 qfq。"""
    _master_to_tmp(monkeypatch, tmp_path)
    code = "000001"
    store.put_master_kline(code, pd.DataFrame([_bar("2026-09-08", 10.0), _bar("2026-09-09", 12.34)]),
                           meta={"source": "seed"})
    meta = store.get_master_kline_meta(code)
    assert meta["last_close"] == 12.34, "meta 未落最新 bar 的 close"


def _mk_spot_indexed(monkeypatch, tmp_path, code, prev_qfq, new_unadj):
    """造一票主档(昨收 qfq=prev_qfq)+ 当日 spot(未复权 close=new_unadj);返回 spot df。"""
    _master_to_tmp(monkeypatch, tmp_path)
    store.put_master_kline(code, pd.DataFrame([_bar("2026-09-09", prev_qfq)]), meta={"source": "seed"})
    return pd.DataFrame([_spot_row(code, new_unadj)])


def test_p0_3_real_exright_triggers_backfill(monkeypatch, tmp_path):
    """真除权:跳变超阈值 且 日历命中 → 强制全量 backfill,且该票进 forced 集(将从 spot 剔除)。"""
    code = "600000"
    spot = _mk_spot_indexed(monkeypatch, tmp_path, code, prev_qfq=20.0, new_unadj=10.0)  # 腰斩
    monkeypatch.setattr(master_sync, "_exright_calendar_hit", lambda c, s, e: True)   # 日历命中
    called = {}

    def fake_backfill(codes, *a, **k):
        called["codes"] = list(codes)
        return {"ok": 1, "failed": 0}

    monkeypatch.setattr(market, "backfill_master", fake_backfill)
    forced = master_sync._force_backfill_exright([code], spot, _TODAY)
    assert forced == {code} and called["codes"] == [code], "真除权未触发强制 backfill"


def test_p0_3_high_volatility_no_calendar_no_trigger(monkeypatch, tmp_path):
    """高波动误伤防护:跳变超阈值但日历未命中 → 不触发(宁可漏判走陈旧度兜底)。"""
    code = "300001"
    spot = _mk_spot_indexed(monkeypatch, tmp_path, code, prev_qfq=20.0, new_unadj=10.0)
    monkeypatch.setattr(master_sync, "_exright_calendar_hit", lambda c, s, e: False)  # 日历未命中
    monkeypatch.setattr(market, "backfill_master",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("日历未命中不应 backfill")))
    forced = master_sync._force_backfill_exright([code], spot, _TODAY)
    assert forced == set(), "日历未命中却触发了全量 backfill(误伤)"


def test_p0_3_normal_day_skips_calendar_query(monkeypatch, tmp_path):
    """条件A不过(普通波动)→ 连日历都不查(省网络),更不触发。"""
    code = "000001"
    spot = _mk_spot_indexed(monkeypatch, tmp_path, code, prev_qfq=20.0, new_unadj=21.0)  # +5%
    monkeypatch.setattr(master_sync, "_exright_calendar_hit",
                        lambda c, s, e: (_ for _ in ()).throw(AssertionError("条件A不过不应查日历")))
    monkeypatch.setattr(market, "backfill_master",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("正常日不应 backfill")))
    assert master_sync._force_backfill_exright([code], spot, _TODAY) == set()


def test_p0_3_no_baseline_no_trigger(monkeypatch, tmp_path):
    """无昨收基线(新股首次/旧 meta 无 last_close)→ 不判、不触发。"""
    _master_to_tmp(monkeypatch, tmp_path)
    code = "000001"
    monkeypatch.setattr(store, "get_master_kline_meta", lambda c: {"last_date": "2026-09-09"})  # 无 last_close
    monkeypatch.setattr(market, "backfill_master",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("无基线不应 backfill")))
    spot = pd.DataFrame([_spot_row(code, 10.0)])
    assert master_sync._force_backfill_exright([code], spot, _TODAY) == set()


def test_p0_3_sync_master_excludes_forced_from_spot(monkeypatch, tmp_path):
    """集成:除权票被强制 backfill 后,不再进 update_master_from_spot 的 codes(防 qfq 重算又被覆盖)。"""
    _master_to_tmp(monkeypatch, tmp_path)
    exright_code, normal_code = "600000", "000002"
    codes = [exright_code, normal_code]
    monkeypatch.setattr(store, "list_master_codes", lambda: list(codes))
    monkeypatch.setattr(store, "get_master_kline_meta",
                        lambda c: {"last_date": "2026-09-09", "last_close": 20.0})
    from tools.config import settings, stock_pool
    monkeypatch.setattr(settings, "TUSHARE_ENABLED", False)
    monkeypatch.setattr(stock_pool, "is_hk", lambda c: False)
    # spot:除权票腰斩、正常票平;两票都在 spot
    monkeypatch.setattr(market, "fetch_spot_all_tencent",
                        lambda cs: pd.DataFrame([_spot_row(exright_code, 10.0), _spot_row(normal_code, 20.0)]))
    monkeypatch.setattr(master_sync, "_exright_calendar_hit",
                        lambda c, s, e: c == exright_code)                # 只有除权票日历命中
    bf = {}

    def fake_backfill(codes, *a, **k):
        bf["codes"] = list(codes)
        return {"ok": 1, "failed": 0}

    monkeypatch.setattr(market, "backfill_master", fake_backfill)
    seen = {}

    def fake_update(codes=None, date=None, spot=None, source=None, **k):
        seen["codes"] = list(codes)
        return {"ok": len(codes), "skipped": 0}

    monkeypatch.setattr(market, "update_master_from_spot", fake_update)
    master_sync.sync_master(codes, as_of=_TODAY)
    assert bf["codes"] == [exright_code], "除权票未被强制全量 backfill"
    assert seen["codes"] == [normal_code], "除权票未从 spot 增量集剔除"
    store.set_active_date(None)


def test_p0_3_calendar_hit_window_semantics(monkeypatch):
    """条件B 日历窗口语义:除权日须 (last_date, as_of] 内才命中——早于/等于 last_date 不算,
    晚于 as_of 不算。用假 baostock 会话锁死,不触网。"""
    class _FakeRS:
        def __init__(self, dates):
            self.error_code = "0"
            self.fields = ["dividOperateDate"]
            self._rows = [[d] for d in dates]
            self._i = -1

        def next(self):
            self._i += 1
            return self._i < len(self._rows)

        def get_row_data(self):
            return self._rows[self._i]

    class _FakeBS:
        def __init__(self, dates):
            self._dates = dates

        def query_dividend_data(self, code, year, yearType):
            return _FakeRS([d for d in self._dates if d.startswith(year)])

    from contextlib import contextmanager
    from tools.collectors import baostock_src

    def make_session(dates):
        @contextmanager
        def _sess():
            yield _FakeBS(dates)
        return _sess

    monkeypatch.setattr(baostock_src, "bs_code", lambda c: "sh.600000")
    # 除权日 2026-09-10 落在 (2026-09-09, 2026-09-10] → 命中
    monkeypatch.setattr(baostock_src, "session", make_session(["2026-09-10"]))
    assert master_sync._exright_calendar_hit("600000", "2026-09-09", "2026-09-10") is True
    # 除权日 == last_date(边界左开)→ 不命中
    monkeypatch.setattr(baostock_src, "session", make_session(["2026-09-09"]))
    assert master_sync._exright_calendar_hit("600000", "2026-09-09", "2026-09-10") is False
    # 除权日晚于 as_of → 不命中
    monkeypatch.setattr(baostock_src, "session", make_session(["2026-09-11"]))
    assert master_sync._exright_calendar_hit("600000", "2026-09-09", "2026-09-10") is False


# ———————————— P0-2 · 盘中 provisional 标记,收盘覆盖除标(风险 G)————————————
def test_p0_2_intraday_bar_marked_provisional(monkeypatch, tmp_path):
    """盘中写入(provisional=True)→ 当日 bar 在主档 meta 标 provisional;默认读取不受影响。"""
    _master_to_tmp(monkeypatch, tmp_path)
    code = "000001"
    store.put_master_kline(code, pd.DataFrame([_bar("2026-09-09", 10.0)]), meta={"source": "seed"})
    store.set_active_date(_TODAY)
    spot = pd.DataFrame([_spot_row(code, 10.8)])   # 盘中午间价
    market.update_master_from_spot(codes=[code], date=_TODAY, spot=spot, source="test", provisional=True)
    assert store.master_provisional_dates(code) == {_TODAY}, "盘中 bar 未标 provisional"
    # 默认读取路径含该 bar(零影响);confirmed 读取剔除临时价日
    assert len(store.get_master_kline(code)) == 2
    assert pd.Timestamp(_TODAY) not in pd.to_datetime(store.get_master_kline_confirmed(code)["date"]).values
    store.set_active_date(None)


def test_p0_2_close_overwrite_clears_provisional(monkeypatch, tmp_path):
    """收盘正式 bar(provisional=False)覆盖同日 → provisional 标记清除,confirmed 读取重新含该日。"""
    _master_to_tmp(monkeypatch, tmp_path)
    code = "000001"
    store.put_master_kline(code, pd.DataFrame([_bar("2026-09-09", 10.0)]), meta={"source": "seed"})
    store.set_active_date(_TODAY)
    # 盘中:临时价 10.8 标 provisional
    market.update_master_from_spot(codes=[code], date=_TODAY, spot=pd.DataFrame([_spot_row(code, 10.8)]),
                                   source="test", provisional=True)
    assert store.master_provisional_dates(code) == {_TODAY}
    # 收盘:正式价 11.2 覆盖同日,provisional=False → 除标
    market.update_master_from_spot(codes=[code], date=_TODAY, spot=pd.DataFrame([_spot_row(code, 11.2)]),
                                   source="test", provisional=False)
    assert store.master_provisional_dates(code) == set(), "收盘覆盖后未清除 provisional 标记"
    m = store.get_master_kline_confirmed(code)
    assert float(m[m["date"] == pd.Timestamp(_TODAY)]["close"].iloc[0]) == 11.2, "收盘定稿价未进 confirmed"
    store.set_active_date(None)


def test_p0_2_default_off_no_provisional(monkeypatch, tmp_path):
    """默认(provisional=False)写入零影响:不产生任何 provisional 标记。"""
    _master_to_tmp(monkeypatch, tmp_path)
    code = "000001"
    store.put_master_kline(code, pd.DataFrame([_bar("2026-09-09", 10.0)]), meta={"source": "seed"})
    store.set_active_date(_TODAY)
    market.update_master_from_spot(codes=[code], date=_TODAY, spot=pd.DataFrame([_spot_row(code, 10.8)]),
                                   source="test")   # provisional 缺省 False
    assert store.master_provisional_dates(code) == set()
    # confirmed 与默认读取一致(无标记 → 等价)
    assert len(store.get_master_kline_confirmed(code)) == len(store.get_master_kline(code)) == 2
    store.set_active_date(None)


def test_p0_2_provisional_persists_across_other_day_writes(monkeypatch, tmp_path):
    """provisional 标记跨"写别的日"持久:只有覆盖被标日本身(写成 final)才除标。"""
    _master_to_tmp(monkeypatch, tmp_path)
    code = "000001"
    store.put_master_kline(code, pd.DataFrame([_bar("2026-09-09", 10.0)]), meta={"source": "seed"})
    # 09-10 标 provisional
    store.append_master_kline(code, pd.DataFrame([_bar("2026-09-10", 10.8)]),
                              provisional_dates={"2026-09-10"})
    assert store.master_provisional_dates(code) == {"2026-09-10"}
    # 再写 09-11(别的日,非 provisional)→ 09-10 标记应保留(未被覆盖成 final)
    store.append_master_kline(code, pd.DataFrame([_bar("2026-09-11", 11.0)]))
    assert store.master_provisional_dates(code) == {"2026-09-10"}, "写别的日误清了 provisional"
