"""产出缺口护栏 tools/ops/output_guard.py 的语义锁。

为什么有这些断言(防未来改写时被无意删掉):
- 趋势跟随口径(SEPA)是本项目唯一趋势策略,曾因日期漂移(定时任务跨午夜/机器睡眠唤醒后
  用 wall-clock 写到 D+1)在 08-28、09-03 两次静默零产出。护栏的价值就是让"没产出"必须
  留痕、告警、可监控,绝不静默;
- 文件在但 as_of 不符 = 正是漂移那类 bug 的指纹,必须判为缺失(不能只查文件存在);
- 非交易日不得误报;
- CLI 齐全退 0、缺口退 2(非零供定时任务 `||` 捕获)。
设计见 docs/计划/开发日志_20260904_SEPA日期漂移修复.md
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.ops import output_guard as og

REPO = Path(__file__).resolve().parents[1]
DATE = "2026-09-03"


def _write_view(analysis_dir: Path, view: str, date: str, as_of: str | None = None):
    d = analysis_dir / date
    d.mkdir(parents=True, exist_ok=True)
    payload = {"as_of": as_of if as_of is not None else date, "session": "收盘", "rows": []}
    (d / f"{view}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _all_present(analysis_dir: Path, date: str, as_of: str | None = None):
    for v in og.DEFAULT_REQUIRED:
        _write_view(analysis_dir, v, date, as_of=as_of)


def test_齐全时ok(tmp_path):
    _all_present(tmp_path, DATE)
    r = og.check_outputs(DATE, analysis_dir=tmp_path, trading_day=True)
    assert r["ok"] is True and r["missing"] == [] and r["skipped"] is False


def test_缺任一视图判缺口(tmp_path):
    # 只写两个,缺 SEPA雷达
    _write_view(tmp_path, "SEPA合格池", DATE)
    _write_view(tmp_path, "SEPA观察池", DATE)
    r = og.check_outputs(DATE, analysis_dir=tmp_path, trading_day=True)
    assert r["ok"] is False
    assert {m["view"] for m in r["missing"]} == {"SEPA雷达"}


def test_全缺判缺口(tmp_path):
    r = og.check_outputs(DATE, analysis_dir=tmp_path, trading_day=True)
    assert r["ok"] is False
    assert {m["view"] for m in r["missing"]} == set(og.DEFAULT_REQUIRED)


def test_as_of不符判缺口_漂移指纹(tmp_path):
    # 文件都在,但 as_of 写成了 D+1(正是 08-28/09-03 漂移那类 bug)——不能当已产出放过
    _all_present(tmp_path, DATE, as_of="2026-09-04")
    r = og.check_outputs(DATE, analysis_dir=tmp_path, trading_day=True)
    assert r["ok"] is False
    assert all("as_of 不符" in m["reason"] for m in r["missing"])


def test_坏json判缺口(tmp_path):
    _write_view(tmp_path, "SEPA合格池", DATE)
    _write_view(tmp_path, "SEPA观察池", DATE)
    d = tmp_path / DATE
    (d / "SEPA雷达.json").write_text("{不是合法json", encoding="utf-8")
    r = og.check_outputs(DATE, analysis_dir=tmp_path, trading_day=True)
    assert r["ok"] is False
    assert any(m["view"] == "SEPA雷达" and "JSON" in m["reason"] for m in r["missing"])


def test_非交易日跳过不误报(tmp_path):
    # 非交易日即使一个视图都没有,也不告警
    r = og.check_outputs(DATE, analysis_dir=tmp_path, trading_day=False)
    assert r["ok"] is True and r["skipped"] is True


def test_marker_缺口才写(tmp_path):
    r_ok = og.check_outputs(DATE, analysis_dir=tmp_path, trading_day=True)  # 全缺
    m = og.write_marker(r_ok, analysis_dir=tmp_path)
    assert m is not None and m.exists()
    payload = json.loads(m.read_text(encoding="utf-8"))
    assert payload["date"] == DATE and len(payload["missing"]) == len(og.DEFAULT_REQUIRED)


def test_marker_齐全不写(tmp_path):
    _all_present(tmp_path, DATE)
    r = og.check_outputs(DATE, analysis_dir=tmp_path, trading_day=True)
    assert og.write_marker(r, analysis_dir=tmp_path) is None
    assert not (tmp_path / DATE / og.MARKER_NAME).exists()


def test_自定义required(tmp_path):
    _write_view(tmp_path, "面板", DATE)
    r = og.check_outputs(DATE, analysis_dir=tmp_path, required=("面板",), trading_day=True)
    assert r["ok"] is True


def _run_cli(analysis_root: Path, *args):
    """在子进程跑 CLI,用 monkeypatch 不便跨进程,故指向真实 PROJECT_ROOT 下的 data/analysis。
    这里改用直接调用 _main 更稳(CLI 退出码语义)。"""
    return og._main(list(args))


def test_cli退出码_齐全0_缺口2(tmp_path, monkeypatch):
    # 把护栏的 analysis 根指到 tmp,交易日固定为 True
    monkeypatch.setattr(og, "_analysis_root", lambda: tmp_path)
    monkeypatch.setattr(og.trade_cal, "is_trading_day", lambda d: True)

    # 缺口 → 退 2
    assert og._main(["--date", DATE, "--marker"]) == 2
    assert (tmp_path / DATE / og.MARKER_NAME).exists()

    # 补齐 → 退 0
    _all_present(tmp_path, DATE)
    assert og._main(["--date", DATE]) == 0


def test_cli非交易日退0(tmp_path, monkeypatch):
    monkeypatch.setattr(og, "_analysis_root", lambda: tmp_path)
    monkeypatch.setattr(og.trade_cal, "is_trading_day", lambda d: False)
    assert og._main(["--date", DATE]) == 0
