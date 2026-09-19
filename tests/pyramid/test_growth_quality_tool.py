"""growth_quality 语义锁。锁死成长盈利档位(增速/负债率/毛利/净利率) + ROE未年化口径 +
财报明细保字段 + 缺数据NA不编（Wave2 基本面·成长盈利）。"""
import os
import json
import pytest

from tools.pyramid.registry import get
from tools.pyramid.tools.growth_quality_tool import (
    _增速档, _负债率档, _毛利率档, _净利率档, _报告类型, _亿, GrowthQualityTool,
)
from tools.pyramid.tools.financial_redflag_tool import _QUALITY档
from tools.pyramid._common import 格档
import tools.pyramid.tools  # noqa: F401  触发 register

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("GROWTH_TEST_DATA_ROOT") or os.environ.get("REDFLAG_TEST_DATA_ROOT") or ROOT
AS_OF = "2026-09-18"
CODE = "000026"


# ── 增速档边界锁（营收/净利共用：衰退≤-20/下滑≤0/平稳≤15/增长≤30/高增长）──
def test_增速档位边界():
    assert 格档(-25, _增速档)[0] == "衰退"
    assert 格档(-20, _增速档)[0] == "衰退"
    assert 格档(-0.1, _增速档)[0] == "下滑"
    assert 格档(0, _增速档)[0] == "下滑"
    assert 格档(15, _增速档)[0] == "平稳"
    assert 格档(30, _增速档)[0] == "增长"
    assert 格档(30.1, _增速档)[0] == "高增长"


# ── 负债率档边界锁（低≤30/中≤50/偏高≤70/高）──
def test_负债率档位边界():
    assert 格档(30, _负债率档)[0] == "低"
    assert 格档(50, _负债率档)[0] == "中"
    assert 格档(70, _负债率档)[0] == "偏高"
    assert 格档(70.1, _负债率档)[0] == "高"


# ── 毛利率/净利率档边界锁（跨行业粗参考）──
def test_毛利率档位边界():
    assert 格档(20, _毛利率档)[0] == "低"
    assert 格档(40, _毛利率档)[0] == "中"
    assert 格档(60, _毛利率档)[0] == "高"
    assert 格档(60.1, _毛利率档)[0] == "很高"


def test_净利率档位边界():
    assert 格档(5, _净利率档)[0] == "薄"
    assert 格档(15, _净利率档)[0] == "中"
    assert 格档(30, _净利率档)[0] == "厚"
    assert 格档(30.1, _净利率档)[0] == "很厚"


# ── 报告类型推断（未年化提示用）──
def test_报告类型推断():
    assert _报告类型("20260331") == "一季报"
    assert _报告类型("2026-06-30") == "半年报"
    assert _报告类型("20260930") == "三季报"
    assert _报告类型("20261231") == "年报"
    assert _报告类型(None) == ""
    assert _报告类型("bad") == ""


def test_亿换算():
    assert _亿(989349187.28) == 9.89
    assert _亿(None) is None


