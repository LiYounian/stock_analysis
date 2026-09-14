"""Shadow-run 扩样证据框架单测(headless α 证据·II)。

锁"为什么改"的语义(约法第 6 条):
  候选池(shadow_pool):
    1. 全域宽网聚合层(策略0合议/最大范围选股)必须被排除,不计入多命中。
    2. 只认成员资格、不认榜内名次(宽网榜按 code 升序非强度序);多命中优先入池。
    3. 规则确定性可复现:同输入 → 同池;排序键 (hit desc, 最短命中榜 asc, code asc)。
    4. 防未来靠"策略视图为当日产物";构造侧不引入未来数据(此处校验纯函数确定性)。
  α 打分(shadow_score):
    5. 买入侧 = stance∈{买入,可参与};α = 买入侧 r 均值 − 全A等权(全样本 r 均值)。
    6. r_5 仅当日 <= 结算截止(T5_CUTOFF)才算;之后只算 r_1(诚实截断)。
    7. 汇总:日均 α ± SE(n>1 才有 SE) + pooled 命中率。
纯函数 + 临时 fixture,不联网、不碰生产。
"""
from __future__ import annotations

import json
from pathlib import Path

from tools.analysis import shadow_pool as sp
from tools.analysis import shadow_score as ss


# ============================================================
# 候选池构造
# ============================================================
def _write_views(day: Path, views: dict):
    day.mkdir(parents=True, exist_ok=True)
    for name, codes in views.items():
        # 榜单单元用 write_picks 认得的 "入选清单" 键 + code 字段
        obj = {"as_of": day.name, "入选清单": [{"code": c} for c in codes]}
        (day / f"{name}.json").write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def test_excludes_universe_aggregators(tmp_path):
    date = "2026-08-20"
    day = tmp_path / date
    # 策略0合议/最大范围选股 含 A,B 但应被排除 → A,B 不因它们获得命中
    _write_views(day, {
        "动量组合": ["000001", "000009"],
        "箱体形态": ["000002", "000010"],
        "策略0合议": ["000001", "000002", "000009", "000010", "000099"],
        "最大范围选股": ["000001", "000002", "000009", "000010", "000099"],
    })
    _pool, meta = sp.build_pool(str(tmp_path), date)
    assert "策略0合议" not in meta["screens"]
    assert "最大范围选股" not in meta["screens"]
    # 000001 只真正命中 动量组合(1 个选择性策略),排除层不该把它抬成多命中
    assert meta["hit_counts"].get("000001") == 1


def test_multi_hit_priority_and_membership(tmp_path):
    date = "2026-08-20"
    day = tmp_path / date
    # M 同时在两个选择性策略 → hit=2,应排在只命中 1 个的票前面
    _write_views(day, {
        "动量组合": ["600000", "600001", "600002"],
        "放量后缩量回踩": ["600000", "600003", "600004"],
        "趋势深跌反包": ["600005"],
    })
    pool, meta = sp.build_pool(str(tmp_path), date)
    assert meta["hit_counts"]["600000"] == 2
    assert pool[0] == "600000"  # 多命中优先


def test_pool_deterministic_and_capped(tmp_path):
    date = "2026-08-20"
    day = tmp_path / date
    big = [f"{i:06d}" for i in range(40)]
    _write_views(day, {"动量组合": big, "放量后缩量回踩": big[::-1]})
    p1, _ = sp.build_pool(str(tmp_path), date)
    p2, _ = sp.build_pool(str(tmp_path), date)
    assert p1 == p2                    # 确定性
    assert len(p1) <= sp.CAP           # 不超过上限
    assert len(p1) >= sp.MIN           # 至少补到下限


# ============================================================
# α 打分
# ============================================================
def _card():
    # 一日 scorecard:全样本 5 只,r_1 均值 = (1-1+2-2+0)/5 = 0.0
    return {"2026-09-03": {
        "AAA": {"r_1": 1.0, "r_5": 3.0},
        "BBB": {"r_1": -1.0, "r_5": -3.0},
        "CCC": {"r_1": 2.0, "r_5": 4.0},
        "DDD": {"r_1": -2.0, "r_5": -1.0},
        "EEE": {"r_1": 0.0, "r_5": 0.0},
    }}


def test_buyside_alpha_math():
    card = _card()
    units = [
        {"code": "AAA", "stance": "买入"},
        {"code": "CCC", "stance": "可参与"},
        {"code": "BBB", "stance": "观望"},   # 非买入侧,不计
    ]
    r = ss.score_day("2026-09-03", units, card)
    h1 = r["horizons"]["r_1"]
    assert r["buy_side"] == ["AAA", "CCC"]
    assert h1["bench_mean"] == 0.0
    assert h1["buy_mean"] == 1.5            # (1+2)/2
    assert h1["alpha_pp"] == 1.5            # 1.5 - 0.0
    assert h1["hit_rate"] == 1.0            # 两只均 r>0


def test_r5_cutoff_respected():
    card = _card()
    units = [{"code": "AAA", "stance": "买入"}]
    after = ss.score_day("2026-09-10", units, card | {"2026-09-10": card["2026-09-03"]})
    assert "r_5" not in after["horizons"]   # 截止后不算 T+5
    before = ss.score_day("2026-09-03", units, card)
    assert "r_5" in before["horizons"]


def test_aggregate_day_mean_se():
    card = _card()
    day1 = ss.score_day("2026-09-03", [{"code": "AAA", "stance": "买入"}], card)  # α=1.0
    card2 = {"2026-09-04": card["2026-09-03"]}
    day2 = ss.score_day("2026-09-04", [{"code": "CCC", "stance": "买入"}], card2)  # α=2.0
    agg = ss.aggregate([day1, day2], horizon="r_1")
    assert agg["n_days"] == 2
    assert abs(agg["day_mean_alpha_pp"] - 1.5) < 1e-9
    assert agg["day_se_alpha_pp"] is not None
    assert agg["pooled_n_buys"] == 2
