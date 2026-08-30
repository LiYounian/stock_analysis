"""非银金融(券商/保险)行业财报专家模块单测。

锁语义(防未来 prompt/代码重写误删规则):
  - 契约:KEY 为申万一级规范名"非银金融";dimension_specs 结构合法;weights 归一且回报最重;
  - **金融业特判**:五维用金融口径键(营收增速_银行/成本收入比/ROA 等),**不含制造业口径**
    (无毛利率/存货周转天数子指标);SKIP 制造业通用红旗(高负债/现金含量不足/毛利率跳升等);
  - 每条专属红旗至少一个"命中/不命中"边界断言;专属红旗为金融口径(自营/浮盈/杠杆/成本收入比/扣非);
  - 空/半空输入不抛异常、返回 list。
纯函数、不触网、不读盘。
"""
import importlib

mod = importlib.import_module("tools.analysis.financial.industry.非银金融")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


# ———————————— 契约 ————————————
def test_key_is_canonical_sw_name():
    assert mod.KEY == "非银金融"
    assert isinstance(mod.NOTE, str) and mod.NOTE


def test_dimension_specs_structure():
    specs = mod.dimension_specs()
    assert isinstance(specs, dict) and specs
    for dim, subs in specs.items():
        for sub in subs:
            assert len(sub) == 4
            name, key, lo, hi = sub
            assert isinstance(name, str) and isinstance(key, str)
            assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))


def test_dimension_specs_is_financial_not_manufacturing():
    """金融业特判:用金融口径键,不套制造业口径(无毛利率/存货周转子指标)。"""
    specs = mod.dimension_specs()
    all_keys = {sub[1] for subs in specs.values() for sub in subs}
    # 金融口径键在场
    assert "营收增速_银行" in all_keys      # 用营业收入口径(非营业总收入)
    assert "成本收入比" in all_keys          # 金融效率口径
    assert "ROA" in all_keys
    # 制造业口径键**不应**出现(否则=错误套用通用制造业评分)
    assert "毛利率" not in all_keys
    assert "存货周转天数" not in all_keys


def test_weights_return_heaviest():
    """金融看盈利质量与回报:回报权重最大。"""
    w = mod.weights()
    assert isinstance(w, dict) and abs(sum(w.values()) - 1.0) < 1e-6
    assert w["回报"] == max(w.values())


def test_skip_flags_manufacturing_dropped():
    """跳掉对非银不适用的制造业通用红旗(与金融业自动跳过并集冗余)。"""
    for name in ("高负债", "现金含量不足", "短债覆盖不足", "应收存货激增", "毛利率异常跳升"):
        assert name in mod.SKIP_FLAGS


# ———————————— 专属红旗:边界(金融口径)————————————
def test_flag_proprietary_trading_reliance_hit_and_miss():
    # 命中:(投资收益 6 + 公允价值变动 0)/营业收入 10 = 0.6 > 0.5
    hit = mod.extra_flags({}, {"利润表": {"营业收入": 1e10, "投资收益": 6e9, "公允价值变动收益": 0.0}})
    assert "自营依赖过高" in _codes(hit)
    # 不命中:自营占比低
    miss = mod.extra_flags({}, {"利润表": {"营业收入": 1e10, "投资收益": 2e9, "公允价值变动收益": 0.0}})
    assert "自营依赖过高" not in _codes(miss)


def test_flag_fairvalue_quality_hit_and_miss():
    # 命中:公允价值变动 4 / 营业利润 10 = 0.4 > 0.3(且均为正)
    hit = mod.extra_flags({}, {"利润表": {"营业利润": 1e9, "公允价值变动收益": 4e8}})
    assert "浮盈质量存疑" in _codes(hit)
    # 不命中:公允价值变动占比低
    miss = mod.extra_flags({}, {"利润表": {"营业利润": 1e9, "公允价值变动收益": 1e8}})
    assert "浮盈质量存疑" not in _codes(miss)
    # 不命中:公允价值变动为负(浮亏,不当浮盈红旗)
    miss2 = mod.extra_flags({}, {"利润表": {"营业利润": 1e9, "公允价值变动收益": -4e8}})
    assert "浮盈质量存疑" not in _codes(miss2)


def test_flag_high_leverage_hit_and_miss():
    # 命中:资产 15 / 归母权益 1 = 15 > 12
    hit = mod.extra_flags({}, {"资产负债表": {"资产总计": 1.5e11, "归母股东权益": 1e10}})
    assert "杠杆过高" in _codes(hit)
    # 不命中:杠杆适中
    miss = mod.extra_flags({}, {"资产负债表": {"资产总计": 5e10, "归母股东权益": 1e10}})
    assert "杠杆过高" not in _codes(miss)


def test_flag_cost_income_ratio_hit_and_miss():
    # 命中:业管费 7 / 营业收入 10 = 70% > 60%
    hit = mod.extra_flags({}, {"利润表": {"营业收入": 1e10, "业务及管理费": 7e9}})
    assert "成本收入比走高" in _codes(hit)
    miss = mod.extra_flags({}, {"利润表": {"营业收入": 1e10, "业务及管理费": 4e9}})
    assert "成本收入比走高" not in _codes(miss)


def test_flag_nonrecurring_quality_hit_and_miss():
    # 命中路径 A:扣非占归母 < 0.6
    assert "扣非质量差" in _codes(mod.extra_flags({"扣非占归母": 0.3}, {}))
    # 命中路径 B:归母正而扣非负
    hitB = mod.extra_flags({}, {"利润表": {"归母净利润": 1e9, "扣非归母净利润": -2e8}})
    assert "扣非质量差" in _codes(hitB)
    # 不命中:扣非占归母健康
    assert "扣非质量差" not in _codes(mod.extra_flags({"扣非占归母": 0.95}, {}))


# ———————————— 缺值/半空 ————————————
def test_extra_flags_empty_inputs_no_raise():
    assert isinstance(mod.extra_flags({}, {}), list)
    assert isinstance(mod.extra_flags(None, None), list)
    empty = mod.extra_flags({}, {"利润表": {}, "资产负债表": {}})
    assert isinstance(empty, list) and empty == []


def test_extra_flags_output_shape():
    for f in mod.extra_flags({"扣非占归母": 0.2},
                             {"利润表": {"营业收入": 1e10, "投资收益": 6e9, "公允价值变动收益": 0.0}}):
        assert set(f) >= {"code", "命中", "严重度", "值"}
        assert f["严重度"] in ("高", "中", "低")
