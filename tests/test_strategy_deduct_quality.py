"""扣非质量主筛(#31)单测。

断言锁住"为什么这么设计"的语义(守则6:防未来重写误删规则):
  · 因子方向(五维越大越好 → 综合分越高 → 排名越前)
  · 单期质量领先度 = 扣非增速 − 归母增速;任一缺 → None
  · 管线注入的跨期质量领先度**覆盖**单期现算值
  · **缺维重归一**(缺某维 → 权重摊到可用维,不把缺维当中性 0 稀释;不塌缩成 0)
  · 某维横截面有效样本 <2 → 该维退出(不给全体记 0)
  · 全维缺失 → 不参与召回(不是给 0 分)
  · 可交易性门(ST/停牌/低成交额/低流通市值/无流动性数据)分类计数;apply_liquidity=False 可关
  · 样本 <2 降级、空 records 不炸、top_k 生效
  · 注册为「选股」策略;管线跨期领先度锚披露日(防未来函数)
"""
from __future__ import annotations

import math

from tools.strategy import deduct_quality as dq
from tools.strategy import registry as reg


def _derived(kf=None, kfz=None, cfo=None, gm_pct=None, gm_growth=None):
    """构造 financial.derived 子集。kf=扣非增速, kfz=扣非占归母, cfo=现金含量, gm_pct=毛利率,
    gm_growth=归母净利增速(供单期领先度)。"""
    d = {}
    if kf is not None:
        d["扣非净利增速"] = kf
    if kfz is not None:
        d["扣非占归母"] = kfz
    if cfo is not None:
        d["现金含量_CFO比净利"] = cfo
    if gm_pct is not None:
        d["毛利率"] = gm_pct
    if gm_growth is not None:
        d["归母净利增速"] = gm_growth
    return d


def _rec(derived=None, lead=None, amount_yuan=1e9, mktcap_yi=100.0, name=None,
         with_snapshot=True):
    """构造中心记录:financial.derived + 可选注入领先度 + snapshot + valuation。"""
    rec = {
        "meta": {"code": "999999", "name": name},
        "valuation": {"mktcap_yi": mktcap_yi},
        "financial": {"derived": derived or {}},
    }
    if with_snapshot:
        rec["snapshot"] = {"pct_chg": 0.0, "amount": amount_yuan, "close": 10.0}
    if lead is not None:
        rec["扣非质量"] = {"质量领先度": lead}
    return rec


# ————————————————————————————— 纯因子函数 —————————————————————————————
def test_single_period_lead_direction():
    """单期质量领先度 = 扣非增速 − 归母增速;扣非快于归母 → 正。"""
    assert dq.single_period_lead(_derived(kf=50, gm_growth=30)) == 20.0
    assert dq.single_period_lead(_derived(kf=10, gm_growth=40)) == -30.0


def test_single_period_lead_missing_is_none():
    """任一增速缺 → None(不塌缩成 0)。"""
    assert dq.single_period_lead(_derived(kf=50)) is None
    assert dq.single_period_lead(_derived(gm_growth=30)) is None
    assert dq.single_period_lead({}) is None


def test_core_factors_missing_stay_none():
    """核心 4 维缺 → None,不冒充 0。"""
    f = dq.core_factors(_derived(kf=10))
    assert f["扣非增速"] == 10.0
    assert f["扣非占归母"] is None
    assert f["现金含量"] is None
    assert f["毛利率"] is None


def test_injected_lead_overrides_single_period():
    """管线注入的跨期领先度覆盖 derived 单期现算值。"""
    rec = _rec(_derived(kf=50, gm_growth=30), lead=99.0)   # 单期=20,注入=99
    assert dq.factors_of(rec)["质量领先度"] == 99.0
    rec2 = _rec(_derived(kf=50, gm_growth=30))             # 无注入 → 单期兜底
    assert dq.factors_of(rec2)["质量领先度"] == 20.0


# ————————————————————————————— 排序方向 —————————————————————————————
def _mono_records():
    """3 票,每维 A>B>C(全维越大越好)→ 期望排名 A,B,C。"""
    return {
        "A": _rec(_derived(kf=90, kfz=1.0, cfo=1.5, gm_pct=60, gm_growth=40)),  # 单期lead=50
        "B": _rec(_derived(kf=50, kfz=0.8, cfo=1.0, gm_pct=40, gm_growth=30)),  # lead=20
        "C": _rec(_derived(kf=10, kfz=0.6, cfo=0.5, gm_pct=20, gm_growth=5)),   # lead=5
    }