# ── 脱敏假 json fixture ──
def _write_fake(tmp_path, code, fundamental=None, financial=None, valuation=None):
    d = os.path.join(str(tmp_path), "data", "analysis", AS_OF)
    os.makedirs(d, exist_ok=True)
    doc = {}
    if fundamental is not None:
        doc["fundamental"] = fundamental
    if financial is not None:
        doc["financial"] = financial
    if valuation is not None:
        doc["valuation"] = valuation
    with open(os.path.join(d, f"{code}.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    return str(tmp_path)


# ── ROE 未年化口径 + 强弱挂 five_dims.回报（方案a·不设年化绝对档）──
def test_ROE未年化口径挂回报维(tmp_path):
    root = _write_fake(
        tmp_path, "600010",
        fundamental={"营收": 1e9, "净利": 1e8, "营收增速": 7.0, "净利增速": 40.0,
                     "ROE": 1.93, "毛利率": 34.0, "净利率": 6.5, "负债率": 10.0,
                     "每股股利": None},
        financial={"five_dims": {"回报": 24.78}},
        valuation={"报告期": "20260331"},
    )
    r = GrowthQualityTool().run(AS_OF, "600010", root=root)
    盈利 = next(it for it in r.字段解读 if it["名"] == "盈利能力")
    assert "一季报未年化" in 盈利["口径"]
    assert "回报维24.78" in 盈利["意味"]
    assert "年化15%" in 盈利["口径"]  # 明标不可直接比年化
    # 不出现任何年化 ROE 绝对档名（优/良/中/弱），只给原始值
    assert "ROE1.93" in 盈利["值"]


# ── 金融业口径：负债率标例外、不套常规档语义 ──
def test_金融业负债率例外(tmp_path):
    root = _write_fake(
        tmp_path, "600011",
        fundamental={"营收": 1e9, "净利": 1e8, "营收增速": 5.0, "净利增速": 5.0,
                     "ROE": 2.0, "毛利率": None, "净利率": None, "负债率": 92.0},
        financial={"金融业口径": True, "five_dims": {"回报": 50.0}},
    )
    r = GrowthQualityTool().run(AS_OF, "600011", root=root)
    负债 = next(it for it in r.字段解读 if it["名"] == "负债率")
    assert "金融业口径" in 负债["口径"] and "勿套档" in 负债["意味"]  # v2：勿套档 caveat 进意味


# ── quality 复用 financial_redflag._QUALITY档（单一口径源）──
def test_quality复用口径():
    assert 格档(85, _QUALITY档)[0] == "优"
    assert 格档(68.5, _QUALITY档)[0] == "良"
    assert 格档(30, _QUALITY档)[0] == "差"


# ── 缺数据：无 fundamental → missing 不编 ──
def test_缺fundamental_missing():
    r = GrowthQualityTool().run(AS_OF, "999999", root=DATA_ROOT)
    assert r.freshness == "missing"
    assert "数据缺失" in r.浓缩块 and r.面 == "基本面"


# ── 部分缺数据：增速缺 → 该条标增速缺失、值NA 不编；股利None→NA ──
def test_字段缺数据标NA不编(tmp_path):
    root = _write_fake(
        tmp_path, "600012",
        fundamental={"营收": 1e9, "净利": None, "营收增速": None, "净利增速": None,
                     "ROE": None, "毛利率": None, "净利率": None, "负债率": None,
                     "每股股利": None},
        financial={},
    )
    r = GrowthQualityTool().run(AS_OF, "600012", root=root)
    营收 = next(it for it in r.字段解读 if it["名"] == "营收")
    assert 营收["口径"] == "增速缺失"
    股利 = next(it for it in r.字段解读 if it["名"] == "每股股利")
    assert 股利["值"] == "NA"
    # 契约保证：所有条目口径/意味非空（不空编）
    for it in r.字段解读:
        assert it["名"] and it["口径"] and it["意味"]


# ── v2 语义锁：财报质量→财报质量汇总(综合句) + 意味影响化 + 口径剔区间共性 ──
def test_v2_财报质量汇总_综合句(tmp_path):
    root = _write_fake(
        tmp_path, "600013",
        fundamental={"营收": 1.19e8, "净利": 1.1e7, "营收增速": 431.0, "净利增速": 600.0,
                     "ROE": 4.09, "毛利率": 20.46, "净利率": 9.28, "负债率": 63.69,
                     "每股股利": None},
        financial={"评级": "中", "quality_score": 56.6, "报告期": "2026-06-30",
                   "five_dims": {"成长": 100.0, "质量": 30.7, "健康": 77.4,
                                 "运营": 81.5, "回报": 18.4}},
    )
    r = GrowthQualityTool().run(AS_OF, "600013", root=root)
    汇总 = next(it for it in r.字段解读 if it["名"] == "财报质量汇总")
    assert "财报质量" not in {it["名"] for it in r.字段解读} - {"财报质量汇总"}  # 旧名已改
    assert 汇总["意味"].startswith("影响：")
    # 综合句点出最强/最弱维（成长100最强、回报18.4最弱）
    assert "成长100最强" in 汇总["意味"] and "回报18.4最弱" in 汇总["意味"]
    # 增速档进卡的意味影响化、口径只留档名（区间进词表）
    营收 = next(it for it in r.字段解读 if it["名"] == "营收")
    assert 营收["口径"] == "高增长" and 营收["意味"].startswith("影响：")
    assert "≤" not in 营收["口径"]  # 区间不在个股卡口径重复


# ── v2 五维逐维锁：dims 存在 → 五维各一行(值+档+影响)；G3 上限=13 ──
def test_v2_五维逐维展开(tmp_path):
    root = _write_fake(
        tmp_path, "600014",
        fundamental={"营收": 1e9, "净利": 1e8, "营收增速": 431.0, "净利增速": 600.0,
                     "ROE": 4.09, "毛利率": 20.0, "净利率": 9.0, "负债率": 63.0,
                     "每股股利": None},
        financial={"评级": "中", "quality_score": 56.6, "报告期": "2026-06-30",
                   "利润表摘要": {"归母净利增速": 272.0, "扣非净利增速": 165.0, "营收增速": 474.0},
                   "five_dims": {"成长": 100.0, "质量": 30.69, "健康": 77.44,
                                 "运营": 81.5, "回报": 18.38}},
    )
    r = GrowthQualityTool().run(AS_OF, "600014", root=root)
    名集 = {it["名"] for it in r.字段解读}
    for 维 in ("成长", "质量", "健康", "运营", "回报"):
        assert 维 in 名集, f"缺五维逐维行 {维}"
    成长 = next(it for it in r.字段解读 if it["名"] == "成长")
    assert 成长["值"] == "100.00" and 成长["口径"] == "优" and 成长["意味"].startswith("影响：")
    回报 = next(it for it in r.字段解读 if it["名"] == "回报")
    assert 回报["口径"] == "差" and "资本回报低" in 回报["意味"]  # 18.38→差·弱句
    # 财报增速明细保扣非
    增速 = next(it for it in r.字段解读 if it["名"] == "财报增速明细")
    assert "扣非165.0" in 增速["值"]
    # G3 例外：本工具上限=13，实际 12 行 ≤13 不 raise
    assert r.max_浓缩块_行 == 13
    assert len([l for l in r.浓缩块.splitlines() if l.strip()]) <= 13
    assert "财报明细" not in 名集  # 旧单行五维已拆


# ── 真实票集成：000026 关键档锁 + 五维逐维（面=基本面）──
@pytest.mark.skipif(
    not os.path.exists(os.path.join(DATA_ROOT, "data", "analysis", AS_OF, f"{CODE}.json")),
    reason="需主仓 per-stock json（设 GROWTH_TEST_DATA_ROOT 指回主仓）",
)
def test_run_真实票():
    r = get("growth_quality").run(AS_OF, CODE, root=DATA_ROOT)
    assert r.面 == "基本面" and r.freshness == "fresh"
    名集 = {it["名"] for it in r.字段解读}
    # 五维逐维在卡（dims 存在时）；否则退化为"财报五维"缺行
    assert ("回报" in 名集) or ("财报五维" in 名集)
    txt = r.to_prompt()
    assert "净利: " in txt and "高增长" in txt  # 000026 净利增速 → 高增长
    assert "财报质量汇总" in txt                 # 汇总行
    assert "回报维" in txt                       # ROE 强弱挂回报维
    assert len([l for l in r.浓缩块.splitlines() if l.strip()]) <= 13  # G3 例外上限


# ── 缺口二:行业评分档透传(非重算)语义锁 ──
def test_行业评分档_透传毛利率负债率(tmp_path):
    """financial 块带 analyzer 已算的 行业评分档 → 盈利能力/负债率 透传"本行业评分档",
    档=格档(score,_QUALITY档)(与 five_dims 同口径),不是跨行业粗档;口径标"非经验百分位"。"""
    root = _write_fake(
        tmp_path, "600020",
        fundamental={"营收": 1e9, "净利": 1e8, "营收增速": 10.0, "净利增速": 10.0,
                     "ROE": 8.0, "毛利率": 32.5, "净利率": None, "负债率": 45.0, "每股股利": None},
        financial={"金融业口径": False, "five_dims": {"回报": 50.0},
                   "行业评分档": {
                       "毛利率": {"值": 32.5, "行业": "电子", "区间": [10, 55], "score": 50.0},
                       "资产负债率": {"值": 45.0, "行业": "电子", "区间": [75, 30], "score": 66.67}}},
    )
    r = GrowthQualityTool().run(AS_OF, "600020", root=root)
    盈利 = next(it for it in r.字段解读 if it["名"] == "盈利能力")
    assert f"本行业电子评分档{格档(50.0, _QUALITY档)[0]}" in 盈利["值"]   # 透传 score→_QUALITY档,非跨行业_毛利率档
    assert "非经验百分位" in 盈利["口径"] and "本行业评分档" in 盈利["口径"]
    负债 = next(it for it in r.字段解读 if it["名"] == "负债率")
    assert f"本行业电子评分档{格档(66.67, _QUALITY档)[0]}" in 负债["口径"]  # 反向指标:低负债→高分→更优


def test_无行业评分档_回退跨行业粗档(tmp_path):
    """无 行业评分档(通用兜底票)→ 毛利率/负债率 回退跨行业粗档,并明标'跨行业'(行为不变)。"""
    root = _write_fake(
        tmp_path, "600021",
        fundamental={"营收": 1e9, "净利": 1e8, "营收增速": 10.0, "净利增速": 10.0,
                     "ROE": 8.0, "毛利率": 32.5, "净利率": 9.0, "负债率": 45.0, "每股股利": None},
        financial={"金融业口径": False, "five_dims": {"回报": 50.0}},   # 无 行业评分档
    )
    r = GrowthQualityTool().run(AS_OF, "600021", root=root)
    盈利 = next(it for it in r.字段解读 if it["名"] == "盈利能力")
    assert "跨行业" in 盈利["值"] and "跨行业粗参考" in 盈利["口径"]
    assert "本行业" not in 盈利["值"]
    负债 = next(it for it in r.字段解读 if it["名"] == "负债率")
    assert "跨行业粗档" in 负债["口径"]


def test_金融业负债率不透行业档(tmp_path):
    """金融业口径票即便偶带 行业评分档.资产负债率,负债率仍走金融例外(勿套档),不透行业评分档。"""
    root = _write_fake(
        tmp_path, "600022",
        fundamental={"营收": 1e9, "净利": 1e8, "营收增速": 5.0, "净利增速": 5.0,
                     "ROE": 2.0, "毛利率": None, "净利率": None, "负债率": 92.0},
        financial={"金融业口径": True, "five_dims": {"回报": 50.0},
                   "行业评分档": {"资产负债率": {"值": 92.0, "行业": "银行", "区间": [75, 30], "score": 0.0}}},
    )
    r = GrowthQualityTool().run(AS_OF, "600022", root=root)
    负债 = next(it for it in r.字段解读 if it["名"] == "负债率")
    assert "金融业口径" in 负债["口径"] and "勿套档" in 负债["意味"]
    assert "本行业" not in 负债["口径"]


# ── 缺口三①:通用兜底口径明标(行业专家=null 但有 quality/五维 → 打标'通用测算',非数据缺失)──
def test_通用兜底_明标通用口径(tmp_path):
    """行业专家=null(如综合类 000504)但已算出 quality/five_dims:财报质量汇总打标'通用口径'+
    意味标'五维为通用测算,非数据缺失',避免体检卡'显糙'被误读成缺数据。"""
    root = _write_fake(
        tmp_path, "600040",
        fundamental={"营收": 1e9, "净利": 1e8, "营收增速": 5.0, "净利增速": 5.0,
                     "ROE": 4.0, "毛利率": 20.0, "净利率": 9.0, "负债率": 60.0, "每股股利": None},
        financial={"行业专家": None, "评级": "中", "quality_score": 55.0,
                   "报告期": "2026-06-30", "five_dims": {"成长": 50, "质量": 40, "健康": 60,
                                                       "运营": 55, "回报": 30}},
    )
    r = GrowthQualityTool().run(AS_OF, "600040", root=root)
    汇总 = next(it for it in r.字段解读 if it["名"] == "财报质量汇总")
    assert "通用口径(无行业专属专家)" in 汇总["口径"]
    assert "通用测算" in 汇总["意味"] and "非数据缺失" in 汇总["意味"]
    assert r.fields.get("通用兜底") is True and r.fields.get("行业专家") is None


def test_行业命中_不打通用兜底标(tmp_path):
    """行业专家命中(如电子)→ 不打通用兜底标(行为不变)。"""
    root = _write_fake(
        tmp_path, "600041",
        fundamental={"营收": 1e9, "净利": 1e8, "营收增速": 5.0, "净利增速": 5.0,
                     "ROE": 4.0, "毛利率": 20.0, "净利率": 9.0, "负债率": 60.0, "每股股利": None},
        financial={"行业专家": "电子", "评级": "中", "quality_score": 55.0,
                   "报告期": "2026-06-30", "five_dims": {"成长": 50, "质量": 40, "健康": 60,
                                                       "运营": 55, "回报": 30}},
    )
    r = GrowthQualityTool().run(AS_OF, "600041", root=root)
    汇总 = next(it for it in r.字段解读 if it["名"] == "财报质量汇总")
    assert "通用口径" not in 汇总["口径"] and "通用测算" not in 汇总["意味"]
    assert r.fields.get("通用兜底") is False
