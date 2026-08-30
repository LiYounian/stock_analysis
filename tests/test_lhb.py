"""lhb.py 龙虎榜采集器单测(mock 网络,不触网)。

锁的语义:
- _norm_row 归一到契约字段,**丢弃全部前视列(上榜后1/2/5/10日)**——防未来函数红线;
- 盘后披露标记 visible_after_close=True;direction = 净买额符号(+1/−1/0);
- 落盘按票分区 + (list_date,reason) 去重幂等 + 前向增量并集(旧快照独有条保留);
- 新鲜度门控:缓存新鲜跳过重写;stale_codes 过滤;
- lhb_asof **严格小于 as_of**(盘后披露,上榜日当天不可用)——无未来函数切片;
- 区间拉取失败优雅降级(空 DataFrame,不抛)。
"""
import pandas as pd
import pytest

from tools.collectors import lhb
from tools.store import repo as store


def _raw_row(code="000564", list_date="2024-01-05", reason="日振幅15%",
             net_buy=100.0, **kw):
    """东财原始一行(含前视列,用于验证被丢弃)。"""
    row = {
        "代码": code, "名称": "某股", "上榜日": list_date, "解读": "游资买入",
        "收盘价": 1.66, "涨跌幅": 5.06, "龙虎榜净买额": net_buy,
        "龙虎榜买入额": 700.0, "龙虎榜卖出额": 400.0, "龙虎榜成交额": 1100.0,
        "市场总成交额": 24623625.0, "净买额占总成交比": 11.9, "成交额占总成交比": 48.2,
        "换手率": 0.04, "流通市值": 2.5e10, "上榜原因": reason,
        # —— 前视列(未来信息)——,必须被丢弃
        "上榜后1日": 4.8, "上榜后2日": 10.2, "上榜后5日": 15.0, "上榜后10日": 31.3,
    }
    row.update(kw)
    return row


# ———————————— 解析 / 防未来函数(丢前视列) ————————————
def test_norm_row_contract_and_drops_lookahead():
    ev = lhb._norm_row(_raw_row(net_buy=954439.0))
    # 前视列绝不入库
    for leak in ("上榜后1日", "上榜后2日", "上榜后5日", "上榜后10日",
                 "lookahead_1d", "fwd1", "上榜后1日收益"):
        assert leak not in ev
    assert ev["code"] == "000564" and ev["list_date"] == "2024-01-05"
    assert ev["reason"] == "日振幅15%" and ev["direction"] == 1
    assert ev["visible_after_close"] is True          # 盘后披露标记
    assert ev["net_buy"] == 954439.0 and ev["net_buy_ratio"] == 11.9


def test_direction_sign():
    assert lhb.lhb_direction(100.0) == 1
    assert lhb.lhb_direction(-100.0) == -1
    assert lhb.lhb_direction(0.0) == 0 and lhb.lhb_direction(None) == 0


def test_norm_row_rejects_bad():
    assert lhb._norm_row({"代码": "", "上榜日": "2024-01-05"}) is None
    assert lhb._norm_row({"代码": "abc", "上榜日": "2024-01-05"}) is None
    assert lhb._norm_row({"代码": "000001", "上榜日": ""}) is None


def test_norm_date_variants():
    assert lhb._norm_date("20240105") == "2024-01-05"
    assert lhb._norm_date("2024-01-05") == "2024-01-05"


def _install_range(monkeypatch, rows):
    """把 fetch_range_df 换成返回归一后的 DataFrame(等价 mock 网络)。"""
    df = pd.DataFrame([lhb._norm_row(r) for r in rows])
    monkeypatch.setattr(lhb, "fetch_range_df", lambda *a, **k: df)


# ———————————— 落盘 / 幂等 / 增量 / 门控 ————————————
def test_fetch_persists_per_code_and_dedup(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_range(monkeypatch, [
        _raw_row(code="000564", list_date="2024-01-05", reason="A"),
        _raw_row(code="000564", list_date="2024-01-05", reason="A"),   # 完全同键 → 去重
        _raw_row(code="000564", list_date="2024-01-05", reason="B"),   # 同日不同原因 → 保留
        _raw_row(code="000595", list_date="2024-01-05", reason="A"),
    ])
    out = lhb.fetch_lhb("20240101", "20240110")
    assert set(out) == {"000564", "000595"}
    reasons = sorted(ev["reason"] for ev in out["000564"])
    assert reasons == ["A", "B"]                       # 同键去重、异原因保留
    assert store.get_raw_meta("lhb", "000564")["source"] == "eastmoney"


def test_incremental_union_keeps_old(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_range(monkeypatch, [_raw_row(list_date="2024-01-03", reason="旧")])
    lhb.fetch_lhb("20240101", "20240104")
    _install_range(monkeypatch, [_raw_row(list_date="2024-01-08", reason="新")])
    out = lhb.fetch_lhb("20240105", "20240110", skip_fresh=False)
    dates = {ev["list_date"] for ev in out["000564"]}
    assert dates == {"2024-01-03", "2024-01-08"}       # 旧快照独有条保留


def test_freshness_gate_skips_fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_range(monkeypatch, [_raw_row(reason="首采")])
    lhb.fetch_lhb("20240101", "20240110")
    # 新鲜快照 → 门控跳过写入(即便区间又给了新数据,也不覆盖)
    _install_range(monkeypatch, [_raw_row(reason="不该写入")])
    out = lhb.fetch_lhb("20240101", "20240110", skip_fresh=True, max_days=30)
    assert [ev["reason"] for ev in out["000564"]] == ["首采"]


def test_stale_codes_filters(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_range(monkeypatch, [_raw_row(code="000564")])
    lhb.fetch_lhb("20240101", "20240110")
    assert lhb.stale_codes(["000564", "000999"], max_days=30) == ["000999"]


def test_fetch_range_df_degrades(monkeypatch):
    """akshare 抛错 → 空 DataFrame,不抛(优雅降级)。"""
    import akshare as ak
    monkeypatch.setattr(ak, "stock_lhb_detail_em",
                        lambda **k: (_ for _ in ()).throw(ConnectionError("限流")))
    assert lhb.fetch_range_df("20240101", "20240110").empty


# ———————————— as-of 无未来函数(严格小于) ————————————
def test_asof_strict_less_than(monkeypatch, tmp_path):
    """盘后披露:上榜日 D 当天不可用 → as_of=D 切掉,as_of=D+1 才可见。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_range(monkeypatch, [_raw_row(list_date="2024-01-05", reason="X")])
    lhb.fetch_lhb("20240101", "20240110")
    assert lhb.lhb_asof("000564", "2024-01-05") == []          # 当天不可用
    got = lhb.lhb_asof("000564", "2024-01-08")
    assert len(got) == 1 and got[0]["reason"] == "X"


def test_load_roundtrip_and_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_range(monkeypatch, [_raw_row(code="000564")])
    lhb.fetch_lhb("20240101", "20240110")
    assert isinstance(lhb.load_lhb("000564"), list)
    with pytest.raises(FileNotFoundError):
        lhb.load_lhb("999999")
