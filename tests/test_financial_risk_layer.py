"""财报高危红旗风险层单测(WI-6 · feat/redflag-risk-layer)。

锁死语义(为什么改——防未来重写误删规则):
  - 红旗作**风险/减仓/警示层**,非选股 alpha 源:验值坐实高危红旗票 60 日崩盘率约 2 倍
    (finval 用 flags.has_high_severity 口径);评分无 alpha。故此层只做风险标注,不改选股逻辑。
  - **高危口径**:严重度=「高」的红旗即高危(与 has_high_severity 一致)——
    通用高危(扣非为负/非标审计意见/审计机构未备案)+ 行业专家专属高危(flags_detail 带 severity)。
  - 审计闸门补挂的「非标审计意见/审计机构未备案」只进轻量 flags 列表(未进 flags_detail),
    双源取并仍须命中(源2:轻量 flags 里 config 严重度=高)。
  - **优雅降级**:无财报数据(约 175 只之外)或仅中/低红旗 → risk=None,前端不渲染标注。
  - 选股页 自选股 / 综合选股 / 各在产策略行 均挂 risk;命中高危票 route 渲染出「⚠财报高危」。
  - **未改选股逻辑/strategy.json**:仅展示层就地补键,不参与打分排序(融合降权留 TODO)。
  - 非投资建议:风险提示 ≠ 卖出建议。
data-independent(monkeypatch _load_all / 直接构造 financial 块,不触网、不读盘)。
"""
from fastapi.testclient import TestClient

from tools.analysis import council
from web import data_access as da
from web.app import app

client = TestClient(app)


# ————————————————————————————————————————————————
# financial_risk:高危口径 + 双源取并 + 优雅降级
# ————————————————————————————————————————————————
def _rec_with_fin(code, financial):
    """最小 record:meta + financial 块(+ council 供选股页装配)。"""
    rec = {"meta": {"code": code, "name": "T" + code, "sector": "半导体",
                    "industry": "芯片", "as_of": "2026-08-08"},
           "signals": {}, "financial": financial}
    rec["council"] = council.build_council_block(rec)
    return rec


def test_no_financial_block_returns_none():
    """无财报数据 → None(约 175 只之外的票优雅无标注)。"""
    assert da.financial_risk({"meta": {"code": "000001"}}) is None
    assert da.financial_risk({"financial": None}) is None
    assert da.financial_risk(None) is None


def test_only_mid_low_flags_returns_none():
    """仅中/低严重度红旗 → 非高危 → None(不误标)。"""
    fin = {"报告期": "2026-03-31", "评级": "中",
           "flags": ["商誉高企", "高负债"],
           "flags_detail": [{"code": "商誉高企", "命中": True, "严重度": "中", "值": {}},
                            {"code": "高负债", "命中": True, "严重度": "中", "值": {}}]}
    assert da.financial_risk({"financial": fin}) is None


def test_high_severity_flag_in_detail_flagged():
    """flags_detail 里 severity=高(扣非为负)→ high_risk,带红旗名/评级/报告期。"""
    fin = {"报告期": "2026-03-31", "评级": "风险",
           "flags": ["应收存货激增", "扣非为负"],
           "flags_detail": [{"code": "应收存货激增", "命中": True, "严重度": "中", "值": {}},
                            {"code": "扣非为负", "命中": True, "严重度": "高", "值": {"扣非归母净利润": -1e8}}]}
    risk = da.financial_risk({"financial": fin})
    assert risk and risk["high_risk"] is True
    assert risk["flags"] == ["扣非为负"]                 # 只列高危,不含中红旗
    assert risk["评级"] == "风险" and risk["报告期"] == "2026-03-31"
    assert "扣非为负" in risk["label"]


def test_audit_gate_flag_only_in_light_list_flagged():
    """审计闸门补挂的「非标审计意见」只在轻量 flags 列表(未进 flags_detail),
    源2 靠 config 严重度=高 仍须命中(锁双源取并)。"""
    fin = {"报告期": "2025-12-31", "评级": "风险",
           "flags": ["高负债", "非标审计意见"],           # 非标审计意见:analyzer 后补进轻量 flags
           "flags_detail": [{"code": "高负债", "命中": True, "严重度": "中", "值": {}}]}
    risk = da.financial_risk({"financial": fin})
    assert risk and "非标审计意见" in risk["flags"]        # 源2 命中
    assert "高负债" not in risk["flags"]                  # 中红旗不算高危


