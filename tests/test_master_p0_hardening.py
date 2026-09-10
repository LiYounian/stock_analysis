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
