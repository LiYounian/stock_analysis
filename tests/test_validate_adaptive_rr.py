"""自适应RR A/B 验证脚本冒烟测试(hermetic,不触真数据/不起子进程)。

锁住空 panel bug 的**修复语义**(空样本显式返回 2、不再抛 KeyError)与 RR 分布提取口径。
"""
import numpy as np
import pandas as pd

from tools.backtest import validate_adaptive_rr as v


def test_rr_from_panel_extracts_ratio_and_flags_constant():
    """RR = br_gain/br_loss 逐观测提取;恒定 1.33 占比可检出个股化。"""
    pa = pd.DataFrame({
        "N": [5, 5, 5, 5, 1],
        "br_gain": [1.33, 2.00, 1.00, np.nan, 5.0],   # 最后一行 N=1 应被过滤
        "br_loss": [1.00, 1.00, 1.00, 1.00, 1.00],
    })
    rr = v._rr_from_panel(pa, N=5)
    assert len(rr) == 3                      # 只取 N=5 且 gain/loss 非空
    assert abs(rr[0] - 1.33) < 1e-9          # 1.33/1.00
    assert abs(rr[1] - 2.00) < 1e-9
    # 恒定 1.33 判定:仅 1/3 命中 1.33
    const = float(np.mean(np.abs(rr - 1.33) < 0.02)) * 100
    assert 30.0 < const < 40.0


def test_run_ab_empty_universe_returns_2_not_keyerror(monkeypatch):
    """采样为空(worktree 无数据的原始 bug 场景)→ 显式返回 2,绝不抛 KeyError 'N'。"""
    monkeypatch.setattr(v, "_resolve_data_root", lambda _c: None)
    import tools.backtest.backtest_predict as bp
    monkeypatch.setattr(bp, "_sample_universe", lambda n, seed: [])
    rc = v.run_ab(10, 5, 42, horizons=(1, 5, 10), jobs=1)
    assert rc == 2


def test_run_ab_empty_panel_returns_2(monkeypatch):
    """采样非空但每票历史不足 → panel 空 → 显式返回 2(无 'N' 列不炸)。"""
    monkeypatch.setattr(v, "_resolve_data_root", lambda _c: None)
    import tools.backtest.backtest_predict as bp
    monkeypatch.setattr(bp, "_sample_universe", lambda n, seed: ["000001", "000002"])
    monkeypatch.setattr(v, "build_panel_parallel",
                        lambda *a, **k: pd.DataFrame())   # 模拟空 panel
    rc = v.run_ab(2, 5, 42, horizons=(1, 5, 10), jobs=1)
    assert rc == 2


def test_resolve_data_root_prefers_valid_cli(tmp_path):
    """--data-root 指向含 master/kline 的目录 → 采用;否则回退探测。"""
    (tmp_path / "master" / "kline").mkdir(parents=True)
    got = v._resolve_data_root(str(tmp_path))
    assert got == tmp_path.resolve()
