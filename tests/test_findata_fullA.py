"""全A 财报覆盖扩展 单测(feat/findata-fullA)。

锁死语义(为什么改,防未来重写时无意删规则):
  1. 增量/幂等/断点续采:select_pending 按新鲜度门控——未采/超龄纳入,新鲜跳过,force 全采。
  2. 回填编排:backfill 只对 need 调 fetch;dry_run 不触网;重跑对已新鲜票天然跳过(幂等)。
  3. 数据供给:screen_council.build_min_record 挂 as_of 财报块(缺 raw → None,财报专家弃权,行为同旧);
     as_of 透传到 build_financial_block(防未来函数锚)。
  4. 红旗接入排序:全A 策略0 排序本身对高危红旗票降权/否决沉底(与 web 同一纯函数),
     无高危/无财报块 → 排序分==综合分、no-op(不回归)。
  5. 高危剂量口径:flags.high_flag_count 双源取并(flags_detail 严重度=高 ∪ 轻量 flags 配高危)。
  6. 防未来函数:财报只按披露日≤as_of 可见(analyzer 已控);本层不引未来数据。

data-independent:store/fetch/universe 全走 monkeypatch,不触网、不读真实 data/raw。
⚠️ 非投资建议。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.analysis.financial import flags as fin_flags
from tools.collectors import financial_backfill as fb
from tools.pipeline import screen_council as sc


# ————————————————————————————————————————————————
# 1) 高危剂量口径:high_flag_count 双源取并
# ————————————————————————————————————————————————
def test_high_flag_count_none_and_empty():
    assert fin_flags.high_flag_count(None) == 0
    assert fin_flags.high_flag_count({}) == 0
    assert fin_flags.high_flag_count({"flags": [], "flags_detail": []}) == 0


def test_high_flag_count_from_flags_detail_severity():
    """源1:flags_detail 里 严重度=高 计数;中/低 不计。"""
    block = {"flags_detail": [
        {"code": "扣非为负", "严重度": "高"},
        {"code": "商誉高企", "严重度": "中"},
        {"code": "三道红线踩线", "严重度": "高"},
    ]}
    assert fin_flags.high_flag_count(block) == 2


def test_high_flag_count_double_source_union_dedup():
    """源2:轻量 flags 里 config 严重度=高但未进 detail 的(如非标审计意见);与源1去重取并。"""
    from tools.config import strategy
    sev = (strategy.THRESHOLDS.get("财报", {}) or {}).get("严重度", {}) or {}
    # 找一个 config 里标为「高」的轻量红旗名做真实口径断言(审计闸门补挂类)
    # 选一个与 flags_detail 里不同名的高危红旗(审计闸门补挂类:非标审计意见/审计机构未备案)
    high_names = [k for k, v in sev.items() if v == "高" and k != "扣非为负"]
    assert high_names, "config 应至少有一个非扣非类高危红旗名(审计闸门补挂)"
    name = high_names[0]
    block = {"flags": [name, "某中危"], "flags_detail": [{"code": "扣非为负", "严重度": "高"}]}
    # 源1 扣非为负(1) ∪ 源2 name(1) = 2;"某中危" 不在 config 高危 → 不计
    assert fin_flags.high_flag_count(block) == 2
    # 去重:同名同时在两源只计一次
    block2 = {"flags": [name], "flags_detail": [{"code": name, "严重度": "高"}]}
    assert fin_flags.high_flag_count(block2) == 1


# ————————————————————————————————————————————————
# 2) 增量 / 幂等 / 断点续采:select_pending
# ————————————————————————————————————————————————
def test_select_pending_incremental_gating(monkeypatch):
    """未采(stale=True)/超龄纳入;新鲜跳过。"""
    fresh = {"000001"}   # 视为新鲜(未超龄)
    monkeypatch.setattr(fb.store, "is_stale",
                        lambda kind, code, max_days: code not in fresh)
    need = fb.select_pending(["000001", "000002", "000003"], max_age_days=45)
    assert need == ["000002", "000003"]   # 000001 新鲜跳过


def test_select_pending_force_takes_all(monkeypatch):
    """force=True 忽略新鲜度,全部纳入(且补零 6 位、保序去重)。"""
    monkeypatch.setattr(fb.store, "is_stale", lambda *a, **k: False)  # 全新鲜
    need = fb.select_pending(["1", "000001", "600519"], force=True)
    assert need == ["000001", "600519"]   # "1"→"000001" 与显式 000001 去重


def test_select_pending_dedup_preserves_order(monkeypatch):
    monkeypatch.setattr(fb.store, "is_stale", lambda *a, **k: True)
    assert fb.select_pending(["000002", "000001", "000002"]) == ["000002", "000001"]


# ————————————————————————————————————————————————
# 3) 回填编排:backfill_financial(dry_run / 只采 need / 幂等)
# ————————————————————————————————————————————————
def test_backfill_dry_run_no_network(monkeypatch):
    """dry_run:只算规模/耗时,绝不调 fetch_financial。"""
    monkeypatch.setattr(fb.store, "set_active_date", lambda d: None)
    monkeypatch.setattr(fb.store, "is_stale", lambda *a, **k: True)   # 全部需采
    called = {"n": 0}
    monkeypatch.setattr(fb.fin, "fetch_financial",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {})
    stat = fb.backfill_financial(codes=["000001", "000002"], as_of="2026-08-08", dry_run=True)
    assert called["n"] == 0                      # 未触网
    assert stat["need"] == 2 and stat["dry_run"] is True
    assert "est_sleep_minutes" in stat


def test_backfill_only_fetches_need_and_counts(monkeypatch):
    """只对 need 调 fetch;ok/failed 由 fetch 返回统计(采到几只算 ok)。"""
    monkeypatch.setattr(fb.store, "set_active_date", lambda d: None)
    stale = {"000002", "000003"}                 # 000001 新鲜跳过
    monkeypatch.setattr(fb.store, "is_stale",
                        lambda kind, code, md: code in stale)
    seen = {}

    def fake_fetch(part, as_of=None):
        seen["part"] = list(part)
        seen["as_of"] = as_of
        return {"000002": {"code": "000002"}}    # 只 000002 采成功,000003 失败(降级)

    monkeypatch.setattr(fb.fin, "fetch_financial", fake_fetch)
    stat = fb.backfill_financial(codes=["000001", "000002", "000003"], as_of="2026-08-08")
    assert seen["part"] == ["000002", "000003"]  # 000001 未进 fetch(新鲜跳过)
    assert seen["as_of"] == "2026-08-08"         # as_of 透传(元数据锚)
    assert stat["fresh_skipped"] == 1 and stat["need"] == 2
    assert stat["ok"] == 1 and stat["failed"] == 1


def test_backfill_idempotent_second_run_skips(monkeypatch):
    """幂等/断点续采:采过的票下轮新鲜 → 不再 fetch(need=0)。"""
    monkeypatch.setattr(fb.store, "set_active_date", lambda d: None)
    fetched: set[str] = set()
    # is_stale:采过的(在 fetched)视为新鲜;否则陈旧
    monkeypatch.setattr(fb.store, "is_stale",
                        lambda kind, code, md: code not in fetched)

    def fake_fetch(part, as_of=None):
        for c in part:
            fetched.add(c)
        return {c: {"code": c} for c in part}

    monkeypatch.setattr(fb.fin, "fetch_financial", fake_fetch)
    s1 = fb.backfill_financial(codes=["000001", "000002"], as_of="2026-08-08")
    assert s1["need"] == 2 and s1["ok"] == 2
    s2 = fb.backfill_financial(codes=["000001", "000002"], as_of="2026-08-08")
    assert s2["need"] == 0 and s2["ok"] == 0     # 第二轮全新鲜跳过


def test_backfill_universe_default_and_limit(monkeypatch):
    """codes=None → 走 universe.universe_codes(limit 透传);不触网。"""
    monkeypatch.setattr(fb.store, "set_active_date", lambda d: None)
    monkeypatch.setattr(fb.store, "is_stale", lambda *a, **k: False)  # 全新鲜 → need=0
    seen = {}
    monkeypatch.setattr(fb.universe, "universe_codes",
                        lambda limit=None, exclude_bj=True: seen.update(
                            {"limit": limit, "bj": exclude_bj}) or ["000001", "000002"])
    monkeypatch.setattr(fb.fin, "fetch_financial", lambda *a, **k: {})
    stat = fb.backfill_financial(codes=None, as_of="2026-08-08", limit=500)
    assert seen == {"limit": 500, "bj": True}
    assert stat["universe"] == 2


# ————————————————————————————————————————————————
# 4)+5) 数据供给 + 红旗接入排序:screen_council
# ————————————————————————————————————————————————
def _kline(n=120, trend=0.0, start=10.0, seed=0):
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


def test_build_min_record_attaches_financial_and_threads_as_of(monkeypatch):
    """build_min_record:挂 financial 块 + as_of 透传到 build_financial_block;不塞 meta.as_of。"""
    seen = {}

    def fake_block(code, as_of=None, industry=None, sector=None):
        seen["as_of"] = as_of
        return {"评级": "良", "flags": [], "flags_detail": []}

    monkeypatch.setattr(sc.board, "board_of", lambda code: None)
    from tools.analysis.financial import analyzer as fr_analyzer
    monkeypatch.setattr(fr_analyzer, "build_financial_block", fake_block)
    rec = sc.build_min_record("000001", _kline(trend=0.1, seed=1), as_of="2026-08-08")
    assert rec["financial"] == {"评级": "良", "flags": [], "flags_detail": []}
    assert seen["as_of"] == "2026-08-08"          # 防未来函数锚透传
    assert "as_of" not in rec["meta"]             # 不塞 meta.as_of(事件驱动专家仍弃权)


def test_build_min_record_none_financial_when_absent(monkeypatch):
    """无财报 raw(build_financial_block 抛/返 None)→ financial=None,财报专家弃权(行为同旧)。"""
    monkeypatch.setattr(sc.board, "board_of", lambda code: None)
    from tools.analysis.financial import analyzer as fr_analyzer
    monkeypatch.setattr(fr_analyzer, "build_financial_block",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    rec = sc.build_min_record("000001", _kline(trend=0.1, seed=1), as_of="2026-08-08")
    assert rec["financial"] is None
    from tools.analysis import council
    cblk = council.build_council_block(rec, None)
    conf = {e["专家"]: e["置信度"] for e in cblk["experts"]}
    assert conf["财报"] == 0.0                     # 无块 → 财报专家弃权


def _patch_blocks(monkeypatch, blocks: dict):
    """让 build_financial_block 按 code 返回预置块(缺 → None)。"""
    monkeypatch.setattr(sc.board, "board_of", lambda code: None)
    monkeypatch.setattr(sc.store, "set_active_date", lambda d: None)
    from tools.analysis.financial import analyzer as fr_analyzer
    monkeypatch.setattr(fr_analyzer, "build_financial_block",
                        lambda code, as_of=None, industry=None, sector=None: blocks.get(code))


def test_redflag_demotes_high_risk_in_fullA_ranking(monkeypatch):
    """默认(降权)模式:高危红旗票在全A排序本身被降权(排序分=综合分−罚分)+ 带 财报风险 标注。

    两票同 K 线走势(base 综合分相近),坏票挂高危红旗块 → 排序分被扣、财报覆盖统计到 2。
    """
    kmap = {"000001": _kline(trend=0.12, seed=2),   # 干净
            "000009": _kline(trend=0.12, seed=2)}   # 同走势但高危红旗
    monkeypatch.setattr(sc.market, "load_kline", lambda code: kmap[code])
    _patch_blocks(monkeypatch, {
        "000001": {"评级": "良", "flags": [], "flags_detail": []},
        "000009": {"评级": "风险", "flags": ["扣非为负"],
                   "flags_detail": [{"code": "扣非为负", "严重度": "高"}]},
    })
    captured = {}
    monkeypatch.setattr(sc.store, "put_view",
                        lambda name, obj, date=None: captured.update({"obj": obj}) or "p")
    v = sc.run_council_screen(["000001", "000009"], as_of="2026-08-08", fetch=False)
    assert v["财报覆盖"] == 2                        # 两票都挂到 as_of 财报块
    assert v["命中高危红旗"] == 1
    rows = {r["code"]: r for r in v["top"]}
    bad = rows["000009"]
    assert bad["财报风险"] is not None and bad["财报风险"]["高危数"] == 1
    assert bad["财报风险"]["罚分"] == 0.5
    # 降权:排序分 = 综合分 − 罚分(接入排序生效于全A排序本身)
    assert bad["排序分"] == pytest.approx(bad["综合分"] - 0.5, abs=1e-6)
    good = rows["000001"]
    assert good["财报风险"] is None and good["排序分"] == pytest.approx(good["综合分"], abs=1e-6)
    # 高危票排在干净票之后(沉底方向正确)
    order = [r["code"] for r in v["top"]]
    assert order.index("000009") > order.index("000001")


def test_redflag_veto_mode_sinks_to_bottom(monkeypatch):
    """否决模式:高危票强制沉底(否决标记),排在无否决票之后;保留展示(不剔除)。"""
    kmap = {"000001": _kline(trend=-0.2, seed=7),    # 干净但走势弱(综合分低)
            "000009": _kline(trend=0.2, seed=8)}     # 走势强(综合分高)但高危红旗被否决
    monkeypatch.setattr(sc.market, "load_kline", lambda code: kmap[code])
    _patch_blocks(monkeypatch, {
        "000009": {"评级": "风险", "flags": ["扣非为负"],
                   "flags_detail": [{"code": "扣非为负", "严重度": "高"}]},
    })
    # 临时切财报轴否决模式(不改 config 文件,monkeypatch 纯函数读的 cfg)。
    # 汇聚器(risk_veto_adjust)财报轴读 redflag_cfg();龙虎榜轴无快照→不发声,只财报轴否决沉底。
    from tools.config import strategy as _strat
    monkeypatch.setattr(_strat, "redflag_cfg",
                        lambda: {"启用": True, "模式": "否决", "每面罚分": 0.5,
                                 "罚分上限": 1.2, "否决沉底保留展示": True})
    monkeypatch.setattr(sc.store, "set_active_date", lambda d: None)
    monkeypatch.setattr(sc.store, "put_view", lambda name, obj, date=None: "p")
    v = sc.run_council_screen(["000009", "000001"], as_of="2026-08-08", fetch=False)
    order = [r["code"] for r in v["top"]]
    assert order[-1] == "000009"                     # 否决沉底(即便走势强也排最后)
    bad = next(r for r in v["top"] if r["code"] == "000009")
    assert bad["财报风险"]["否决"] is True and bad["财报风险"]["剔除"] is False


def test_redflag_noop_when_no_financial_regression(monkeypatch):
    """回归:无财报块(financial=None)→ 财报风险=None、红旗层零罚分(排序分==入排序基准分)。

    为什么断言的是「排序分 == 综合分_收缩」而不是「== 综合分」(锁"为什么改"):
      本测试锁的语义是**红旗接入排序的 no-op**——没有高危红旗就不应该被红旗层扣一分钱。
      排序分的公式是 `排序分 = 入排序基准分 − Σ罚分`,而**入排序基准分不是综合分**:
      自 1a37f6f「弃权软收缩接入策略0排序」起,基准分改成了 `综合分_收缩`
      (config『合议.弃权置信度标注.收缩启用』,诊断源 docs/每日分析/策略建议/合议专家
      弃权置信度标注.md)——发声专家 < 收缩门槛 的票,综合分先向中性软收缩再进排序。
      本用例两票只有技术类专家发声(参与2 < 门槛3)→ 必然被收缩,于是老写法
      `排序分 == 综合分` 从那次接入起**恒假**、与红旗层无关;继续这么写等于用一条必红的
      断言掩盖真正要保护的 no-op 语义。
      → 现在:红旗层 no-op 用「排序分 == 综合分_收缩 且 罚分层未发声(财报风险=None)」锁,
        收缩层是否生效由下面单独一条断言显式承认(而不是混进红旗层断言里)。
    """
    kmap = {"000001": _kline(trend=0.15, seed=2),
            "000002": _kline(trend=-0.15, seed=3)}
    monkeypatch.setattr(sc.market, "load_kline", lambda code: kmap[code])
    _patch_blocks(monkeypatch, {})   # 无任何块 → 全 None
    monkeypatch.setattr(sc.store, "put_view", lambda name, obj, date=None: "p")
    v = sc.run_council_screen(["000001", "000002"], as_of="2026-08-08", fetch=False)
    assert v["财报覆盖"] == 0 and v["命中高危红旗"] == 0
    for r in v["top"]:
        assert r["财报风险"] is None                              # 两轴都没发声 → 无风险标注
        # 红旗层 no-op:排序分 == 入排序基准分(= 综合分_收缩),一分钱罚分都没扣
        assert r["排序分"] == pytest.approx(r["综合分_收缩"], abs=1e-9)
        # 收缩层确实是唯一造成「排序分 != 综合分」的层:两票均因发声专家过少被标低置信度
        if r["排序分"] != pytest.approx(r["综合分"], abs=1e-9):
            assert r["低合议置信度"] is True and r["参与专家数"] < 3
    scores = [r["综合分"] for r in v["top"]]
    assert scores == sorted(scores, reverse=True)    # 与旧降序一致
