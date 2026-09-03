"""baostock 源封装 + backfill_master + spot 列映射单测(mock baostock/akshare,不触网)。

锁语义:
  - bs_code 交易所前缀映射;
  - fetch_one 列归一到 _STD_COLS、adjust→adjustflag、空数据/接口错误抛错;
  - backfill_master 经 session 落主档、单只失败跳过不中断;
  - fetch_spot_all 中文列 → 标准列 + code 补零。
"""
import pandas as pd
import pytest

from tools.collectors import baostock_src, market
from tools.store import repo as store


def test_bs_code():
    """判据已收到 `tools.config.exchange` 单一真源,这里只锁 baostock 侧的协议形式。

    ⚠️ 北交所从"映射成 bj./sh. 前缀"改成 **None(源不支持)**。2026-09-03 真调实测:
    baostock 协议只认 `sh.`/`sz.`,`bj.830799` 报 error 10004011「股票代码未标识sh或sz」,
    而 `sh.920002`/`sz.920002` 更坏 —— error_code 0 **success 但 0 行**,调用方会误读成
    "这只票没有历史数据"。故显式表达"该源不覆盖北交所",由调用方记降级。
    全段路由与降级语义见 tests/test_exchange_single_source.py。
    """
    assert baostock_src.bs_code("600519") == "sh.600519"
    assert baostock_src.bs_code("000021") == "sz.000021"
    assert baostock_src.bs_code("300750") == "sz.300750"
    assert baostock_src.bs_code("830799") is None      # 北交所:baostock 不覆盖
    assert baostock_src.bs_code("920002") is None      # 北交所现行段,同上


class _FakeRS:
    """模拟 baostock query 结果集。"""
    def __init__(self, rows, fields, error_code="0", error_msg="ok"):
        self._rows = rows
        self._i = -1
        self.fields = fields
        self.error_code = error_code
        self.error_msg = error_msg

    def next(self):
        self._i += 1
        return self._i < len(self._rows)

    def get_row_data(self):
        return self._rows[self._i]


def _install_fake_bs(monkeypatch, rows=None, error_code="0"):
    import baostock as bs
    fields = ["date", "open", "high", "low", "close", "volume", "amount", "turn", "pctChg"]
    if rows is None:
        rows = [
            ["2026-08-05", "10.0", "10.5", "9.8", "10.2", "1000", "1.0e6", "0.05", "1.2"],
            ["2026-08-06", "10.2", "10.8", "10.1", "10.6", "1200", "1.2e6", "0.06", "3.9"],
        ]
    captured = {}

    def fake_query(code, fld, start_date, end_date, frequency, adjustflag):
        captured["code"] = code
        captured["adjustflag"] = adjustflag
        return _FakeRS(rows, fields, error_code=error_code)

    monkeypatch.setattr(bs, "login", lambda *a, **k: type("L", (), {"error_code": "0", "error_msg": "ok"})())
    monkeypatch.setattr(bs, "logout", lambda *a, **k: None)
    monkeypatch.setattr(bs, "query_history_k_data_plus", fake_query)
    return captured


def test_fetch_one_normalizes(monkeypatch):
    cap = _install_fake_bs(monkeypatch)
    with baostock_src.session():
        df = baostock_src.fetch_one("000021", "2026-08-01", "2026-08-07", adjust="qfq")
    assert list(df.columns) == baostock_src._STD_COLS
    assert cap["adjustflag"] == "2"            # qfq → 2
    assert cap["code"] == "sz.000021"
    assert df["date"].is_monotonic_increasing
    assert df.iloc[-1]["close"] == 10.6


def test_fetch_one_adjust_hfq_none(monkeypatch):
    cap = _install_fake_bs(monkeypatch)
    with baostock_src.session():
        baostock_src.fetch_one("600519", "2026-08-01", "2026-08-07", adjust="hfq")
    assert cap["adjustflag"] == "1"
    with baostock_src.session():
        baostock_src.fetch_one("600519", "2026-08-01", "2026-08-07", adjust="")
    assert cap["adjustflag"] == "3"


def test_fetch_one_empty_raises(monkeypatch):
    _install_fake_bs(monkeypatch, rows=[])
    with baostock_src.session():
        with pytest.raises(ValueError):
            baostock_src.fetch_one("000021", "2026-08-01", "2026-08-07")


def test_fetch_one_error_code_raises(monkeypatch):
    _install_fake_bs(monkeypatch, error_code="10001")
    with baostock_src.session():
        with pytest.raises(ConnectionError):
            baostock_src.fetch_one("000021", "2026-08-01", "2026-08-07")


def test_backfill_master(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_MASTER_DIR", tmp_path / "master")
    _install_fake_bs(monkeypatch)
    res = market.backfill_master(["000021", "600519"], start="20260801", end="20260807")
    assert res == {"ok": 2, "failed": 0}
    assert store.has_master_kline("000021")
    got = store.get_master_kline("600519")
    assert len(got) == 2 and got["date"].is_monotonic_increasing


def test_backfill_master_skips_failure(monkeypatch, tmp_path):
    """单只抛错 → 记失败跳过,不中断整批。"""
    monkeypatch.setattr(store, "_MASTER_DIR", tmp_path / "master")
    _install_fake_bs(monkeypatch)

    def flaky(code, s, e, adjust="qfq"):
        if code == "000021":
            raise ValueError("空数据")
        return baostock_src.fetch_one.__wrapped__(code, s, e, adjust) if hasattr(
            baostock_src.fetch_one, "__wrapped__") else pd.DataFrame({
                "date": pd.to_datetime(["2026-08-06"]), "open": [1.0], "high": [1.0],
                "low": [1.0], "close": [1.0], "volume": [1.0], "amount": [1.0],
                "turnover": [0.0], "pct_chg": [0.0]})

    monkeypatch.setattr(baostock_src, "fetch_one", flaky)
    res = market.backfill_master(["000021", "600519"], start="20260801", end="20260807")
    assert res["ok"] == 1 and res["failed"] == 1
    assert not store.has_master_kline("000021")
    assert store.has_master_kline("600519")


def test_fetch_spot_all_column_map(monkeypatch):
    import akshare as ak
    fake = pd.DataFrame({
        "代码": ["21", "600519"], "名称": ["深振业A", "贵州茅台"],
        "今开": [10.0, 1300.0], "最高": [10.5, 1310.0], "最低": [9.9, 1295.0],
        "最新价": [10.3, 1305.0], "成交量": [1000, 500], "成交额": [1e6, 5e8],
        "换手率": [0.05, 0.01], "涨跌幅": [1.2, -0.3],
    })
    monkeypatch.setattr(ak, "stock_zh_a_spot_em", lambda *a, **k: fake)
    df = market.fetch_spot_all()
    assert set(["code", "open", "high", "low", "close", "volume",
                "amount", "turnover", "pct_chg"]).issubset(df.columns)
    assert list(df["code"]) == ["000021", "600519"]     # 补零到 6 位
    assert df.iloc[0]["close"] == 10.3
