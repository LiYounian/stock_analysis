"""财报采集·报告期指纹复用(哲学二)单测——锁「报告期新鲜度闸」语义(约法6,全 mock 不触网)。

锁死的规则(防未来 prompt/代码重写误删):
  1. 报告期未变 → 跳过采集(spy 0 次);
  2. 报告期过期(缓存最新可见期 < 法定应披露最新期)→ 触发重采;
  3. 无缓存 → 采;
  4. 日更混合池 → 只采过期/缺失子集,不是全A;
  5. 开关关闭(报告期指纹复用=False)→ 退回存在性 skip(有缓存即不采),可逆;
  6. 法定披露日历 expected_latest_report_period 的边界口径;
  7. 防未来函数:disclosure_date > as_of 的报告期不算「可见」,不参与新鲜度判定。
"""
from unittest.mock import MagicMock

from tools.collectors import financial as fin
from tools.store import repo as store


# ————————————————————— 夹具:写合成财报 raw —————————————————————
def _put_raw(code: str, periods):
    """periods: [(report_date, disclosure_date)];写一份最小 financial_report raw。"""
    payload = {
        "code": code, "name": "测试股",
        "periods": {
            rd: {"report_date": rd, "disclosure_date": dd,
                 "report_type": None, "利润表": {}, "资产负债表": {}, "现金流量表": {}}
            for rd, dd in periods
        },
        "n_periods": len(periods),
    }
    store.put_raw("financial_report", code, payload, meta={"source": "test"})


def _spy_fetch(monkeypatch):
    """把 fin.fetch_financial 换成 spy(返回 {}),记录被要求采集的 codes。"""
    spy = MagicMock(return_value={})
    monkeypatch.setattr(fin, "fetch_financial", spy)
    return spy


AS_OF = "2026-09-09"          # md 09-09 ∈ [08-31,10-30] → 法定应披露最新期 = 2026-06-30(中报)


