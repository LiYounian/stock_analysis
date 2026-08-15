"""财报 P0 数值层单测(不触网,mock akshare / 合成 raw)。

锁语义(约法6):
  - 采集:三表对齐合并 + 单表失败降级不崩 + 全表失败跳过 + 落盘往返。
  - 衍生:累计同比增速 / 单季拆分 / 关键比率算对。
  - 红旗:各阈值**边界**命中/不命中语义(防未来重写误删规则)。
  - 评分:评级映射 + 高危红旗封顶。
  - 无未来函数:analyze/query 只见 disclosure_date <= as_of 的报告期。
  - 契约:financial 块合法/非法评级校验。
"""
import types

import pandas as pd
import pytest

from tools.collectors import financial as fin
from tools.analysis.financial import analyzer, flags, metrics, scoring
from tools.store import repo as store


# ————————————————————— 采集:符号映射 / 合并 / 降级 —————————————————————
def test_em_symbol_prefix():
    assert fin._em_symbol("600519") == "SH600519"
    assert fin._em_symbol("000001") == "SZ000001"
    assert fin._em_symbol("300760") == "SZ300760"
    assert fin._em_symbol("830799") == "BJ830799"
    assert fin._em_symbol("1") == "SZ000001"          # 补零


def _fake_table(fn_key, periods):
    """构造一张 by_report 表 DataFrame。periods: [(report_date, notice_date, {en:val})]。"""
    rows = []
    for rd, nd, vals in periods:
        base = {"SECURITY_NAME_ABBR": "测试股", "REPORT_DATE": rd + " 00:00:00",
                "NOTICE_DATE": nd + " 00:00:00", "REPORT_TYPE": "年报",
                "OPINION_TYPE": "标准无保留意见"}
        base.update(vals)
        rows.append(base)
    return pd.DataFrame(rows)


def _install_fake_ak(monkeypatch, profit=None, balance=None, cashflow=None):
    def mk(df):
        return (lambda symbol: df)
    fake = types.SimpleNamespace(
        stock_profit_sheet_by_report_em=mk(profit),
        stock_balance_sheet_by_report_em=mk(balance),
        stock_cash_flow_sheet_by_report_em=mk(cashflow),
    )
    monkeypatch.setitem(__import__("sys").modules, "akshare", fake)


