"""闭环产出回写主仓单测(tools.sync.mirror_to_main)。

锁住语义(防未来 prompt/代码重写时无意删掉规则):
- analysis 整目录 + scorecard + receipt 落到主仓目标绝对路径;
- receipt **最后写**(它是选股门控就绪信号,必在 analysis 全套就位后才出现);
- 源==目标(在主仓自身跑)→ no-op,不自拷自;
- backtest 只拷 *forward_scorecard*.csv,绝不碰同目录已跟踪 fixture;
- 逐文件原子拷(不残留临时文件);
- 只 copy、不产生任何 git 变更(纯文件系统操作,不 import git)。
"""
from __future__ import annotations

from pathlib import Path

from tools.sync import mirror_to_main as M


def _seed_source(src: Path, date: str) -> None:
    """在源(模拟 dailyjob)造出当日 analysis + scorecard + receipt。"""
    daily = src / "data" / "analysis" / date
    daily.mkdir(parents=True)
    (daily / "600000.json").write_text('{"code":"600000"}', encoding="utf-8")
    (daily / "market_forecast.json").write_text('{"as_of":"%s"}' % date, encoding="utf-8")
    (daily / "估值位监控.json").write_text("{}", encoding="utf-8")
    sub = daily / "sub"                       # 子目录也要被递归拷
    sub.mkdir()
    (sub / "note.txt").write_text("x", encoding="utf-8")

    bt = src / "data" / "analysis" / "backtest"
    bt.mkdir(parents=True)
    (bt / "forward_scorecard.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (bt / "lhb_forward_scorecard.csv").write_text("c,d\n3,4\n", encoding="utf-8")
    (bt / "eval_v3.json").write_text('{"tracked":"fixture"}', encoding="utf-8")   # 已跟踪 fixture,不该被拷

    rc = src / "data" / "sync_receipts"
    rc.mkdir(parents=True)
    (rc / f"{date}.json").write_text('{"date":"%s","shards":{}}' % date, encoding="utf-8")


def test_mirror_lands_analysis_and_receipt(tmp_path):
    """analysis 整目录(含子目录)+ receipt 落到主仓目标。"""
    src, dst = tmp_path / "dailyjob", tmp_path / "main"
    date = "2026-09-11"
    _seed_source(src, date)

    r = M.mirror_daily_to_main(date, source_root=src, main_root=dst)

    assert r["mirrored"] is True
    dst_daily = dst / "data" / "analysis" / date
    assert (dst_daily / "600000.json").read_text(encoding="utf-8") == '{"code":"600000"}'
    assert (dst_daily / "market_forecast.json").exists()
    assert (dst_daily / "估值位监控.json").exists()
    assert (dst_daily / "sub" / "note.txt").read_text(encoding="utf-8") == "x"
    assert (dst / "data" / "sync_receipts" / f"{date}.json").exists()
    assert r["receipt"] is True
    assert r["analysis_files"] == 4          # 600000 + market_forecast + 估值位监控 + sub/note


def test_scorecard_only_forward_not_tracked_fixtures(tmp_path):
    """backtest 只拷 *forward_scorecard*.csv;已跟踪 fixture(eval_v3.json)绝不被拷。"""
    src, dst = tmp_path / "dailyjob", tmp_path / "main"
    date = "2026-09-11"
    _seed_source(src, date)

    r = M.mirror_daily_to_main(date, source_root=src, main_root=dst)

    dst_bt = dst / "data" / "analysis" / "backtest"
    assert (dst_bt / "forward_scorecard.csv").exists()
    assert (dst_bt / "lhb_forward_scorecard.csv").exists()
    assert not (dst_bt / "eval_v3.json").exists()   # 跟踪 fixture 不能脏到主仓
    assert r["scorecard_files"] == 2


def test_receipt_written_last(tmp_path, monkeypatch):
    """receipt 必在所有 analysis 文件之后写(就绪信号语义)。用文件写入调用序断言。"""
    src, dst = tmp_path / "dailyjob", tmp_path / "main"
    date = "2026-09-11"
    _seed_source(src, date)

    order: list[str] = []
    real_copy = M._atomic_copy_file

    def spy(s: Path, d: Path):
        order.append(str(d))
        real_copy(s, d)

    monkeypatch.setattr(M, "_atomic_copy_file", spy)
    M.mirror_daily_to_main(date, source_root=src, main_root=dst)

    receipt_idx = next(i for i, p in enumerate(order) if "sync_receipts" in p)
    analysis_idxs = [i for i, p in enumerate(order) if f"analysis/{date}" in p or f"analysis\\{date}" in p]
    scorecard_idxs = [i for i, p in enumerate(order) if "backtest" in p]
    assert analysis_idxs and scorecard_idxs
    assert receipt_idx == len(order) - 1                 # receipt 是最后一个写的
    assert receipt_idx > max(analysis_idxs + scorecard_idxs)


def test_noop_when_source_equals_target(tmp_path):
    """源==目标(在主仓自身跑)→ no-op,不自拷自。"""
    root = tmp_path / "main"
    date = "2026-09-11"
    _seed_source(root, date)

    r = M.mirror_daily_to_main(date, source_root=root, main_root=root)

    assert r["mirrored"] is False
    assert "no-op" in r["reason"]
    assert r["analysis_files"] == 0 and r["receipt"] is False


def test_atomic_no_tmp_leftover(tmp_path):
    """逐文件原子拷,完成后目标目录无残留临时文件。"""
    src, dst = tmp_path / "dailyjob", tmp_path / "main"
    date = "2026-09-11"
    _seed_source(src, date)

    M.mirror_daily_to_main(date, source_root=src, main_root=dst)

    leftovers = [p for p in (dst / "data").rglob("*.tmp")]
    assert leftovers == []
    hidden_tmp = [p for p in (dst / "data").rglob(".*mirror-*")]
    assert hidden_tmp == []


def test_resolve_main_repo_priority(tmp_path, monkeypatch):
    """主仓根定位优先级:显式 > env STOCK_MAIN_REPO > symlink > 默认。"""
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    assert M.resolve_main_repo(explicit) == explicit.resolve()

    envdir = tmp_path / "envrepo"
    envdir.mkdir()
    monkeypatch.setenv("STOCK_MAIN_REPO", str(envdir))
    assert M.resolve_main_repo() == envdir.resolve()

    monkeypatch.delenv("STOCK_MAIN_REPO", raising=False)


def test_resolve_main_repo_via_master_symlink(tmp_path, monkeypatch):
    """data/master 是指向主仓的 symlink 时,据此推出主仓根(dailyjob 生产场景)。"""
    main = tmp_path / "stock_analysis"
    (main / "data" / "master").mkdir(parents=True)
    dj = tmp_path / "dailyjob"
    (dj / "data").mkdir(parents=True)
    link = dj / "data" / "master"
    link.symlink_to(main / "data" / "master")

    monkeypatch.delenv("STOCK_MAIN_REPO", raising=False)
    monkeypatch.setattr(M.settings, "DATA_MASTER", link)
    assert M.resolve_main_repo() == main.resolve()


def test_missing_source_daily_is_graceful(tmp_path):
    """源当日目录不存在 → 不崩,analysis_files=0(仅回执/scorecard 若有则拷)。"""
    src, dst = tmp_path / "dailyjob", tmp_path / "main"
    (src / "data").mkdir(parents=True)
    r = M.mirror_daily_to_main("2026-09-11", source_root=src, main_root=dst)
    assert r["analysis_files"] == 0
    assert r["mirrored"] is False