def test_ranking_direction_monotonic():
    """五维全部越大越好:综合分单调 → A 第一、C 末位。"""
    out = dq.combo_deduct_quality_screen(_mono_records(), top_k=3)
    assert out["codes"] == ["A", "B", "C"]
    scores = {d["code"]: d["综合分"] for d in out["因子明细"]}
    assert scores["A"] > scores["B"] > scores["C"]


def test_top_k_truncates():
    out = dq.combo_deduct_quality_screen(_mono_records(), top_k=2)
    assert out["codes"] == ["A", "B"]
    assert out["top_k"] == 2


# ————————————————————————————— 缺维重归一(核心纪律) —————————————————————————————
def test_missing_dim_renormalizes_not_zero():
    """缺某维 → 复合在**可用维**上重归一(= 可用维 z 的加权均值),
    不把缺维当中性 0 稀释(否则会被 /D 拉小)。"""
    recs = _mono_records()
    # B 缺毛利率一维(其余 4 维不变)
    del recs["B"]["financial"]["derived"]["毛利率"]
    out = dq.combo_deduct_quality_screen(recs, top_k=3)
    row_b = next(d for d in out["因子明细"] if d["code"] == "B")
    assert row_b["参与维数"] == 4                     # 只 4 维参与
    assert row_b["毛利率_z"] is None
    # 综合分 == 4 个可用维 z 的均值(等权重归一),而非 (4 z 之和)/5
    zs = [row_b[f"{dim}_z"] for dim in
          ("扣非增速", "扣非占归母", "现金含量", "质量领先度")]
    assert all(z is not None for z in zs)
    # 重归一:综合分 = 可用维 z 均值(/4),而不是缺维当 0 稀释后的 /5
    assert math.isclose(row_b["综合分"], sum(zs) / 4, abs_tol=1e-3)
    assert not math.isclose(row_b["综合分"], sum(zs) / 5, abs_tol=1e-3)


def test_dim_with_single_value_drops_out():
    """某维横截面只有 1 只有效 → 无法标准化 → 该维退出所有票(视为全体缺维)。"""
    recs = {
        "A": _rec(_derived(kf=90, kfz=1.0, cfo=1.5, gm_growth=40)),   # 无毛利率
        "B": _rec(_derived(kf=50, kfz=0.8, cfo=1.0, gm_growth=30)),   # 无毛利率
        "C": _rec(_derived(kf=10, kfz=0.6, cfo=0.5, gm_pct=20, gm_growth=5)),  # 唯一有毛利率
    }
    out = dq.combo_deduct_quality_screen(recs, top_k=3)
    # 毛利率仅 C 一只有效 → 该维退出,所有票毛利率_z 记 None
    for d in out["因子明细"]:
        assert d["毛利率_z"] is None


def test_all_dims_missing_excluded():
    """全维缺失(空 derived + 无领先度)→ 不参与召回,计入'全维缺失'跳过。"""
    recs = _mono_records()
    recs["Z"] = _rec({})                              # 空 derived,单期领先度也 None
    out = dq.combo_deduct_quality_screen(recs, top_k=5)
    assert "Z" not in out["codes"]
    assert out["跳过"].get("全维缺失", 0) >= 1


# ————————————————————————————— 可交易性门 —————————————————————————————
def test_liquidity_gate_st_skipped():
    recs = _mono_records()
    recs["A"]["meta"]["name"] = "*ST测试"
    out = dq.combo_deduct_quality_screen(recs, top_k=5, exclude_st=True)
    assert "A" not in out["codes"]
    assert out["跳过"].get("ST", 0) == 1


def test_liquidity_gate_suspended_no_snapshot():
    recs = _mono_records()
    recs["A"].pop("snapshot")
    out = dq.combo_deduct_quality_screen(recs, top_k=5)
    assert "A" not in out["codes"]
    assert out["跳过"].get("停牌或无快照", 0) == 1


def test_liquidity_gate_low_amount():
    recs = _mono_records()
    recs["A"]["snapshot"]["amount"] = 1e6              # 100 万元 < 5000 万门槛
    out = dq.combo_deduct_quality_screen(recs, top_k=5, min_amount_wan=5000)
    assert "A" not in out["codes"]
    assert out["跳过"].get("低成交额", 0) == 1


