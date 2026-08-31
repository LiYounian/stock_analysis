"""成长期未盈利半导体(fabless/科创)财报豁免 + 路由单测。

锁三条语义(改动依据见 docs/财报分析专家/半导体_成长期财报分析要点.md):
  1. 电子/半导体专家路由命中:半导体池(申万二级 801081)成分即便 board_membership 数据缺,
     也能兜底路由到「电子」专家;
  2. 成长期未盈利半导体(高研发+高增+当期扣非为负)不被「扣非为负」一刀切高危封顶:
     dynamic_skip 豁免通用高危,改由专属「成长期未盈利」提示 + runway 承压红旗承接;
  3. 非该画像不受影响:普通电子亏损票(低研发/负增)仍保留「扣非为负」高危封顶;
     非电子票路由/红旗不变。
"""
from __future__ import annotations

from tools.analysis.financial import analyzer as A
from tools.analysis.financial import flags as F
from tools.analysis.financial import metrics as M
from tools.analysis.financial import scoring as S
from tools.analysis.financial.industry import 电子 as 电子


# ── 构造工具:成长期未盈利 fabless 画像 / 普通电子亏损画像 ─────────────
def _growth_semi_derived():
    return {"研发费用率": 25.0, "营收增速": 35.0, "毛利率": 55.0}


def _growth_semi_struct(kf=-1.0e8, 货币资金=None, CFO=None):
    s = {"利润表": {"营业总收入": 5.0e8, "扣非归母净利润": kf, "归母净利润": kf},
         "资产负债表": {}, "现金流量表": {}}
    if 货币资金 is not None:
        s["资产负债表"]["货币资金"] = 货币资金
    if CFO is not None:
        s["现金流量表"]["经营活动现金流量净额"] = CFO
    return s


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


# ── 1. 路由:半导体池成分兜底命中电子 ───────────────────────────────
def test_半导体池成分路由到电子():
    """申万二级 801081 半导体池成分:industry 缺 + board_of 缺 时,兜底路由到「电子」。"""
    import json
    from tools.config import settings
    semi = json.loads((settings.PROJECT_ROOT / "config" / "semi_universe.json").read_text("utf-8"))
    assert semi, "半导体池不应为空(config/semi_universe.json)"
    code = semi[0]
    assert A._in_semi_universe(code) is True
    # industry=None(池外/未填)→ 走 board_of(数据缺→None)→ 半导体池兜底 → 电子
    assert A._industry_key(code, industry=None) == "电子"


def test_非半导体票不被半导体兜底误路由():
    """非池内票不因半导体兜底被误判为电子(该兜底仅对池成分生效)。"""
    fake = "999999"                      # 不在任何池/主档
    assert A._in_semi_universe(fake) is False
    # industry=None + board 缺 + 非半导体池 → None(不误路由成电子)
    assert A._industry_key(fake, industry=None) != "电子"


def test_显式电子行业仍命中电子():
    assert A._industry_key("688262", industry="电子") == "电子"
    assert A._industry_key("688262", industry="半导体") == "电子"   # 别名对齐


# ── 2. 成长期未盈利画像判定 ─────────────────────────────────────────
def test_is_growth_semi_命中():
    assert 电子._is_growth_semi(_growth_semi_derived(), _growth_semi_struct()) is True


def test_is_growth_semi_研发不足不命中():
    d = dict(_growth_semi_derived(), 研发费用率=8.0)      # < 15
    assert 电子._is_growth_semi(d, _growth_semi_struct()) is False


def test_is_growth_semi_低增不命中():
    d = dict(_growth_semi_derived(), 营收增速=5.0)         # < 20
    assert 电子._is_growth_semi(d, _growth_semi_struct()) is False


def test_is_growth_semi_已盈利不命中():
    assert 电子._is_growth_semi(_growth_semi_derived(),
                              _growth_semi_struct(kf=1.0e8)) is False   # 扣非为正


def test_is_growth_semi_缺值不命中():
    assert 电子._is_growth_semi({}, {}) is False


# ── 3. dynamic_skip 豁免语义 ────────────────────────────────────────
def test_dynamic_skip_成长半导体豁免通用高危():
    skip = 电子.dynamic_skip(_growth_semi_derived(), _growth_semi_struct())
    assert "扣非为负" in skip and "现金含量不足" in skip


