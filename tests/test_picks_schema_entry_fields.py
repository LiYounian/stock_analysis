"""锁：次日实盘口径·研判扩字段的 schema 校验（2026-09-15 整改设计 §3/§6，阶段2A）。

为什么改（守则#6：断言锁住语义，防未来重写误删规则）：
  · **向后兼容是硬要求**——存量选股 json 无这些新字段（缺=None）必须照常通过校验，
    绝不因新增字段而让历史数据报错；
  · entry_type 有值时才校验枚举（a/b/c/open），非法才报错；
  · 数值字段（P_entry/P_dip/expected_close_positive_prob）有值时必须是数字，概率另须 ∈ [0,1]。
"""
from __future__ import annotations

import copy

import datetime as _dt

import pytest

from tools.analysis import picks_schema as ps
from tools.analysis import write_picks as wp


def _ok_doc(**pick_over):
    doc = copy.deepcopy(ps.EXAMPLE_OK)
    doc["picks"][0].update(pick_over)
    return doc


def test_legacy_picks_without_new_fields_pass():
    """存量选股 json（EXAMPLE_OK，完全无新字段）→ 缺=None，照常通过（向后兼容硬要求）。"""
    assert ps.validate_picks(copy.deepcopy(ps.EXAMPLE_OK)) == []


def test_new_fields_all_present_and_valid_pass():
    doc = _ok_doc(
        entry_rule="早盘9:30–10:00触及12.00挂限价买，到14:30未触及放弃",
        entry_type="b", P_entry=12.00, T_obs="09:30–10:00", P_dip=11.80,
        target_line="D+2目标12.9", stop_line="跌破11.6止损",
        expected_close_positive_prob=0.62,
    )
    assert ps.validate_picks(doc) == []


def test_new_fields_explicit_none_pass():
    """新字段显式 None（而非缺失）也必须放行。"""
    doc = _ok_doc(entry_type=None, P_entry=None, P_dip=None,
                  expected_close_positive_prob=None, entry_rule=None)
    assert ps.validate_picks(doc) == []


@pytest.mark.parametrize("et", sorted(ps.ENTRY_TYPE))
def test_entry_type_all_enum_members_pass(et):
    assert ps.validate_picks(_ok_doc(entry_type=et)) == []


def test_entry_type_illegal_errors():
    errs = ps.validate_picks(_ok_doc(entry_type="x"))
    assert any("entry_type" in e for e in errs), errs


def test_numeric_field_wrong_type_errors():
    errs = ps.validate_picks(_ok_doc(P_entry="12元"))
    assert any("P_entry" in e for e in errs), errs


def test_prob_out_of_range_errors():
    assert any("expected_close_positive_prob" in e
               for e in ps.validate_picks(_ok_doc(expected_close_positive_prob=1.5)))
    assert any("expected_close_positive_prob" in e
               for e in ps.validate_picks(_ok_doc(expected_close_positive_prob=-0.1)))


def test_prob_boundaries_pass():
    assert ps.validate_picks(_ok_doc(expected_close_positive_prob=0.0)) == []
    assert ps.validate_picks(_ok_doc(expected_close_positive_prob=1.0)) == []


# —— write_picks 透传：新字段随 canonical doc 落盘（Agent 给才落，缺则不落，向后兼容）——
_NOW = _dt.datetime(2026, 9, 9, 20, 15, 3, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
_FAKE_REC = {"601061": {"meta": {"code": "601061", "name": "中信金属",
                                 "industry": "有色", "as_of": "2026-09-09"},
                        "snapshot": {"close": 12.18, "pct_chg": 4.91}}}


def _loader(code, pick_date):
    return _FAKE_REC.get(code)


def _unit(**over):
    u = {"code": "601061", "type": "买入候选", "buy_rank": 1, "stance": "买入",
         "dir_1d": "偏多", "dir_1d_conf": "中", "dir_5d": "偏多", "dir_5d_conf": "中",
         "sentiment_quality": "ok", "key_reason": "r", "key_risk": "k",
         "detail_anchor": "选股/2026-09-09.md#1"}
    u.update(over)
    return u


def test_write_picks_passthrough_new_fields(tmp_path):
    doc, errs = wp.build_picks_json(
        "2026-09-09", [_unit(entry_type="a", P_entry=12.0, target_line="D+2 12.9",
                             stop_line="11.6", entry_rule="开盘价买", T_obs="集合竞价",
                             expected_close_positive_prob=0.6)],
        record_loader=_loader, analysis_dir=tmp_path, now=_NOW, predict_for="2026-09-10")
    assert errs == [], errs
    p = doc["picks"][0]
    assert p["entry_type"] == "a" and p["P_entry"] == 12.0
    assert p["target_line"] == "D+2 12.9" and p["stop_line"] == "11.6"
    assert p["expected_close_positive_prob"] == 0.6


def test_write_picks_without_new_fields_omits_keys(tmp_path):
    """Agent 不给新字段 → canonical pick 不含这些键（缺=None，向后兼容），且校验通过。"""
    doc, errs = wp.build_picks_json(
        "2026-09-09", [_unit()], record_loader=_loader,
        analysis_dir=tmp_path, now=_NOW, predict_for="2026-09-10")
    assert errs == [], errs
    p = doc["picks"][0]
    assert "entry_type" not in p and "P_entry" not in p
