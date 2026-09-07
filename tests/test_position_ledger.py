"""模拟持仓账本(日内超短线循环)单测。

正确性红线(改坏即挂):
  · α 配对口径:alpha = 个股涨幅% − 基准涨幅%(基准=全A等权净值点位,同时点配对);
  · **基准缺失 → alpha=None(绝不假造 0/中性)**(经验#11/#17);
  · 一码一仓:重复建仓 ValueError;未持仓平仓 ValueError;
  · 平仓结算实现收益/超额并移入 closed;
  · 持有天数按交易日计(建仓当日=第1个交易日);
  · 缺报价的持仓标 stale、保留上次价、不假造;
  · save/load 原子往返一致。

hermetic:monkeypatch 交易日计数,不触网、不读真实账本文件。
"""
import json

import pytest

from tools.pipeline import position_ledger as pl


@pytest.fixture(autouse=True)
def _fixed_trading_days(monkeypatch):
    """把持有天数计成"自然日差+1",与真实日历解耦(可预期)。"""
    from datetime import datetime

    def _fake(open_date, date):
        d0 = datetime.strptime(open_date, "%Y-%m-%d")
        d1 = datetime.strptime(date, "%Y-%m-%d")
        return max(0, (d1 - d0).days + 1)

    monkeypatch.setattr(pl, "count_trading_days", _fake)


def test_open_then_mark_alpha_paired():
    led = pl._empty_ledger()
    pl.open_position(led, code="600519", name="贵州茅台", date="2026-09-07",
                     price=100.0, bench=1000.0)
    # 次日:个股 +10%,基准净值 1000→1005(+0.5%)→ alpha = 10 − 0.5 = 9.5
    pl.mark_to_market(led, date="2026-09-08",
                      quotes={"600519": {"price": 110.0}}, bench=1005.0)
    pos = pl.find_open(led, "600519")
    assert pos["pnl_pct"] == pytest.approx(10.0)
    assert pos["alpha_pct"] == pytest.approx(9.5)
    assert pos["days_held"] == 2          # 建仓日+次日


def test_alpha_none_when_bench_missing():
    led = pl._empty_ledger()
    pl.open_position(led, code="000001", name="平安银行", date="2026-09-07", price=10.0)
    assert pl.find_open(led, "000001")["alpha_pct"] is None   # 建仓无基准
    pl.mark_to_market(led, date="2026-09-08",
                      quotes={"000001": {"price": 11.0}}, bench=None)
    pos = pl.find_open(led, "000001")
    assert pos["pnl_pct"] == pytest.approx(10.0)
    assert pos["alpha_pct"] is None       # 基准缺 → 绝不假造 0


def test_duplicate_open_rejected():
    led = pl._empty_ledger()
    pl.open_position(led, code="600519", name="贵州茅台", date="2026-09-07", price=100.0)
    with pytest.raises(ValueError):
        pl.open_position(led, code="600519", name="贵州茅台", date="2026-09-07", price=101.0)


def test_close_settles_and_moves():
    led = pl._empty_ledger()
    pl.open_position(led, code="600519", name="贵州茅台", date="2026-09-07",
                     price=100.0, bench=1000.0)
    closed = pl.close_position(led, code="600519", date="2026-09-09",
                               price=120.0, bench=1010.0, reason="止盈")
    assert pl.find_open(led, "600519") is None          # 已移出 open
    assert closed["realized_pnl_pct"] == pytest.approx(20.0)
    assert closed["realized_alpha_pct"] == pytest.approx(20.0 - 1.0)   # 基准 +1%
    assert closed["status"] == "已平仓"
    assert led["closed"][0]["close_reason"] == "止盈"


def test_close_without_holding_rejected():
    led = pl._empty_ledger()
    with pytest.raises(ValueError):
        pl.close_position(led, code="600519", date="2026-09-07", price=100.0)


def test_missing_quote_marks_stale():
    led = pl._empty_ledger()
    pl.open_position(led, code="600519", name="贵州茅台", date="2026-09-07",
                     price=100.0, bench=1000.0)
    pl.mark_to_market(led, date="2026-09-08", quotes={}, bench=1005.0)  # 无报价
    pos = pl.find_open(led, "600519")
    assert pos["stale"] is True
    assert pos["last_price"] == 100.0        # 保留上次价、不假造
    assert pos["days_held"] == 2             # 天数仍更新


def test_list_open_codes():
    led = pl._empty_ledger()
    pl.open_position(led, code="600519", name="贵州茅台", date="2026-09-07", price=100.0)
    pl.open_position(led, code="000001", name="平安银行", date="2026-09-07", price=10.0)
    assert set(pl.list_open_codes(led)) == {"600519", "000001"}


def test_save_load_roundtrip(tmp_path):
    led = pl._empty_ledger()
    pl.open_position(led, code="600519", name="贵州茅台", date="2026-09-07",
                     price=100.0, bench=1000.0)
    p = tmp_path / "positions.json"
    pl.save(led, p)
    assert not (tmp_path / "positions.json.tmp").exists()   # tmp 已 rename 掉
    again = pl.load(p)
    assert again["open"][0]["code"] == "600519"
    assert again["updated_at"] is not None
    # 文件是合法 JSON、含 version
    assert json.loads(p.read_text(encoding="utf-8"))["version"] == pl.LEDGER_VERSION


def test_load_missing_returns_empty(tmp_path):
    led = pl.load(tmp_path / "nope.json")
    assert led["open"] == [] and led["closed"] == []


# ── 记分卡 summarize ──

def test_summary_insufficient():
    assert pl.summarize(pl._empty_ledger(), min_n=3)["insufficient"] is True


def test_summary_stats():
    led = pl._empty_ledger()
    # 三笔:+20%(bench+1→α19)、-5%(bench+0→α-5)、+10%(无bench→α不计)
    for code, price, bench in [("600519", 100.0, 1000.0), ("000001", 10.0, 1000.0),
                               ("300308", 50.0, None)]:
        pl.open_position(led, code=code, name=code, date="2026-09-07", price=price, bench=bench)
    pl.close_position(led, code="600519", date="2026-09-09", price=120.0, bench=1010.0)  # +20% α19
    pl.close_position(led, code="000001", date="2026-09-09", price=9.5, bench=1000.0)     # -5%  α-5
    pl.close_position(led, code="300308", date="2026-09-09", price=55.0, bench=None)      # +10% α None
    s = pl.summarize(led)
    assert s["n"] == 3
    assert s["win_rate"] == pytest.approx(2 / 3)      # 两笔正
    assert s["median_pnl_pct"] == pytest.approx(10.0) # 收益 {20,-5,10} 中位 10
    assert s["alpha_n"] == 2                           # 只两笔有基准超额
    assert s["median_alpha_pct"] == pytest.approx((19.0 + -5.0) / 2)  # {19,-5} 中位
    assert s["profit_loss_ratio"] == pytest.approx(15.0 / 5.0)        # avg盈15 / |avg亏5|
