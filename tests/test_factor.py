"""多因子(F6)单测:原始因子提取 / 截面标准化+合成 / 分层单调性 / 「多因子」专家信封。

锁语义:子指标方向定向(越小越好翻转)、缺因子降级(不崩)、综合分位→强度符号、
专家产出过 F1 校验、未预算→弃权缺失。
"""
import pandas as pd
import pytest

from tools.analysis import experts
from tools.analysis.factor import factor as fac
from tools.analysis.factor import score as sc
from tools.contracts.expert import validate_verdict


def _rec(code, roe=None, gm=None, debt=None, pe=None, pb=None, growth=None):
    return {"meta": {"code": code},
            "fundamental": {"ROE": roe, "毛利率": gm, "负债率": debt, "净利增速": growth},
            "valuation": {"pe_ttm": pe, "pb": pb}}


# ---------- factor.raw_factors / 低波 ----------
def test_raw_factors_extract():
    r = fac.raw_factors(_rec("A", roe=15, gm=30, debt=40, pe=12, pb=1.5, growth=20))
    assert r["ROE"] == 15 and r["负债率"] == 40 and r["PE_TTM"] == 12 and r["净利增速"] == 20
    assert r["股息率"] is None and r["北向净流入趋势"] is None       # 记录无→缺失
    assert r["年化波动率"] is None                                    # 无 kline


def test_annualized_vol():
    closes = [100 * (1.01 ** i) for i in range(80)]                  # 稳定上行,低波
    df = pd.DataFrame({"close": closes})
    v = fac.annualized_vol(df, win=60)
    assert v is not None and v >= 0
    assert fac.annualized_vol(df.head(5), win=60) is None            # 样本不足→None


# ---------- 股息率 = 每股股利 / 最新收盘 × 100(锁 真0 vs 缺失) ----------
def test_dividend_yield_positive():
    df = pd.DataFrame({"close": [9.0, 10.0]})                        # 最新收盘 10
    assert fac.dividend_yield(0.5, df) == pytest.approx(5.0)         # 0.5/10×100

def test_dividend_yield_zero_is_real_not_missing():
    df = pd.DataFrame({"close": [10.0]})
    assert fac.dividend_yield(0.0, df) == 0.0                        # 无分红=真 0,非 None
    assert fac.dividend_yield(0, df) == 0.0

def test_dividend_yield_missing_dps_is_none():
    df = pd.DataFrame({"close": [10.0]})
    assert fac.dividend_yield(None, df) is None                      # 采集缺失→None

def test_dividend_yield_no_price_degrades_none():
    assert fac.dividend_yield(0.5, None) is None                     # 有分红但无价格分母→缺失
    assert fac.dividend_yield(0.5, pd.DataFrame({"close": []})) is None
    # 无分红即便无价也仍是真 0(不需价格)
    assert fac.dividend_yield(0.0, None) == 0.0

def test_raw_factors_dividend_availability():
    """含每股股利 + kline → 股息率可得(>0);无分红票 → 0(仍可得,availability 计入)。"""
    df = pd.DataFrame({"close": [9.0, 10.0]})
    rec_pay = {"meta": {"code": "P"}, "fundamental": {"每股股利": 0.5}, "valuation": {}}
    rec_nopay = {"meta": {"code": "N"}, "fundamental": {"每股股利": 0.0}, "valuation": {}}
    r_pay = fac.raw_factors(rec_pay, df)
    r_nopay = fac.raw_factors(rec_nopay, df)
    assert r_pay["股息率"] == pytest.approx(5.0)
    assert r_nopay["股息率"] == 0.0                                  # 真 0,非 None → availability 命中
    # availability 用 is not None 判定:有分红与无分红都算"可得"
    from tools.analysis.factor import score as sc
    avail = sc._availability({"P": r_pay, "N": r_nopay})
    assert avail["股息率"] == pytest.approx(1.0)


# ---------- 新增因子:筹码 / 预期 / 主力(借鉴 a-stock-data)----------
def test_raw_factors_new_blocks_extract():
    rec = {"meta": {"code": "A"}, "fundamental": {}, "valuation": {},
           "chip": {"获利比例": 0.3, "集中度90": 0.12},
           "consensus": {"预期增速": 0.25},
           "holder": {"户数环比": -3.5, "连续减少期数": 4}}
    r = fac.raw_factors(rec)
    assert r["获利比例"] == 0.3 and r["筹码集中度"] == 0.12
    assert r["预期增速"] == 0.25
    assert r["股东户数环比"] == -3.5 and r["户数连续减少期数"] == 4