# ————————————————————— 1. 报告期未变 → 跳过 —————————————————————
def test_报告期未变跳过采集(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    store.set_active_date(AS_OF)
    _put_raw("600000", [("2026-03-31", "2026-04-20"), ("2026-06-30", "2026-08-15")])  # 已达中报
    spy = _spy_fetch(monkeypatch)
    out = fin.fetch_financial_missing_or_stale(["600000"], as_of=AS_OF, reuse_fingerprint=True)
    spy.assert_not_called()                    # 新鲜 → 不采
    assert out == {}


# ————————————————————— 2. 报告期过期 → 重采 + 防未来 —————————————————————
def test_报告期过期触发重采(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    store.set_active_date(AS_OF)
    # 缓存仅到一季报(2026-03-31)< 应披露的中报(2026-06-30)→ 过期
    _put_raw("600001", [("2025-12-31", "2026-04-20"), ("2026-03-31", "2026-04-20")])
    spy = _spy_fetch(monkeypatch)
    fin.fetch_financial_missing_or_stale(["600001"], as_of=AS_OF, reuse_fingerprint=True)
    spy.assert_called_once()
    assert list(spy.call_args.args[0]) == ["600001"]


def test_未来披露报告期不算可见(monkeypatch, tmp_path):
    """防未来函数:中报 report_date 已在缓存,但 disclosure_date > as_of → 不可见,
    最新可见仍是一季报 < 应披露中报 → 判过期重采(不因未披露的未来期误判为新鲜)。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    store.set_active_date(AS_OF)
    _put_raw("600002", [("2026-03-31", "2026-04-20"), ("2026-06-30", "2026-10-01")])  # 中报披露在未来
    spy = _spy_fetch(monkeypatch)
    fin.fetch_financial_missing_or_stale(["600002"], as_of=AS_OF, reuse_fingerprint=True)
    spy.assert_called_once()
    assert list(spy.call_args.args[0]) == ["600002"]


# ————————————————————— 3. 无缓存 → 采 —————————————————————
def test_无缓存触发采集(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    store.set_active_date(AS_OF)
    spy = _spy_fetch(monkeypatch)
    fin.fetch_financial_missing_or_stale(["600003"], as_of=AS_OF, reuse_fingerprint=True)
    spy.assert_called_once()
    assert list(spy.call_args.args[0]) == ["600003"]


# ————————————————————— 4. 日更混合池 → 只采过期/缺失 —————————————————————
def test_日更混合只采过期缺失子集(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    store.set_active_date(AS_OF)
    _put_raw("600010", [("2026-06-30", "2026-08-15")])   # 新鲜
    _put_raw("600011", [("2026-03-31", "2026-04-20")])   # 过期(仅一季报)
    # 600012 无缓存
    spy = _spy_fetch(monkeypatch)
    fin.fetch_financial_missing_or_stale(["600010", "600011", "600012"],
                                         as_of=AS_OF, reuse_fingerprint=True)
    spy.assert_called_once()
    assert set(spy.call_args.args[0]) == {"600011", "600012"}   # 不含新鲜的 600010,非全A


# ————————————————————— 5. 开关关闭 → 退回存在性 skip —————————————————————
def test_开关关闭退回存在性skip(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    store.set_active_date(AS_OF)
    _put_raw("600020", [("2026-03-31", "2026-04-20")])   # 过期,但开关关 → 有缓存即不采
    # 600021 无缓存 → 仍采(两种口径都采缺失)
    spy = _spy_fetch(monkeypatch)
    fin.fetch_financial_missing_or_stale(["600020", "600021"],
                                         as_of=AS_OF, reuse_fingerprint=False)
    spy.assert_called_once()
    assert list(spy.call_args.args[0]) == ["600021"]     # 过期的 600020 被存在性 skip 跳过


def test_开关关闭全命中不采(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    store.set_active_date(AS_OF)
    _put_raw("600022", [("2026-03-31", "2026-04-20")])
    spy = _spy_fetch(monkeypatch)
    out = fin.fetch_financial_missing_or_stale(["600022"], as_of=AS_OF, reuse_fingerprint=False)
    spy.assert_not_called()
    assert out == {}


# ————————————————————— 6. 法定披露日历边界 —————————————————————
def test_expected_latest_report_period_日历边界():
    f = fin.expected_latest_report_period
    assert f("2026-01-15") == "2025-09-30"   # 年初:去年年报未到法定截止 → 去年三季报
    assert f("2026-04-29") == "2025-09-30"   # 一季报截止前一天
    assert f("2026-04-30") == "2026-03-31"   # 一季报截止 → 一季报(年报同期到但期更旧)
    assert f("2026-08-30") == "2026-03-31"   # 中报截止前一天
    assert f("2026-08-31") == "2026-06-30"   # 中报截止 → 中报
    assert f("2026-10-30") == "2026-06-30"   # 三季报截止前一天
    assert f("2026-10-31") == "2026-09-30"   # 三季报截止 → 三季报
    assert f("2026-12-31") == "2026-09-30"   # 年末仍是三季报(年报次年 4/30 才法定到期)


# ————————————————————— 7. 空池 / 缓存无可见期 —————————————————————
def test_空池直接返回(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    spy = _spy_fetch(monkeypatch)
    assert fin.fetch_financial_missing_or_stale([], as_of=AS_OF) == {}
    spy.assert_not_called()


def test_缓存无可见报告期触发采集(monkeypatch, tmp_path):
    """有 raw 文件但其报告期披露日全在 as_of 之后(无可见期)→ 判需采(不误判新鲜)。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    store.set_active_date(AS_OF)
    _put_raw("600030", [("2026-06-30", "2026-10-01")])   # 仅一期且披露在未来 → 无可见
    spy = _spy_fetch(monkeypatch)
    fin.fetch_financial_missing_or_stale(["600030"], as_of=AS_OF, reuse_fingerprint=True)
    spy.assert_called_once()
    assert list(spy.call_args.args[0]) == ["600030"]
