"""tools/review/render：冻结列 + 幂等 upsert + summarize（胜率 r_d1·收益榜 r_exit·未成交单列）。"""
from __future__ import annotations

import pandas as pd

from tools.review.attribution import classify
from tools.review.render import (
    GRAND_COLUMNS,
    summarize,
    to_row,
    write_daily_md,
    write_grand_csv,
)
from tools.review.types import Attribution, ModelALabels, Pick


def _row(code, version="今日选股", 来源="板块催化", r_exit=None, r_d1=None,
         close_positive=None, untriggered=False, status="settled", correct=None, wrong=None):
    p = Pick(date="2026-09-16", code=code, name=code, version_tag=version, 来源=来源, 角色="龙头")
    lab = ModelALabels(filled=(False if untriggered else True), r_d1=r_d1, r_exit=r_exit,
                       close_positive=close_positive, untriggered=untriggered, status=status,
                       alpha_exit=(r_exit - 1 if r_exit is not None else None))
    attr = Attribution(correct_bucket=correct, wrong_bucket=wrong)
    return to_row(p, lab, attr, {"native_sector": None, "native_nextday": None})


def test_to_row_frozen_columns():
    r = _row("000001", r_exit=5.0, r_d1=4.0, close_positive=True)
    assert list(r.keys()) == GRAND_COLUMNS      # 键==冻结列（顺序也锁）


def test_summarize_win_and_return():
    rows = [
        _row("A", r_exit=5.0, r_d1=4.0, close_positive=True),     # 赢
        _row("B", r_exit=-2.0, r_d1=-2.0, close_positive=False),  # 亏
        _row("C", untriggered=True, status="not_entered"),        # 未成交(不进分母/收益榜)
    ]
    s = summarize(rows)
    assert s["n_total"] == 3 and s["n_untriggered"] == 1
    assert s["n_胜率分母"] == 2                  # 未成交剔出
    assert s["胜率_r_d1"] == 0.5                 # 1 赢 / 2
    assert s["平均r_exit_收益榜"] == 1.5          # (5 + -2)/2；未成交不计
    assert abs(s["未成交率"] - round(1 / 3, 4)) < 1e-9


def test_summarize_buckets_and_pending():
    rows = [
        _row("A", r_exit=3.0, r_d1=3.0, close_positive=True, correct="策略"),
        _row("B", r_exit=-1.0, r_d1=-1.0, close_positive=False, wrong="追高"),
        _row("D", r_exit=None, close_positive=None, status="pending"),   # pending 不进收益榜
    ]
    s = summarize(rows)
    assert s["选对桶分布"] == {"策略": 1} and s["选错桶分布"] == {"追高": 1}
    assert s["n_pending"] == 1
    assert s["平均r_exit_收益榜"] == 1.0          # (3 + -1)/2；pending 的 None 不计


def test_grand_csv_idempotent_upsert(tmp_path):
    csv = tmp_path / "grand.csv"
    write_grand_csv([_row("A", r_exit=1.0), _row("B", r_exit=2.0)], csv_path=str(csv))
    # 同 date 重跑（换内容）→ 覆盖当日全部行、不累加
    write_grand_csv([_row("A", r_exit=9.0)], csv_path=str(csv))
    df = pd.read_csv(csv, dtype={"date": str, "code": str})
    assert len(df) == 1                          # 当日行被整体覆盖（B 也随之替换掉）
    assert df.iloc[0]["code"] == "A" and float(df.iloc[0]["r_exit"]) == 9.0


def test_grand_csv_multi_date_accumulate(tmp_path):
    csv = tmp_path / "grand.csv"
    write_grand_csv([_row("A", r_exit=1.0)], csv_path=str(csv))
    r2 = _row("A", r_exit=2.0)
    r2["date"] = "2026-09-17"                     # 不同 date → 累积
    write_grand_csv([r2], csv_path=str(csv))
    df = pd.read_csv(csv, dtype={"date": str})
    assert set(df["date"]) == {"2026-09-16", "2026-09-17"}


def test_write_daily_md(tmp_path):
    rows = [_row("A", r_exit=5.0, r_d1=4.0, close_positive=True, correct="策略")]
    path = write_daily_md("2026-09-16", rows, md_dir=str(tmp_path))
    text = open(path, encoding="utf-8").read()
    assert "统一大复盘 · 2026-09-16" in text
    assert "主榜=r_exit" in text and "策略" in text