def test_raw_factors_new_blocks_missing_degrade():
    r = fac.raw_factors(_rec("A"))          # 无 chip/consensus/holder 块
    for k in ("获利比例", "筹码集中度", "预期增速", "股东户数环比", "户数连续减少期数"):
        assert r[k] is None                 # 缺块 → None 降级(不崩)


def test_cross_section_new_factor_direction():
    """低获利比例/高集中(小)/高预期增速/户数减少 → 综合分更高(方向定向生效)。"""
    def _mk(code, win, conc, growth, hchg, streak):
        return {"meta": {"code": code}, "fundamental": {}, "valuation": {},
                "chip": {"获利比例": win, "集中度90": conc},
                "consensus": {"预期增速": growth},
                "holder": {"户数环比": hchg, "连续减少期数": streak}}
    raw = {
        "优": fac.raw_factors(_mk("优", 0.2, 0.08, 0.40, -5.0, 5)),   # 深套/锁仓/高预期/吸筹
        "劣": fac.raw_factors(_mk("劣", 0.9, 0.30, 0.02, 4.0, 0)),    # 高获利盘/分散/低预期/散户化
    }
    out = sc.cross_section(raw)
    assert out["优"]["综合分"] > out["劣"]["综合分"]
    assert out["优"]["方向"] == "看多" and out["劣"]["方向"] == "看空"


# ---------- score.cross_section:定向 / 合成 / 降级 ----------
def test_cross_section_orientation_and_direction():
    raw = {
        "好": fac.raw_factors(_rec("好", roe=25, gm=40, debt=20, pe=8, pb=1.0)),   # 高ROE低PE=好
        "中": fac.raw_factors(_rec("中", roe=12, gm=25, debt=45, pe=18, pb=2.0)),
        "差": fac.raw_factors(_rec("差", roe=4, gm=12, debt=70, pe=40, pb=5.0)),   # 低ROE高PE=差
    }
    out = sc.cross_section(raw)
    assert out["好"]["综合分"] > out["中"]["综合分"] > out["差"]["综合分"]  # 好>中>差
    assert out["好"]["方向"] == "看多" and out["好"]["强度"] > 0
    assert out["差"]["方向"] == "看空" and out["差"]["强度"] < 0
    # 齐全度分层口径:分母只算核心必备因子(质量/价值/低波/成长/股息=5),此处仅质量+价值有数据
    # → 2/5(增强因子恒 None 不进分母、不稀释);仍属部分降级
    assert out["好"]["因子齐全度"] == pytest.approx(2 / 5, abs=1e-3)
    assert out["好"]["数据充分度"] == "部分降级"
    # 低 PE 的"好"在价值因子上分位应高(方向-1 翻转生效)
    assert out["好"]["各因子分位"]["价值"] > out["差"]["各因子分位"]["价值"]


def test_cross_section_all_missing_is_neutral():
    raw = {"X": fac.raw_factors(_rec("X"))}          # 全 None
    out = sc.cross_section(raw)
    assert out["X"]["方向"] == "中性" and out["X"]["强度"] == 0.0
    assert out["X"]["数据充分度"] == "缺失"


# ---------- 齐全度分层:增强因子不稀释置信度、有值能加分(修 PR#19 回归)----------
def _kline_80():
    return pd.DataFrame({"close": [100 * (1.005 ** i) for i in range(80)]})   # ≥61 根,低波可算


def _full_core_rec(code):
    """五个核心必备因子全有数据(质量/价值/成长/股息 from record,低波 from kline)。"""
    return {"meta": {"code": code},
            "fundamental": {"ROE": 15, "毛利率": 30, "负债率": 40, "净利增速": 20, "每股股利": 0.5},
            "valuation": {"pe_ttm": 12, "pb": 1.5}}


