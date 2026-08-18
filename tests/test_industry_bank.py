"""银行财报专家模块单测。

锁住:6 个必导出的契约结构 + 每条专属红旗的命中/不命中语义边界 + 空输入不抛。
⚠️ 阈值为工程占位,断言只锁"语义方向",不锁具体分值(标定后区间会变)。
"""
from __future__ import annotations

from tools.analysis.financial import scoring
from tools.analysis.financial.industry import 银行 as bank


# ── 契约结构 ──────────────────────────────────────────────────────────
def test_key_is_canonical():
    assert bank.KEY == "银行"          # 必须正好这个字符串,否则 analyzer 路由不到


def test_note_is_str():
    assert isinstance(bank.NOTE, str) and bank.NOTE


def test_dimension_specs_structure():
    specs = bank.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        assert isinstance(dim, str)
        assert isinstance(subs, list) and subs
        for item in subs:
            assert isinstance(item, tuple) and len(item) == 4
            name, key, lo, hi = item
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_dimension_specs_covers_five_dims():
    # 沿用五维骨架(便于页面对齐)
    assert set(bank.dimension_specs()) == {"成长", "质量", "健康", "运营", "回报"}


def test_dimension_specs_uses_bank_revenue_key():
    # 银行营收必须走『营收增速_银行』(通用『营收增速』对银行为空)
    keys = {k for subs in bank.dimension_specs().values() for (_n, k, _lo, _hi) in subs}
    assert "营收增速_银行" in keys
    assert "营收增速" not in keys           # 不能误用通用营收增速


def test_weights_shape_and_quality_heaviest():
    w = bank.weights()
    assert w is None or isinstance(w, dict)
    assert isinstance(w, dict)
    assert set(w) == {"成长", "质量", "健康", "运营", "回报"}
    # 资产质量给最高权(银行命门是坏账)
    assert w["质量"] == max(w.values())


def test_skip_flags_from_universal_list():
    universal = {"增收不增利", "现金含量不足", "应收存货激增", "商誉高企", "高负债",
                 "扣非占比低", "短债覆盖不足", "扣非为负", "非标审计意见", "毛利率异常跳升"}
    assert isinstance(bank.SKIP_FLAGS, list) and bank.SKIP_FLAGS
    assert set(bank.SKIP_FLAGS) <= universal          # 只能从通用清单里选
    # 制造业口径对银行失效的几条必须跳
    for name in ("高负债", "短债覆盖不足", "应收存货激增", "毛利率异常跳升"):
        assert name in bank.SKIP_FLAGS


# ── extra_flags:健壮性 ────────────────────────────────────────────────
def test_extra_flags_empty_inputs_no_throw():
    assert bank.extra_flags({}, {}) == []
    assert bank.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}}) == []
    assert bank.extra_flags(None, None) == []          # 极端缺值也不抛


def test_extra_flags_returns_only_hit():
    out = bank.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}})
    assert isinstance(out, list)
    assert all(f["命中"] for f in out)


def _codes(flags):
    return {f["code"] for f in flags}


# ── 每条专属红旗:命中 / 不命中边界 ──────────────────────────────────────
def test_flag_成本收入比走高():
    # 命中:业管费/营收 = 60% > 45% 上限
    hi = {"利润表": {"业务及管理费": 60.0, "营业收入": 100.0}}
    assert "成本收入比走高" in _codes(bank.extra_flags({}, hi))
    # 不命中:22.7%(成都银行实测量级)
    lo = {"利润表": {"业务及管理费": 22.7, "营业收入": 100.0}}
    assert "成本收入比走高" not in _codes(bank.extra_flags({}, lo))


def test_flag_拨备计提力度不足():
    # 命中:减值/PPOP = 2/100 = 0.02 < 0.05,且 PPOP>0
    hit = bank.extra_flags({"拨备前营业利润": 100.0}, {"利润表": {"信用减值损失_金融": 2.0}})
    assert "拨备计提力度不足" in _codes(hit)
    # 不命中:减值/PPOP = 30/100 = 0.3(计提充分)
    miss = bank.extra_flags({"拨备前营业利润": 100.0}, {"利润表": {"信用减值损失_金融": 30.0}})
    assert "拨备计提力度不足" not in _codes(miss)
    # 护栏:PPOP<=0 时不判该条(交由"拨备前利润为负")
    neg = bank.extra_flags({"拨备前营业利润": -10.0}, {"利润表": {"信用减值损失_金融": 0.1}})
    assert "拨备计提力度不足" not in _codes(neg)


def test_flag_拨备前利润为负():
    assert "拨备前利润为负" in _codes(bank.extra_flags({"拨备前营业利润": -5.0}, {}))
    assert "拨备前利润为负" not in _codes(bank.extra_flags({"拨备前营业利润": 47.1}, {}))


def test_flag_存贷比过高():
    # 命中:贷款/存款 = 110% > 100%
    hi = {"资产负债表": {"发放贷款及垫款": 110.0, "吸收存款": 100.0}}
    assert "存贷比过高" in _codes(bank.extra_flags({}, hi))
    # 不命中:83.68%(成都银行实测量级)
    lo = {"资产负债表": {"发放贷款及垫款": 83.68, "吸收存款": 100.0}}
    assert "存贷比过高" not in _codes(bank.extra_flags({}, lo))


def test_flag_非息收入占比过低():
    # 命中:非息占比 = (100-95)/100 = 5% < 10%
    hi = {"利润表": {"营业收入": 100.0, "利息净收入": 95.0}}
    assert "非息收入占比过低" in _codes(bank.extra_flags({}, hi))
    # 不命中:非息占比 = 30% (多元化良好)
    lo = {"利润表": {"营业收入": 100.0, "利息净收入": 70.0}}
    assert "非息收入占比过低" not in _codes(bank.extra_flags({}, lo))


def test_健康维_有可算键_不空转():
    """M4 回归锁:健康维依赖的键(PPOP覆盖减值倍数 等)由 metrics 产出后,
    该维用银行 specs 能打出分(非 None),不再整维空转。防未来重写 metrics 时删掉这些键。"""
    # 仅给健康维当前唯一可算子键 PPOP覆盖减值倍数(区间 1.0→4.0),取中段
    dims = scoring.five_dims({"PPOP覆盖减值倍数": 2.5}, bank.dimension_specs())
    assert dims["健康"] is not None
    assert 0 <= dims["健康"] <= 100


def test_flag_dict_shape():
    # 每条红旗结构对齐 flags.py:{code,命中,严重度,值}
    out = bank.extra_flags({"拨备前营业利润": -1.0},
                           {"利润表": {"业务及管理费": 60.0, "营业收入": 100.0}})
    assert out
    for f in out:
        assert set(f) >= {"code", "命中", "严重度", "值"}
        assert f["严重度"] in {"高", "中", "低"}
        assert isinstance(f["值"], dict)