def test_dynamic_skip_普通电子亏损不豁免():
    d = {"研发费用率": 4.0, "营收增速": -10.0, "毛利率": 20.0}   # 低研发+负增
    assert 电子.dynamic_skip(d, _growth_semi_struct()) == []


# ── 4. 端到端:成长半导体扣非为负不再一刀切高危封顶 ─────────────────
def test_成长半导体扣非为负不封顶():
    d, s = _growth_semi_derived(), _growth_semi_struct()
    skip = 电子.dynamic_skip(d, s)
    flags = F.evaluate_flags(d, s, is_financial=False, skip=skip,
                             extra=电子.extra_flags(d, s))
    assert "扣非为负" not in _codes(flags)              # 已豁免
    assert "成长期未盈利_研发驱动" in _codes(flags)      # 专属提示承接
    sc = S.quality_score(d, flags)
    assert sc["高危封顶"] is False                       # 不再高危封顶
    assert sc["评级"] != "风险" or sc["quality_score"] > 35


def test_普通电子亏损仍保留扣非为负高危封顶():
    """无泄漏:低研发/负增的普通电子亏损票,dynamic_skip 空 → 扣非为负仍高危 → 封顶。"""
    d = {"研发费用率": 3.0, "营收增速": -20.0, "毛利率": 15.0}
    s = _growth_semi_struct(kf=-2.0e8)
    skip = 电子.dynamic_skip(d, s)                       # []
    flags = F.evaluate_flags(d, s, is_financial=False, skip=skip,
                             extra=电子.extra_flags(d, s))
    assert "扣非为负" in _codes(flags)
    assert F.has_high_severity(flags) is True
    sc = S.quality_score(d, flags)
    assert sc["高危封顶"] is True


def test_非电子票豁免不生效():
    """无泄漏:非电子专家(无 dynamic_skip 路径)时,通用扣非为负照常升起。"""
    d = _growth_semi_derived()
    s = _growth_semi_struct()
    # 通用兜底(无专家 skip/extra)——模拟非电子路由
    flags = F.evaluate_flags(d, s, is_financial=False, skip=None, extra=None)
    assert "扣非为负" in _codes(flags)


# ── 5. runway 承压红旗 + marker ─────────────────────────────────────
def test_runway承压命中():
    d = dict(_growth_semi_derived(), 现金runway月数=6.0)   # < 12
    flags = 电子.extra_flags(d, _growth_semi_struct())
    assert "现金runway承压" in _codes(flags)


def test_runway充足不承压():
    d = dict(_growth_semi_derived(), 现金runway月数=60.0)  # 5 年,充足
    flags = 电子.extra_flags(d, _growth_semi_struct())
    assert "现金runway承压" not in _codes(flags)
    assert "成长期未盈利_研发驱动" in _codes(flags)         # 仍有透明化 marker


def test_marker严重度为提示且0扣分():
    d, s = _growth_semi_derived(), _growth_semi_struct()
    f = next(x for x in 电子.extra_flags(d, s) if x["code"] == "成长期未盈利_研发驱动")
    assert f["严重度"] == "提示"
    # 提示项经 scoring 不扣分
    assert S.quality_score(d, [f])["红旗扣分"] == 0


# ── 6. 现金 runway 衍生指标 ─────────────────────────────────────────
def test_现金runway月数_烧钱期可算():
    periods = {"2025-12-31": {"利润表": {"营业总收入": 1.0e8, "营业成本": 5.0e7},
                              "资产负债表": {"货币资金": 1.2e9},
                              "现金流量表": {"经营活动现金流量净额": -1.2e8}}}
    d = M.compute_derived(periods)["2025-12-31"]
    # 年报:月烧钱 = 1.2e8/12 = 1e7;runway = 1.2e9 / 1e7 = 120 个月
    assert d["现金runway月数"] is not None
    assert abs(d["现金runway月数"] - 120.0) < 1e-6


def test_现金runway月数_不烧钱为None():
    periods = {"2025-12-31": {"利润表": {"营业总收入": 1.0e8, "营业成本": 5.0e7},
                              "资产负债表": {"货币资金": 1.2e9},
                              "现金流量表": {"经营活动现金流量净额": 5.0e7}}}   # CFO>0
    d = M.compute_derived(periods)["2025-12-31"]
    assert d["现金runway月数"] is None
