"""房地产行业财报专家模块单测。

锁语义(防未来 prompt/代码重写误删规则):
  - 契约:KEY 为申万一级规范名"房地产";dimension_specs 结构合法;weights 归一且健康(三道红线)最重;
  - 行业画像:高负债属行业特性→SKIP『高负债』,改用三道红线专项;资产负债率/存货周转天数区间大幅放宽;
  - 三道红线专项红旗:踩线档位随条数升;明股实债嫌疑=少数股东损益占比与权益占比错配;
  - 每条专属红旗至少一个"命中/不命中"边界断言;
  - 空/半空输入不抛异常、返回 list。
纯函数、不触网、不读盘。
"""
import importlib

mod = importlib.import_module("tools.analysis.financial.industry.房地产")


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


def _flag(flags, code):
    for f in flags:
        if f.get("命中") and f["code"] == code:
            return f
    return None


# ———————————— 契约 ————————————
def test_key_is_canonical_sw_name():
    assert mod.KEY == "房地产"
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


def test_dimension_specs_leverage_relaxed():
    """地产天然高负债/慢周转:资产负债率反向区间大幅放宽,存货周转天数放宽。"""
    specs = mod.dimension_specs()
    dar = [s for s in specs["健康"] if s[1] == "资产负债率"][0]
    assert dar[2] > dar[3] and dar[2] >= 85    # 0分端 >= 85(远宽于通用 80)
    inv = [s for s in specs["运营"] if s[1] == "存货周转天数"][0]
    assert inv[2] > inv[3] and inv[2] >= 1000  # 开发产品周转极慢,区间放到千天级


def test_weights_health_heaviest():
    """债务安全是命门:健康(三道红线)权重最大。"""
    w = mod.weights()
    assert isinstance(w, dict) and abs(sum(w.values()) - 1.0) < 1e-6
    assert w["健康"] == max(w.values())


def test_skip_flags():
    # 高负债属地产行业特性,SKIP;改用三道红线专项
    assert "高负债" in mod.SKIP_FLAGS


# ———————————— 三道红线专项 ————————————
def _bs_three_lines_all_breach():
    # 净负债率:(200-10)/100=1.9>1 踩;现金短债比:10/50=0.2<1 踩;
    # 剔预:(800-50-50)/(1000-50-50)=700/900=0.78>0.7 踩 → 3 条=红档
    return (
        {"有息负债": 200.0},
        {"资产负债表": {"货币资金": 10.0, "归母股东权益": 100.0, "股东权益合计": 100.0,
                    "负债合计": 800.0, "资产总计": 1000.0,
                    "预收账款": 50.0, "合同负债": 50.0,
                    "短期借款": 50.0, "一年内到期非流动负债": 0.0},
         "利润表": {}, "现金流量表": {}})


def test_flag_three_red_lines_hit_red():
    d, s = _bs_three_lines_all_breach()
    f = _flag(mod.extra_flags(d, s), "三道红线踩线")
    assert f is not None
    assert f["值"]["踩线条数"] == 3
    assert f["值"]["档位"] == "红档"
    assert f["严重度"] == "高"


def test_flag_three_red_lines_miss_green():
    # 全部安全:0 踩线 → 不发声
    d = {"有息负债": 10.0}
    s = {"资产负债表": {"货币资金": 300.0, "归母股东权益": 500.0, "股东权益合计": 500.0,
                   "负债合计": 200.0, "资产总计": 1000.0,
                   "预收账款": 20.0, "合同负债": 20.0,
                   "短期借款": 5.0, "一年内到期非流动负债": 0.0},
         "利润表": {}, "现金流量表": {}}
    assert "三道红线踩线" not in _codes(mod.extra_flags(d, s))


def test_flag_disguised_equity_debt_hit_and_miss():
    # 命中:少数股东权益占比 0.4(>0.3)但损益占比 0.05(<0.4*0.5=0.2)→ 出资不分利
    hit = mod.extra_flags({}, {"利润表": {"净利润": 1e8, "归母净利润": 9.5e7},   # 少数损益 5e6 → 占比 0.05
                               "资产负债表": {"归母股东权益": 6e8, "股东权益合计": 1e9}})  # 少数权益 4e8 → 0.4
    assert "明股实债嫌疑" in _codes(hit)
    # 不命中:权益占比与损益占比匹配(都约 0.4)
    miss = mod.extra_flags({}, {"利润表": {"净利润": 1e8, "归母净利润": 6e7},   # 少数损益 4e7 → 0.4
                                "资产负债表": {"归母股东权益": 6e8, "股东权益合计": 1e9}})
    assert "明股实债嫌疑" not in _codes(miss)


def test_flag_destocking_slowdown_hit_and_miss():
    assert "去化放缓" in _codes(mod.extra_flags({"存货增速": 35.0, "营收增速": 10.0}, {}))
    assert "去化放缓" not in _codes(mod.extra_flags({"存货增速": 15.0, "营收增速": 10.0}, {}))


def test_flag_sales_cooling_hit_and_miss():
    assert "销售去化遇冷" in _codes(mod.extra_flags({"合同负债环比": -30.0}, {}))
    assert "销售去化遇冷" not in _codes(mod.extra_flags({"合同负债环比": -5.0}, {}))


def test_flag_negative_cfo_hit_and_miss():
    hit = mod.extra_flags({}, {"利润表": {"归母净利润": 5e8},
                               "现金流量表": {"经营活动现金流量净额": -3e8}})
    assert "经营现金流为负" in _codes(hit)
    miss = mod.extra_flags({}, {"利润表": {"归母净利润": 5e8},
                                "现金流量表": {"经营活动现金流量净额": 1e8}})
    assert "经营现金流为负" not in _codes(miss)


# ———————————— 缺值/半空 ————————————
def test_extra_flags_empty_inputs_no_raise():
    assert isinstance(mod.extra_flags({}, {}), list)
    assert isinstance(mod.extra_flags(None, None), list)
    empty = mod.extra_flags({}, {"利润表": {}, "资产负债表": {}, "现金流量表": {}})
    assert isinstance(empty, list) and empty == []


def test_extra_flags_output_shape():
    d, s = _bs_three_lines_all_breach()
    for f in mod.extra_flags(d, s):
        assert set(f) >= {"code", "命中", "严重度", "值"}
        assert f["严重度"] in ("高", "中", "低")