def test_liquidity_gate_low_mktcap():
    recs = _mono_records()
    recs["A"]["valuation"]["mktcap_yi"] = 5.0          # 5 亿 < 20 亿门槛
    out = dq.combo_deduct_quality_screen(recs, top_k=5, min_mktcap_yi=20)
    assert "A" not in out["codes"]
    assert out["跳过"].get("低流通市值", 0) == 1


def test_liquidity_gate_no_data():
    recs = _mono_records()
    recs["A"]["snapshot"]["amount"] = None
    recs["A"]["valuation"]["mktcap_yi"] = None
    out = dq.combo_deduct_quality_screen(recs, top_k=5)
    assert "A" not in out["codes"]
    assert out["跳过"].get("无流动性数据", 0) == 1


def test_apply_liquidity_false_bypasses_gate():
    """回测隔离因子 IC 时 apply_liquidity=False → 不施加可交易性门。"""
    recs = _mono_records()
    recs["A"]["meta"]["name"] = "*ST测试"
    recs["B"].pop("snapshot")
    out = dq.combo_deduct_quality_screen(recs, top_k=5, apply_liquidity=False)
    assert set(out["codes"]) == {"A", "B", "C"}
    assert out["跳过"] == {}


# ————————————————————————————— 降级 / 边界 —————————————————————————————
def test_sample_lt_2_degrades():
    out = dq.combo_deduct_quality_screen(
        {"A": _rec(_derived(kf=90, kfz=1.0, cfo=1.5, gm_pct=60, gm_growth=40))}, top_k=5)
    assert out["codes"] == []
    assert "note" in out


def test_empty_records_no_crash():
    out = dq.combo_deduct_quality_screen({}, top_k=5)
    assert out["codes"] == []
    assert out["有效样本"] == 0


def test_registered_as_screen_strategy():
    meta = reg.get("扣非质量")
    assert meta.kind == "选股"
    assert meta.fn is dq.combo_deduct_quality_screen


# ————————————————————————————— 管线:跨期领先度防未来函数 —————————————————————————————
def test_quality_lead_asof_respects_disclosure(monkeypatch):
    """管线 quality_lead_asof 只取披露日 ≤ as_of 的报告期(防未来函数)。"""
    from tools.pipeline import screen_deduct_quality as sp

    # 两期:2025-06-30(披露 2025-08-01,可见)、2025-09-30(披露 2025-10-30,as_of 前不可见)
    fake_raw = {
        "periods": {
            "2024-06-30": {"disclosure_date": "2024-08-01",
                           "利润表": {"营业总收入": 100, "营业成本": 60,
                                    "归母净利润": 10, "扣非归母净利润": 9}},
            "2025-06-30": {"disclosure_date": "2025-08-01",
                           "利润表": {"营业总收入": 200, "营业成本": 100,
                                    "归母净利润": 20, "扣非归母净利润": 30}},
            "2024-09-30": {"disclosure_date": "2024-10-30",
                           "利润表": {"营业总收入": 150, "营业成本": 90,
                                    "归母净利润": 15, "扣非归母净利润": 14}},
            "2025-09-30": {"disclosure_date": "2025-10-30",
                           "利润表": {"营业总收入": 300, "营业成本": 150,
                                    "归母净利润": 30, "扣非归母净利润": 45}},
        }
    }
    monkeypatch.setattr(sp.fin, "load_financial", lambda code: fake_raw)
    # as_of 在 2025-09-30 披露之前 → 只用 2025-06-30(及更早)期
    lead_before = sp.quality_lead_asof("999999", as_of="2025-09-01", n=5)
    # as_of 在 2025-09-30 披露之后 → 纳入该期(领先度均值随之变化)
    lead_after = sp.quality_lead_asof("999999", as_of="2025-11-01", n=5)
    assert lead_before is not None and lead_after is not None
    assert lead_before != lead_after                  # 未披露期不得泄漏进 as_of 前的取值


def test_quality_lead_asof_missing_raw_none(monkeypatch):
    from tools.pipeline import screen_deduct_quality as sp

    def _raise(code):
        raise FileNotFoundError(code)

    monkeypatch.setattr(sp.fin, "load_financial", _raise)
    assert sp.quality_lead_asof("999999", as_of="2025-09-01") is None
