"""数据契约单测:枚举/校验器 + 真实记录合规性。"""
import glob

import pytest

from tools.config import settings
from tools.contracts import record as rc


def _good_record():
    return {
        "schema_version": "1.0",
        "meta": {"code": "000021", "name": "深科技", "sector": "半导体",
                 "industry": "存储", "as_of": "2026-08-06"},
        "signals": {"trend": {"评级": "偏空", "得分": -30, "依据": []},
                    "ob_os": {"verdict": "中性"},
                    "reversal": {"拐点标签": "超跌待反弹", "拐点评分": 45}},
        "prediction": {"买卖倾向": {"结论": "观望", "得分": 1, "依据": []},
                       "持有期建议": {"1日": {}, "5日": {}, "10日": {}}},
        "sentiment": {"净情绪分": 0.45, "利好数": 1, "利空数": 0, "样本数": 1,
                      "events": [{"影响方向": "利好", "与本股关系": "直接",
                                  "层": "公司行为", "影响强度": 4}]},
        "events": [{"date": "2026-08-05", "type": "回购", "impact": "利好", "title": "x"}],
        "timeseries_refs": {}, "provenance": {},
    }


def test_valid_record_passes():
    assert rc.validate_record(_good_record()) == []
    assert rc.is_valid(_good_record())


def test_bad_code():
    r = _good_record(); r["meta"]["code"] = 21
    assert any("code" in e for e in rc.validate_record(r))


def test_bad_enum_direction():
    r = _good_record(); r["sentiment"]["events"][0]["影响方向"] = "大利好"
    assert any("影响方向" in e for e in rc.validate_record(r))


def test_bad_trend_rating():
    r = _good_record(); r["signals"]["trend"]["评级"] = "看多"
    assert any("trend.评级" in e for e in rc.validate_record(r))


def test_null_blocks_tolerated():
    """数据不可用=null 应视为合规(不误报)。"""
    r = _good_record()
    r["signals"] = None; r["prediction"] = None; r["sentiment"] = None
    assert rc.validate_record(r) == []


def test_missing_required_top():
    r = _good_record(); del r["timeseries_refs"]
    assert any("timeseries_refs" in e for e in rc.validate_record(r))


def test_persistence_fields_valid():
    """持续性研判(顶层 rollup + 事件级字段)合法值通过校验。"""
    r = _good_record()
    r["sentiment"]["持续性研判"] = {"结构性利好数": 1, "结构性利空数": 0, "短暂事件数": 0,
                                    "已分类数": 1, "最强结构印证": "强"}
    r["sentiment"]["events"][0].update({"持续性": "结构性持续", "印证强度": "强",
                                        "持续性方向": "利好", "持续性依据": "在手订单饱满"})
    assert rc.validate_record(r) == []


def test_persistence_fields_absent_still_valid():
    """旧记录无持续性字段(未分类)仍合规(向后兼容)。"""
    r = _good_record()
    assert "持续性" not in r["sentiment"]["events"][0]
    assert "持续性研判" not in r["sentiment"]
    assert rc.validate_record(r) == []


def test_bad_persistence_enum_caught():
    r = _good_record()
    r["sentiment"]["events"][0]["持续性"] = "永久性"
    assert any("持续性" in e for e in rc.validate_record(r))
    r2 = _good_record()
    r2["sentiment"]["events"][0]["印证强度"] = "超强"
    assert any("印证强度" in e for e in rc.validate_record(r2))
    r3 = _good_record()
    r3["sentiment"]["持续性研判"] = {"最强结构印证": "巨强"}
    assert any("最强结构印证" in e for e in rc.validate_record(r3))


def test_persistence_enums_registered():
    assert rc.ENUMS["持续性"] == ("结构性持续", "短暂事件", "中性")
    assert rc.ENUMS["印证强度"] == ("强", "中", "弱")


def test_dump_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "SCHEMA_JSON", tmp_path / "record.schema.json")
    p = rc.dump_schema()
    import json
    data = json.loads(open(p, encoding="utf-8").read())
    assert "enums" in data and "影响方向" in data["enums"]


def test_all_real_records_valid():
    """全池已产出的真实记录都应通过契约(回归:抓 serialize 产出漂移)。"""
    files = [f for f in glob.glob(str(settings.PROJECT_ROOT / "data/analysis/*.json"))
             if not f.endswith(("panel.json", "screen.json"))]
    if not files:
        pytest.skip("无 data/analysis 记录")
    import json
    bad = {}
    for f in files:
        errs = rc.validate_record(json.load(open(f, encoding="utf-8")))
        if errs:
            bad[f.rsplit("/", 1)[-1]] = errs
    assert not bad, f"契约不符的记录: {bad}"
