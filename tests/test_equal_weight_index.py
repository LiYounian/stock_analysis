"""全A等权净值指数(α 基准取数)单测。

正确性红线:
  · 日度 mean_pct 链成复利净值,锚定首日=base;
  · 缺 mean_pct 的日跳过(不假造 0)、level_at 对缺失日返回 None;
  · breadth 目录缺失 → 空,不报错。

hermetic:用 tmp breadth 目录喂脚本化 json,不读真实 data/breadth。
"""
import json

from tools.analysis import equal_weight_index as ewi


def _write(d, date, mean_pct):
    payload = {"date": date} if mean_pct is None else {"date": date, "mean_pct": mean_pct}
    (d / f"{date}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_net_value_compounds(tmp_path):
    _write(tmp_path, "2026-09-01", 1.0)    # +1%
    _write(tmp_path, "2026-09-02", -0.5)   # -0.5%
    s = ewi.net_value_series(tmp_path, base=1000.0)
    assert s["2026-09-01"] == 1000.0 * 1.01
    assert s["2026-09-02"] == 1000.0 * 1.01 * 0.995


def test_level_at(tmp_path):
    _write(tmp_path, "2026-09-01", 2.0)
    assert ewi.level_at("2026-09-01", tmp_path, base=1000.0) == 1020.0
    assert ewi.level_at("2026-09-09", tmp_path) is None      # 无该日 → None


def test_missing_mean_pct_skipped(tmp_path):
    _write(tmp_path, "2026-09-01", 1.0)
    _write(tmp_path, "2026-09-02", None)   # mean_pct 缺 → 跳过
    s = ewi.net_value_series(tmp_path, base=1000.0)
    assert "2026-09-02" not in s
    assert s["2026-09-01"] == 1010.0


def test_missing_dir_empty(tmp_path):
    assert ewi.net_value_series(tmp_path / "nope") == {}