def test_industry_expert_high_flag_flagged():
    """行业专家专属高危(flags_detail 带 severity=高,如三道红线踩线)同口径纳入。"""
    fin = {"报告期": "2026-06-30", "评级": "风险", "flags": ["三道红线踩线"],
           "flags_detail": [{"code": "三道红线踩线", "命中": True, "严重度": "高", "值": {}}]}
    risk = da.financial_risk({"financial": fin})
    assert risk and risk["flags"] == ["三道红线踩线"]


def test_attach_risk_mutates_rows():
    """_attach_risk 就地给行补 risk 键;无记录/无财报票 → risk=None。"""
    hi = {"报告期": "2026-03-31", "评级": "风险", "flags": ["扣非为负"],
          "flags_detail": [{"code": "扣非为负", "命中": True, "严重度": "高", "值": {}}]}
    recs = {"000001": {"financial": hi}, "000002": {"meta": {}}}   # 000002 无财报
    rows = [{"code": "000001"}, {"code": "000002"}, {"code": "999999"}]  # 999999 无记录
    da._attach_risk(rows, recs)
    assert rows[0]["risk"] and rows[0]["risk"]["high_risk"] is True
    assert rows[1]["risk"] is None and rows[2]["risk"] is None


# ————————————————————————————————————————————————
# 选股页装配:自选股 / 综合选股行携带 risk
# ————————————————————————————————————————————————
def _high_fin():
    return {"报告期": "2026-03-31", "评级": "风险", "flags": ["扣非为负"],
            "flags_detail": [{"code": "扣非为负", "命中": True, "严重度": "高",
                              "值": {"扣非归母净利润": -2e8}}]}


def _patch(monkeypatch, recs, pool=None):
    monkeypatch.setattr(da, "_load_all", lambda date="latest": recs)
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    monkeypatch.setattr(da, "available_dates", lambda: ["2026-08-08"])
    monkeypatch.setattr(da, "_pool_codes", lambda: set(recs.keys()) if pool is None else pool)
    monkeypatch.setattr(da, "_code_name_map", lambda: {})

    def _no_view(name, date="latest"):
        raise FileNotFoundError(name)
    monkeypatch.setattr(da.store, "get_view", _no_view)


def test_selection_pool_row_carries_risk(monkeypatch):
    """自选股行:高危票带 risk.high_risk=True,含红旗名;无财报票 risk=None。"""
    recs = {"000001": _rec_with_fin("000001", _high_fin()),
            "000002": _rec_with_fin("000002", None)}
    _patch(monkeypatch, recs, pool={"000001", "000002"})
    rows = {r["code"]: r for r in da.selection_page()["rows"]}
    assert rows["000001"]["risk"] and rows["000001"]["risk"]["high_risk"] is True
    assert "扣非为负" in rows["000001"]["risk"]["flags"]
    assert rows["000002"]["risk"] is None                 # 无财报优雅无标注


def test_selection_route_renders_risk_badge(monkeypatch):
    """route:高危自选票渲染出「⚠财报高危」标注 + 红旗名 tooltip。"""
    recs = {"000001": _rec_with_fin("000001", _high_fin())}
    _patch(monkeypatch, recs, pool={"000001"})
    r = client.get("/selection")
    assert r.status_code == 200
    # 服务端 macro 渲染的标注 span(双引号,区别于 JS riskBadge 里的单引号字面量)
    assert '<span class="badge risk-flag"' in r.text
    assert "扣非为负" in r.text                            # tooltip 列红旗名


def test_selection_route_no_badge_when_no_high_flag(monkeypatch):
    """route:无高危红旗票不渲染标注(防误标)。"""
    mid = {"报告期": "2026-03-31", "评级": "中", "flags": ["高负债"],
           "flags_detail": [{"code": "高负债", "命中": True, "严重度": "中", "值": {}}]}
    recs = {"000001": _rec_with_fin("000001", mid)}
    _patch(monkeypatch, recs, pool={"000001"})
    r = client.get("/selection")
    # 无高危红旗 → 无服务端渲染的标注 span(JS 里的字面量不算实际渲染)
    assert r.status_code == 200 and '<span class="badge risk-flag"' not in r.text
