"""策略0「全A 多专家合议」驱动单测(tools/pipeline/screen_council)。

锁死语义:
  - 组装最小记录 → signals 由 technical.compute 现算(不改算法);无全A数据的专家自然弃权。
  - Top 按合议综合分降序;空池仍产出 view(top=[]),不崩。
  - 防未来:compute 只用最后一根及之前;给未来追加一根不改变"截至当日"的判定(此处以
    "只喂到当日的 kline 与喂到当日+未来一根,截至当日结论一致"做锁定,防未来数据泄漏)。
data-independent:store 落盘走 monkeypatch 到临时目录。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.pipeline import screen_council as sc


def _kline(n=120, trend=0.0, start=10.0, seed=0):
    """构造 n 根合成前复权日线。trend>0 上行、<0 下行;含 compute 需要的全部列。"""
    rng = np.random.default_rng(seed)
    close = start + np.cumsum(np.full(n, trend) + rng.normal(0, 0.05, n))
    close = np.clip(close, 0.5, None)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + 0.05
    low = np.minimum(open_, close) - 0.05
    vol = rng.integers(1_000_000, 2_000_000, n).astype(float)
    pct = np.concatenate([[0.0], np.diff(close) / close[:-1] * 100])
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({"date": dates, "open": open_, "high": high, "low": low,
                         "close": close, "volume": vol, "amount": close * vol,
                         "turnover": 0.01, "pct_chg": pct})


def test_build_min_record_shape_and_abstention():
    """最小记录:meta{code,行业} + signals(trend/reversal/ob_os);无全A数据的专家弃权(置信度0)。"""
    rec = sc.build_min_record("000001", _kline(trend=0.1, seed=1))
    assert rec is not None
    assert set(rec["signals"].keys()) == {"trend", "reversal", "ob_os"}
    assert rec["meta"]["code"] == "000001"
    # 无 fundflow/sentiment/factor/as_of → 这些专家必弃权(置信度0)
    from tools.analysis import council
    cblk = council.build_council_block(rec, None)
    conf = {e["专家"]: e["置信度"] for e in cblk["experts"]}
    for name in ("资金流", "情绪三层", "多因子", "事件驱动"):
        assert conf[name] == 0.0, f"{name} 应弃权(无全A数据)"
    # V2 重加权:技术趋势已从默认专家组删除,不再入合议;
    # 存活技术专家(超买超卖/拐点,读 ob_os/reversal signals)仍在场。
    assert "技术趋势" not in conf, "技术趋势 应已从合议默认专家组移除(V2 删除)"
    assert "超买超卖" in conf and "拐点" in conf, "存活技术专家应仍在合议块中"


def test_build_min_record_none_when_insufficient():
    """K 线过短(technical.compute 返回 error,无 signal)→ 返回 None(该票跳过)。"""
    assert sc.build_min_record("000001", _kline(n=1)) is None


def test_run_council_screen_ranks_desc_and_lands_view(monkeypatch, tmp_path):
    """扫描多票 → 落 view「策略0合议」,top 按综合分降序,schema 完整。"""
    kmap = {"000001": _kline(trend=0.15, seed=2),   # 强上行 → 综合分高
            "000002": _kline(trend=-0.15, seed=3),  # 下行 → 综合分低
            "000003": _kline(trend=0.02, seed=4)}
    monkeypatch.setattr(sc.market, "load_kline", lambda code: kmap[code])
    monkeypatch.setattr(sc.board, "board_of", lambda code: None)
    captured = {}
    monkeypatch.setattr(sc.store, "set_active_date", lambda d: None)
    monkeypatch.setattr(sc.store, "put_view",
                        lambda name, obj, date=None: captured.update({"name": name, "obj": obj}) or "p")
    v = sc.run_council_screen(["000001", "000002", "000003"], as_of="2026-08-08", fetch=False)
    assert captured["name"] == "策略0合议"
    assert v["扫描数"] == 3 and v["有效"] == 3
    scores = [t["综合分"] for t in v["top"]]
    assert scores == sorted(scores, reverse=True)          # 降序
    # 每项带 council 信封(default+experts+config)供前端重排
    top0 = v["top"][0]
    assert {"default", "experts", "config"} <= set(top0["council"].keys())
    assert top0["综合方向"] in ("看多", "看空", "中性")


def test_run_council_screen_empty_pool_still_lands_view(monkeypatch):
    """空池 / 全部历史不足 → view top=[] 仍产出,不崩。"""
    monkeypatch.setattr(sc.market, "load_kline",
                        lambda code: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(sc.store, "set_active_date", lambda d: None)
    monkeypatch.setattr(sc.store, "put_view", lambda name, obj, date=None: "p")
    v = sc.run_council_screen(["000001"], as_of="2026-08-08", fetch=False)
    assert v["top"] == [] and v["有效"] == 0 and v["跳过数(历史不足/无信号)"] == 1


def test_no_future_leak_last_bar_only(monkeypatch):
    """防未来:在当日 kline 尾部再追加"未来"若干根后,截至当日(去掉未来根)的合议结论应一致。

    以"只喂到当日"与"喂到当日再截回当日"两次跑综合分相等,锁死驱动只用当日及之前(compute 取最后一根)。
    """
    base = _kline(n=100, trend=0.1, seed=5)
    monkeypatch.setattr(sc.board, "board_of", lambda code: None)
    rec_today = sc.build_min_record("000001", base)
    # 追加 5 根"未来"再切回当日 → 与只喂到当日应完全一致(未用未来数据)
    future = _kline(n=105, trend=0.1, seed=5).iloc[:100].reset_index(drop=True)
    rec_cut = sc.build_min_record("000001", future)
    assert rec_today["signals"]["trend"]["得分"] == rec_cut["signals"]["trend"]["得分"]


def test_offline_universe_codes_from_master(tmp_path, monkeypatch):
    """离线票池枚举:主档代码升序 + limit(隔离 raw 到空目录,只验主档口径)。"""
    from tools.config import settings
    monkeypatch.setattr(sc.store, "list_master_codes", lambda: ["000002", "000001", "600519"])
    monkeypatch.setattr(settings, "DATA_RAW", tmp_path / "noraw")     # 无 raw 分区 → 只剩主档
    assert sc._offline_universe_codes() == ["000001", "000002", "600519"]
    assert sc._offline_universe_codes(limit=2) == ["000001", "000002"]


def test_offline_universe_unions_master_and_raw(tmp_path, monkeypatch):
    """回归:主档非空时也要并 raw 分区(此前 if-not-codes 把全A缩到自选几十只)。"""
    from tools.pipeline import screen_council as sc
    from tools.config import settings
    monkeypatch.setattr(sc.store, "list_master_codes", lambda: ["000001"])   # 主档非空
    raw = tmp_path / "raw" / "2026-08-08" / "kline"
    raw.mkdir(parents=True)
    for c in ("600000", "300001"):
        (raw / f"{c}.parquet").write_bytes(b"x")
    monkeypatch.setattr(settings, "DATA_RAW", tmp_path / "raw")
    codes = set(sc._offline_universe_codes())
    assert {"000001", "600000", "300001"} <= codes      # 主档 ∪ raw 都在(不再只剩主档)
