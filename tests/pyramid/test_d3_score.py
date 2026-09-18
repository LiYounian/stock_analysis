"""D3 记分引擎语义锁。锁死两套命中口径(A绝对/B跑赢基准)、解析、防未来窗口未满。"""
import os
import pytest

import pandas as pd

from tools.pyramid import d3_score as D

# data/(K线·analysis) 常在主仓(worktree gitignored)；用 env 指过去，缺省=本仓根
ROOT = os.environ.get("D3_TEST_DATA_ROOT") or \
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PICK = os.path.join(ROOT, "docs", "每日分析", "选股", "2026-09-17_金字塔_ClaudeCode.md")


# ── 解析选股 md ──
def test_parse_picks():
    if not os.path.exists(PICK):
        pytest.skip("无 9-17 ClaudeCode 选股文件")
    p = D.parse_picks(PICK)
    assert p["buy"] == ["301551", "000725", "300776"]
    assert p["avoid"] == ["002025", "688795", "603042"]
    assert p["as_of"] == "2026-09-17"
    # 止损价 best-effort 解析（正文有 "止损 **21.16**" 等）
    assert p["stops"].get("301551") == 21.16
    assert p["stops"].get("000725") == 5.31


def test_split_codes():
    assert D._split_codes("301551, 000725 , 300776") == ["301551", "000725", "300776"]
    assert D._split_codes("") == [] and D._split_codes(None) == []
    assert D._split_codes("abc, 12345") == []  # 非6位剔除


# ── 两套口径判定（纯函数·核心语义锁）──
def test_judge_买入():
    assert D.judge("买入", 2.0, 1.0) == {"命中A": True, "超额": 1.0, "命中B": True}
    assert D.judge("买入", 2.0, 3.0) == {"命中A": True, "超额": -1.0, "命中B": False}  # 涨但跑输
    assert D.judge("买入", -1.0, 0.0) == {"命中A": False, "超额": -1.0, "命中B": False}


def test_judge_规避():
    # 跌了=避对(A命中)；超额<0=避对(B命中)
    assert D.judge("规避", -2.0, 1.0) == {"命中A": True, "超额": -3.0, "命中B": True}
    assert D.judge("规避", 2.0, 1.0) == {"命中A": False, "超额": 1.0, "命中B": False}  # 涨了=没避对


def test_judge_基准缺():
    r = D.judge("买入", 2.0, None)
    assert r["命中A"] is True and r["超额"] is None and r["命中B"] is None


# ── 防未来：窗口未走完 ──
def test_forward_窗口未满():
    if not os.path.exists(os.path.join(ROOT, "data", "master", "kline", "000725.parquet")):
        pytest.skip("无 K线")
    # 选股日=数据末日 → D+1 不存在 → insufficient
    fr = D.forward_return("000725", "2026-09-17", 1, "2026-09-17", root=ROOT)
    assert fr.get("insufficient") is True


def test_forward_收益算对():
    kp = os.path.join(ROOT, "data", "master", "kline", "000725.parquet")
    if not os.path.exists(kp):
        pytest.skip("无 K线")
    # 独立从 parquet 手算 9-16→9-17（不同代码路径，非自证循环）
    df = pd.read_parquet(kp)
    df = df[df["date"].astype(str).str[:10] <= "2026-09-17"].reset_index(drop=True)
    c0 = float(df.loc[df["date"].astype(str).str[:10] == "2026-09-16", "close"].iloc[0])
    c1 = float(df.loc[df["date"].astype(str).str[:10] == "2026-09-17", "close"].iloc[0])
    exp = round((c1 / c0 - 1) * 100, 2)
    fr = D.forward_return("000725", "2026-09-16", 1, "2026-09-17", root=ROOT)
    assert fr["ret_pct"] == exp and fr["dK_date"] == "2026-09-17"


# ── 基准：覆盖/不覆盖 ──
def test_bench_覆盖与缺():
    bdf = D._load_bench_df(ROOT)
    if bdf is None:
        pytest.skip("无沪深300基准")
    maxd = str(bdf["date"].max())[:10]
    # 覆盖内一对 → 浮点
    b = D.bench_return(str(bdf["date"].iloc[-2])[:10], 1, maxd, root=ROOT)
    assert isinstance(b, float)
    # 记分执行日超基准末日 → 该窗口基准缺(None)
    assert D.bench_return(maxd, 1, maxd, root=ROOT) is None


# ── 集成：9-17 记分卡（D+窗口全未满，仍不炸）──
def test_scorecard_窗口未满不炸():
    if not os.path.exists(PICK):
        pytest.skip("无选股文件")
    sc = D.build_scorecard("2026-09-17", windows=(1,), score_asof="2026-09-17", root=ROOT)
    assert sc["sources"], "应解析到三方文件"
    for p in sc["sources"]:
        assert p["summary"][1]["n"] == 0  # D+1=9-18 未走完
    txt = D.render_scorecard(sc)
    assert "D3 记分卡" in txt