def test_merge_three_tables(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    monkeypatch.setattr(fin.settings, "FETCH_SLEEP_SEC", 0)
    prof = _fake_table("p", [("2025-12-31", "2026-04-01", {"TOTAL_OPERATE_INCOME": 100.0,
                              "PARENT_NETPROFIT": 10.0, "DEDUCT_PARENT_NETPROFIT": 9.0,
                              "OPERATE_COST": 60.0})])
    bal = _fake_table("b", [("2025-12-31", "2026-04-01", {"TOTAL_ASSETS": 500.0,
                             "TOTAL_LIABILITIES": 200.0, "TOTAL_PARENT_EQUITY": 300.0,
                             "GOODWILL": 30.0})])
    cf = _fake_table("c", [("2025-12-31", "2026-04-01", {"NETCASH_OPERATE": 8.0})])
    _install_fake_ak(monkeypatch, prof, bal, cf)
    out = fin.fetch_financial(["000001"])
    assert "000001" in out
    rec = out["000001"]["periods"]["2025-12-31"]
    assert rec["disclosure_date"] == "2026-04-01"
    assert rec["report_type"] == "年报"
    assert rec["audit_opinion"] == "标准无保留意见"
    assert rec["利润表"]["营业总收入"] == 100.0
    assert rec["资产负债表"]["商誉"] == 30.0
    assert rec["现金流量表"]["经营活动现金流量净额"] == 8.0
    # 落盘往返
    loaded = fin.load_financial("000001")
    assert loaded["periods"]["2025-12-31"]["利润表"]["归母净利润"] == 10.0


def test_one_table_fail_degrades(monkeypatch, tmp_path):
    """现金流量表接口抛错 → 该表留空,其余表照常产出,不崩。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    monkeypatch.setattr(fin.settings, "FETCH_SLEEP_SEC", 0)
    prof = _fake_table("p", [("2025-12-31", "2026-04-01", {"TOTAL_OPERATE_INCOME": 100.0})])
    bal = _fake_table("b", [("2025-12-31", "2026-04-01", {"TOTAL_ASSETS": 500.0})])

    def boom(symbol):
        raise ConnectionError("现金流表被限流")
    fake = types.SimpleNamespace(
        stock_profit_sheet_by_report_em=lambda symbol: prof,
        stock_balance_sheet_by_report_em=lambda symbol: bal,
        stock_cash_flow_sheet_by_report_em=boom,
    )
    monkeypatch.setitem(__import__("sys").modules, "akshare", fake)
    out = fin.fetch_financial(["000001"])
    rec = out["000001"]["periods"]["2025-12-31"]
    assert rec["利润表"]["营业总收入"] == 100.0
    assert rec["现金流量表"] == {}                     # 该表降级为空


def test_all_tables_fail_skips_stock(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    monkeypatch.setattr(fin.settings, "FETCH_SLEEP_SEC", 0)

    def boom(symbol):
        raise ConnectionError("全挂")
    fake = types.SimpleNamespace(
        stock_profit_sheet_by_report_em=boom,
        stock_balance_sheet_by_report_em=boom,
        stock_cash_flow_sheet_by_report_em=boom,
    )
    monkeypatch.setitem(__import__("sys").modules, "akshare", fake)
    out = fin.fetch_financial(["000001"])
    assert out == {}                                    # 整票跳过,不抛


# ————————————————————— 衍生指标 —————————————————————
def _synthetic_periods():
    """两年年报:营收 100→120(+20%),归母 10→8(-20%,增收不增利),CFO 各期。"""
    return {
        "2025-12-31": {
            "report_date": "2025-12-31", "disclosure_date": "2026-04-01", "report_type": "年报",
            "利润表": {"营业总收入": 120.0, "营业成本": 90.0, "归母净利润": 8.0,
                     "扣非归母净利润": 4.0, "净利润": 8.0},
            "资产负债表": {"资产总计": 500.0, "负债合计": 380.0, "股东权益合计": 120.0,
                       "归母股东权益": 120.0, "商誉": 50.0, "应收账款": 60.0, "存货": 40.0,
                       "货币资金": 10.0, "短期借款": 30.0, "一年内到期非流动负债": 5.0},
            "现金流量表": {"经营活动现金流量净额": 4.0, "购建固定资产无形资产等支付现金": 2.0},
        },
        "2024-12-31": {
            "report_date": "2024-12-31", "disclosure_date": "2025-04-01", "report_type": "年报",
            "利润表": {"营业总收入": 100.0, "营业成本": 60.0, "归母净利润": 10.0,
                     "扣非归母净利润": 9.0, "净利润": 10.0},
            "资产负债表": {"资产总计": 450.0, "负债合计": 200.0, "股东权益合计": 250.0,
                       "归母股东权益": 250.0, "商誉": 50.0, "应收账款": 30.0, "存货": 20.0,
                       "货币资金": 100.0, "短期借款": 10.0},
            "现金流量表": {"经营活动现金流量净额": 12.0, "购建固定资产无形资产等支付现金": 2.0},
        },
    }


def test_derived_yoy_and_ratios():
    d = metrics.compute_derived(_synthetic_periods())["2025-12-31"]
    assert d["营收增速"] == pytest.approx(20.0)         # (120-100)/100
    assert d["归母净利增速"] == pytest.approx(-20.0)     # (8-10)/10
    assert d["毛利率"] == pytest.approx(25.0)           # (120-90)/120*100
    assert d["扣非占归母"] == pytest.approx(0.5)         # 4/8
    assert d["现金含量_CFO比净利"] == pytest.approx(0.5)  # 4/8
    assert d["资产负债率"] == pytest.approx(76.0)        # 380/500*100
    assert d["自由现金流"] == pytest.approx(2.0)         # 4-2
    # 短债覆盖 = 货币资金 / (短期借款+一年内到期) = 10/35
    assert d["短债覆盖"] == pytest.approx(10.0 / 35.0)


def test_single_quarter_split():
    """Q3=前三季累计−H1 单季拆分。"""
    periods = {
        "2025-06-30": {"利润表": {"营业总收入": 50.0}},
        "2025-09-30": {"利润表": {"营业总收入": 80.0}},
    }
    assert metrics._single_quarter(periods, "2025-09-30", "利润表", "营业总收入") == 30.0
    assert metrics._single_quarter(periods, "2025-03-31", "利润表", "营业总收入") is None  # 缺Q1记录


def test_yoy_negative_prev_direction():
    """去年为负、今年改善 → 增速为正(亏损收窄=正增长)。"""
    assert metrics._yoy_pct(-5.0, -10.0) == pytest.approx(50.0)   # (-5-(-10))/10
    assert metrics._yoy_pct(10.0, 0.0) is None                    # 分母0→None


# ————————————————————— 红旗阈值边界 —————————————————————
def test_flag_cash_content_boundary():
    """CFO/净利 边界:阈值 0.8;0.79 命中,0.80 不命中。"""
    assert any(f["code"] == "现金含量不足" for f in flags.evaluate_flags({"现金含量_CFO比净利": 0.79}))
    assert not any(f["code"] == "现金含量不足" for f in flags.evaluate_flags({"现金含量_CFO比净利": 0.80}))


def test_flag_debt_ratio_boundary():
    assert any(f["code"] == "高负债" for f in flags.evaluate_flags({"资产负债率": 70.1}))
    assert not any(f["code"] == "高负债" for f in flags.evaluate_flags({"资产负债率": 70.0}))


def test_flag_grow_rev_not_profit():
    fs = flags.evaluate_flags({"营收增速": 20.0, "归母净利增速": -20.0})
    assert any(f["code"] == "增收不增利" for f in fs)


def test_flag_recv_inv_surge():
    """应收增速 − 营收增速 > 20pct → 应收存货激增。"""
    fs = flags.evaluate_flags({"营收增速": 10.0, "应收增速": 35.0, "存货增速": 12.0})
    assert any(f["code"] == "应收存货激增" for f in fs)
    fs2 = flags.evaluate_flags({"营收增速": 10.0, "应收增速": 25.0, "存货增速": 12.0})
    assert not any(f["code"] == "应收存货激增" for f in fs2)   # 差 15 < 20,不命中


def test_flag_kf_negative_high_severity():
    fs = flags.evaluate_flags({}, {"利润表": {"扣非归母净利润": -3.0}})
    hit = [f for f in fs if f["code"] == "扣非为负"]
    assert hit and hit[0]["严重度"] == "高"
    assert flags.has_high_severity(fs)


def test_flags_missing_data_no_hit():
    """全 None 输入 → 无红旗(不误杀,不抛)。"""
    assert flags.evaluate_flags({}) == []


# ————————————————————— 评分 —————————————————————
def test_rating_mapping():
    cfg = {"评级映射": {"优": 80, "良": 65, "中": 50, "差": 35}}
    assert scoring._rating(85, cfg) == "优"
    assert scoring._rating(65, cfg) == "良"
    assert scoring._rating(34, cfg) == "风险"
    assert scoring._rating(None, cfg) is None


def test_high_severity_caps_score():
    """含高危红旗 → 评分封顶到高危封顶分(评级≤差)。"""
    derived = {"营收增速": 40, "扣非净利增速": 40, "现金含量_CFO比净利": 1.2,
               "扣非占归母": 1.0, "毛利率": 60, "资产负债率": 30, "短债覆盖": 2,
               "商誉占净资产": 0, "应收周转天数": 30, "存货周转天数": 60, "ROE": 20}
    high_flag = [{"code": "扣非为负", "命中": True, "严重度": "高", "值": {}}]
    sc = scoring.quality_score(derived, high_flag)
    assert sc["高危封顶"] is True
    assert sc["quality_score"] <= scoring._cfg().get("高危封顶分", 35)
    assert sc["评级"] in ("差", "风险")


# ————————————————————— 无未来函数 —————————————————————
def _install_synthetic_raw(monkeypatch):
    payload = {"code": "000001", "name": "测试股", "periods": _synthetic_periods()}
    monkeypatch.setattr(store, "get_raw",
                        lambda kind, code, date="latest": payload if kind == "financial_report"
                        else (_ for _ in ()).throw(FileNotFoundError(kind)))


def test_no_future_function_analyze(monkeypatch):
    """as_of 在 2025 年报披露日(2026-04-01)之前 → 只见 2024 年报。"""
    _install_synthetic_raw(monkeypatch)
    res = analyzer.analyze("000001", as_of="2025-06-30", persist=False)
    assert set(res["periods"]) == {"2024-12-31"}         # 2025 年报未披露,不可见
    assert res["latest_period"] == "2024-12-31"

    res2 = analyzer.analyze("000001", as_of="2026-05-01", persist=False)
    assert "2025-12-31" in res2["periods"]               # 已披露 → 可见


def test_no_future_function_query(monkeypatch):
    _install_synthetic_raw(monkeypatch)
    # 2024 年报归母增速 None(无 2023 期),as_of 卡在 2025 前只看 2024 → 条件取不到,不命中
    hit_early = analyzer.query(["000001"], as_of="2025-06-30",
                               where={"资产负债率": ("<", 50)})   # 2024 负债率 200/450≈44%
    assert "000001" in hit_early
    hit_late = analyzer.query(["000001"], as_of="2026-05-01",
                              where={"资产负债率": ("<", 50)})     # 2025 负债率 76% → 不命中
    assert hit_late == {}


def test_query_requires_as_of(monkeypatch):
    _install_synthetic_raw(monkeypatch)
    with pytest.raises(ValueError):
        analyzer.query(["000001"], as_of="")


def test_build_financial_block(monkeypatch):
    _install_synthetic_raw(monkeypatch)
    blk = analyzer.build_financial_block("000001", as_of="2026-05-01")
    assert blk["报告期"] == "2025-12-31"
    assert blk["评级"] in ("优", "良", "中", "差", "风险")
    assert "利润表摘要" in blk and blk["利润表摘要"]["营业总收入"] == 120.0
    assert isinstance(blk["flags"], list)


# ————————————————————— 契约 financial 块 —————————————————————
def test_contract_financial_block_enum():
    from tools.contracts import record as rc
    base = {"schema_version": "1.0", "meta": {"code": "000001", "name": "x"},
            "events": [], "timeseries_refs": {}, "provenance": {}}
    ok = dict(base, financial={"评级": "良", "quality_score": 70})
    assert rc.validate_record(ok) == []
    bad = dict(base, financial={"评级": "极好"})
    assert any("financial.评级" in e for e in rc.validate_record(bad))
    # null 宽容
    assert rc.validate_record(dict(base, financial=None)) == []


# ———————————— 步骤2:低基数护栏 + 金融业特判(P1)————————————
def test_low_base_guard_suppresses_small_receivables():
    """低基数护栏:应收增速高但应收占营收极小(如茅台)→ 不判'应收存货激增'(修小基数误杀)。"""
    derived = {"营收增速": 6.0, "应收增速": 80.0, "存货增速": None}
    # 应收/营收 = 10/1000 = 1% < 5% 阈值 → 护栏抑制
    st = {"利润表": {"营业总收入": 1000.0}, "资产负债表": {"应收账款": 10.0}}
    names = [f["code"] for f in flags.evaluate_flags(derived, st)]
    assert "应收存货激增" not in names


def test_low_base_guard_allows_material_receivables():
    """基数充分(应收占营收 20% ≥ 5%)+ 增速超阈 → 正常触发'应收存货激增'。"""
    derived = {"营收增速": 6.0, "应收增速": 80.0, "存货增速": None}
    st = {"利润表": {"营业总收入": 1000.0}, "资产负债表": {"应收账款": 200.0}}
    names = [f["code"] for f in flags.evaluate_flags(derived, st)]
    assert "应收存货激增" in names


def test_financial_industry_skips_inapplicable_flags():
    """金融业特判:银行高负债/短债覆盖等对金融业不适用 → is_financial=True 时跳过;非金融照常判。"""
    derived = {"资产负债率": 91.0, "短债覆盖": 0.2, "现金含量_CFO比净利": 0.1}
    fin_names = [f["code"] for f in flags.evaluate_flags(derived, None, is_financial=True)]
    assert "高负债" not in fin_names and "短债覆盖不足" not in fin_names and "现金含量不足" not in fin_names
    non_fin = [f["code"] for f in flags.evaluate_flags(derived, None, is_financial=False)]
    assert "高负债" in non_fin


def test_financial_industry_keeps_applicable_flags():
    """金融业特判只跳'不适用'红旗;扣非为负等普适红旗对金融业仍要判。"""
    derived = {}
    st = {"利润表": {"扣非归母净利润": -5.0}}
    names = [f["code"] for f in flags.evaluate_flags(derived, st, is_financial=True)]
    assert "扣非为负" in names


# ———————————— 步骤3:审计意见闸门(闸门2,P1)————————————
def test_flag_nonstandard_audit_opinion():
    """非标审计意见 → 高危红旗'非标审计意见';标准无保留/空(季报)不判。"""
    bad = flags.evaluate_flags({}, {"audit_opinion": "保留意见"})
    assert any(f["code"] == "非标审计意见" and f["严重度"] == "高" for f in bad)
    assert not any(f["code"] == "非标审计意见"
                   for f in flags.evaluate_flags({}, {"audit_opinion": "标准无保留意见"}))
    assert not any(f["code"] == "非标审计意见"
                   for f in flags.evaluate_flags({}, {"audit_opinion": None}))  # 季报无意见


def test_audit_gate_downgrades_block(monkeypatch):
    """最新年报非标意见 → build_financial_block 传导:评级降'风险' + 闸门=不通过 + 补红旗。"""
    periods = _synthetic_periods()
    periods["2025-12-31"]["audit_opinion"] = "无法表示意见"     # 年报非标
    payload = {"code": "000001", "name": "测试股", "periods": periods}
    monkeypatch.setattr(store, "get_raw",
                        lambda kind, code, date="latest": payload if kind == "financial_report"
                        else (_ for _ in ()).throw(FileNotFoundError(kind)))
    blk = analyzer.build_financial_block("000001", as_of="2026-05-01")
    assert blk["审计意见闸门"] == "不通过"
    assert blk["评级"] == "风险"
    assert "非标审计意见" in blk["flags"]


def test_audit_gate_pass_marks_through(monkeypatch):
    """标准无保留 → 闸门=通过,不强降评级。"""
    periods = _synthetic_periods()
    periods["2025-12-31"]["audit_opinion"] = "标准无保留意见"
    payload = {"code": "000001", "name": "测试股", "periods": periods}
    monkeypatch.setattr(store, "get_raw",
                        lambda kind, code, date="latest": payload if kind == "financial_report"
                        else (_ for _ in ()).throw(FileNotFoundError(kind)))
    blk = analyzer.build_financial_block("000001", as_of="2026-05-01")
    assert blk["审计意见闸门"] == "通过"


# ———————————— 步骤4:财报专家(接入合议决策层,P1)————————————
def test_expert_caibao_direction_and_veto():
    """财报专家:评级→方向/强度;审计闸门不通过→一票否决看空;无块→弃权。"""
    from tools.analysis import experts
    good = experts.expert_财报({"meta": {"code": "1"},
                               "financial": {"评级": "良", "quality_score": 70,
                                             "flags": [], "审计意见闸门": "通过", "is_forecast": False}}).to_dict()
    assert good["方向"] == "看多" and good["强度"] > 0
    risk = experts.expert_财报({"meta": {"code": "1"},
                               "financial": {"评级": "风险", "审计意见闸门": "通过", "is_forecast": False}}).to_dict()
    assert risk["方向"] == "看空" and risk["强度"] < 0
    # 审计闸门否决:即便评级"良",非标 → 强制看空
    veto = experts.expert_财报({"meta": {"code": "1"},
                               "financial": {"评级": "良", "审计意见闸门": "不通过", "is_forecast": False}}).to_dict()
    assert veto["方向"] == "看空" and veto["强度"] == -1.0
    # 无块 → 弃权(中性 + 数据充分度缺失)
    ab = experts.expert_财报({"meta": {"code": "1"}}).to_dict()
    assert ab["方向"] == "中性" and ab["数据充分度"] == "缺失"


def test_expert_caibao_registered_and_in_default_group():
    """财报专家已注册进 BUILTIN 且在合议默认专家组(真正被决策层用上)。"""
    from tools.analysis import experts
    from tools.config.strategy import THRESHOLDS
    assert "财报" in experts.BUILTIN
    assert "财报" in THRESHOLDS["合议"]["默认专家组"]


# ———————————— P2.2:闸门1 审计机构备案核查(M2)————————————
def test_audit_gate_extract_and_check():
    """抽事务所名 + 名录核查:在录/不在录/无名/别名。"""
    from tools.analysis.financial import audit_gate as ag
    on = ag.audit_gate("审计报告 天健会计师事务所（特殊普通合伙）接受委托审计…审计意见 标准无保留意见")
    assert on["闸门1"] == "通过" and on["在录"] is True and on["档位"] == 1
    off = ag.audit_gate("审计报告 张三会计师事务所（特殊普通合伙）审计…")
    assert off["闸门1"] == "不通过" and off["在录"] is False
    assert ag.audit_gate("本段无事务所")["闸门1"] == "未知"       # 抽不到名→不判
    assert ag.check_auditor("普华永道中天会计师事务所")["在录"] is True   # 四大在录


def test_audit_firm_gate_downgrades_block(monkeypatch):
    """build_financial_block 集成闸门1:年报审计机构不在录 → 评级降'风险'+补红旗+闸门=不通过。"""
    periods = _synthetic_periods()
    fin_payload = {"code": "000001", "name": "测试股", "periods": periods}
    ar_payload = {"code": "000001", "disclosure_date": "2026-04-01",
                  "段落": {"审计报告": "审计报告 野鸡会计师事务所（特殊普通合伙）审计…审计意见 标准无保留意见"}}

    def fake_get_raw(kind, code, date="latest"):
        if kind == "financial_report":
            return fin_payload
        if kind == "annual_report_text":
            return ar_payload
        raise FileNotFoundError(kind)
    monkeypatch.setattr(store, "get_raw", fake_get_raw)
    blk = analyzer.build_financial_block("000001", as_of="2026-05-01")
    assert blk["审计机构闸门"] == "不通过"
    assert blk["评级"] == "风险"
    assert "审计机构未备案" in blk["flags"]


def test_audit_firm_gate_pass_in_registry(monkeypatch):
    """审计机构在录 → 闸门1=通过,不强降评级。"""
    fin_payload = {"code": "000001", "name": "测试股", "periods": _synthetic_periods()}
    ar_payload = {"code": "000001", "disclosure_date": "2026-04-01",
                  "段落": {"审计报告": "审计报告 天健会计师事务所（特殊普通合伙）审计…"}}
    monkeypatch.setattr(store, "get_raw",
                        lambda kind, code, date="latest": fin_payload if kind == "financial_report"
                        else (ar_payload if kind == "annual_report_text"
                              else (_ for _ in ()).throw(FileNotFoundError(kind))))
    blk = analyzer.build_financial_block("000001", as_of="2026-05-01")
    assert blk["审计机构闸门"] == "通过"
    assert "审计机构未备案" not in blk["flags"]


def test_expert_caibao_firm_gate_veto():
    """财报专家:审计机构闸门不通过 → 一票否决看空(即便评级良)。"""
    from tools.analysis import experts
    v = experts.expert_财报({"meta": {"code": "1"},
                           "financial": {"评级": "良", "审计意见闸门": "通过",
                                         "审计机构闸门": "不通过", "is_forecast": False}}).to_dict()
    assert v["方向"] == "看空" and v["强度"] == -1.0


# ———————————— P2.3:LLM 文本层(M2)————————————
def test_llm_text_degrades_when_not_configured(monkeypatch):
    """LLM 未配置 → 文本层返回 {qualitative:None, verdict:None}(不阻断)。"""
    from tools.analysis.financial import llm_text
    from tools.llm import client as lc
    monkeypatch.setattr(lc, "is_configured", lambda: False)
    res = llm_text.analyze_text("600519", "贵州茅台", {"MD&A": "一些正文", "风险": "一些风险"})
    assert res == {"qualitative": None, "verdict": None}


def test_llm_text_no_sections_returns_null(monkeypatch):
    """有 LLM 但无 MD&A/风险文本 → qualitative/verdict 均 None。"""
    from tools.analysis.financial import llm_text
    from tools.llm import client as lc
    monkeypatch.setattr(lc, "is_configured", lambda: True)
    res = llm_text.analyze_text("x", "测试", {"MD&A": None, "风险": None})
    assert res == {"qualitative": None, "verdict": None}


def test_block_merges_financial_text_view(monkeypatch):
    """build_financial_block 读预算的 financial_text code_view → 合入 qualitative/verdict(不触发 LLM)。"""
    fin_payload = {"code": "000001", "name": "测试股", "periods": _synthetic_periods()}
    ft_view = {"qualitative": {"增长来源": "产品放量"}, "verdict": {"综合评级": "良"}}

    def fake_get_raw(kind, code, date="latest"):
        if kind == "financial_report":
            return fin_payload
        raise FileNotFoundError(kind)
    monkeypatch.setattr(store, "get_raw", fake_get_raw)
    monkeypatch.setattr(store, "get_code_view",
                        lambda name, code, date="latest": ft_view if name == "financial_text"
                        else (_ for _ in ()).throw(FileNotFoundError(name)))
    blk = analyzer.build_financial_block("000001", as_of="2026-05-01")
    assert blk["qualitative"] == {"增长来源": "产品放量"}
    assert blk["verdict"] == {"综合评级": "良"}


def test_financial_structural_fallback_detects_bank(monkeypatch):
    """行业名解析不到时,结构信号(无营业成本+无存货)兜底识别银行 → 跳过高负债误杀。"""
    bank_periods = {"2025-12-31": {"report_date": "2025-12-31", "disclosure_date": "2026-04-01",
                    "report_type": "年报",
                    "利润表": {"营业总收入": None, "营业成本": None, "归母净利润": 100.0, "扣非归母净利润": 100.0},
                    "资产负债表": {"资产总计": 10000.0, "负债合计": 9200.0, "股东权益合计": 800.0,
                                "归母股东权益": 800.0, "存货": None},
                    "现金流量表": {}}}
    payload = {"code": "601838", "name": "某银行", "periods": bank_periods}
    monkeypatch.setattr(store, "get_raw",
                        lambda kind, code, date="latest": payload if kind == "financial_report"
                        else (_ for _ in ()).throw(FileNotFoundError(kind)))
    monkeypatch.setattr(store, "get_code_view",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("x")))
    blk = analyzer.build_financial_block("601838", as_of="2026-05-01")  # 不传 industry
    assert blk["金融业口径"] is True          # 结构兜底认出银行
    assert "高负债" not in blk["flags"]       # 高负债被金融业特判跳过