def test_completeness_enhancement_none_not_diluted():
    """增强因子(北向/筹码/预期/主力)全 None 时,齐全度 = 只有核心因子的水平,不被恒 None 增强因子稀释。

    锁 PR#19 回归:此前分母含 4 个恒 None 的增强因子 → 5/9≈0.556;修后核心 5/5=1.0(充分)。
    """
    df = _kline_80()
    raw = {"C": fac.raw_factors(_full_core_rec("C"), df)}    # 无 chip/consensus/holder/北向
    out = sc.cross_section(raw)
    assert out["C"]["因子齐全度"] == pytest.approx(1.0, abs=1e-3)   # 核心全齐 → 满,不被增强稀释
    assert out["C"]["数据充分度"] == "充分"
    # 核心缺一(丢股息)→ 4/5=0.8,仍与增强因子无关(不因 4 个 None 增强再往下掉)
    rec4 = _full_core_rec("D")
    rec4["fundamental"].pop("每股股利")
    raw4 = {"D": fac.raw_factors(rec4, df)}
    out4 = sc.cross_section(raw4)
    assert out4["D"]["因子齐全度"] == pytest.approx(4 / 5, abs=1e-3)


def test_completeness_enhancement_present_adds_bonus():
    """增强因子有值时能加分:同一核心水平下,带增强值的齐全度 > 不带增强的。"""
    df = _kline_80()
    rec = _full_core_rec("E")
    rec["fundamental"].pop("每股股利")                       # 核心留 4/5,给加分留头寸(否则封顶 1.0 看不出)
    base = {"E": fac.raw_factors(rec, df)}                   # 无增强 → 4/5=0.8
    rec_enh = dict(rec)
    rec_enh["chip"] = {"获利比例": 0.3, "集中度90": 0.12}    # 筹码增强因子有值
    withenh = {"E": fac.raw_factors(rec_enh, df)}
    c_base = sc.cross_section(base)["E"]["因子齐全度"]
    c_enh = sc.cross_section(withenh)["E"]["因子齐全度"]
    assert c_base == pytest.approx(4 / 5, abs=1e-3)
    assert c_enh > c_base                                    # 增强落地 → 加分
    assert c_enh == pytest.approx((4 + 0.5) / 5, abs=1e-3)   # +0.5 权/核心档


def test_completeness_enhancement_never_exceeds_one():
    """核心全齐 + 增强也有值 → 齐全度封顶 1.0(增强只上不下,绝不下拉核心水平)。"""
    df = _kline_80()
    rec = _full_core_rec("F")
    rec["chip"] = {"获利比例": 0.3, "集中度90": 0.12}
    rec["consensus"] = {"预期增速": 0.2}
    raw = {"F": fac.raw_factors(rec, df)}
    out = sc.cross_section(raw)
    assert out["F"]["因子齐全度"] == pytest.approx(1.0, abs=1e-3)   # 封顶,不 >1


def test_midrank_pctiles():
    p = sc._midrank_pctiles([("a", 1.0), ("b", 2.0), ("c", 3.0)])
    assert p["a"] < p["b"] < p["c"] and 0 < p["a"] < 1


# ---------- 分层单调性 ----------
def test_monotonicity_increasing():
    scores = {c: i for i, c in enumerate("abcde")}
    fwd = {c: i - 2.0 for i, c in enumerate("abcde")}      # 与分同序递增
    r = sc.monotonicity(scores, fwd, layers=5)
    assert r["单调递增"] is True and r["高低价差"] == pytest.approx(4.0)


def test_monotonicity_broken():
    scores = {c: i for i, c in enumerate("abcde")}
    fwd = {"a": 5, "b": 1, "c": 3, "d": 0, "e": 2}         # 打乱
    r = sc.monotonicity(scores, fwd, layers=5)
    assert r["单调递增"] is False


# ---------- 「多因子」专家信封 ----------
def test_expert_多因子_maps_and_validates(monkeypatch):
    from tools.store import repo as store
    fv = {"综合分": 0.82, "综合分位": 0.9, "强度": 0.8, "方向": "看多",
          "因子齐全度": 0.5, "数据充分度": "部分降级", "各因子分位": {"质量": 0.9},
          "依据": ["质量分位0.90"]}
    monkeypatch.setattr(store, "get_code_view", lambda name, code, *a, **k: fv)
    v = experts.build("多因子", _rec("600000"))
    assert v.专家 == "多因子" and v.能力类型 == "评级" and v.方向 == "看多"
    assert v.强度 == pytest.approx(0.8) and v.置信度 == pytest.approx(0.5)
    assert not validate_verdict(v)                        # 过 F1 契约


def test_expert_多因子_missing_view_abstains(monkeypatch):
    from tools.store import repo as store
    def _raise(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(store, "get_code_view", _raise)
    v = experts.build("多因子", _rec("600000"))
    assert v.方向 == "中性" and v.强度 == 0.0 and v.数据充分度 == "缺失"
    assert not validate_verdict(v)
