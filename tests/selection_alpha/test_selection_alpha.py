"""锁定「选股真实次日 α 诊断」口径语义的测试。

覆盖:成交/收益公式、入场档挂价、涨停不可买、成本、防未来、α 基准=全A等权、
picks_loader 的 dump-mode/top_n 截断。断言锁住"为什么这么算",防未来重写误删规则。

⚠️ 测试环境研究模拟,非投资建议。
"""
import json
import os

import numpy as np
import pandas as pd
import pytest

from tools.research.selection_alpha import nextday_kernel as K
from tools.research.selection_alpha import nextday_exec as nx
from tools.research.selection_alpha import picks_loader as pl


# ─────────────────────────── 纯函数:口径公式 ───────────────────────────
def test_fill_and_return_marketable():
    # P=10。open<=P → 成交于 open;open>P 但 low<=P → 成交于 P;low>P → 未触发
    P = np.array([10.0, 10.0, 10.0])
    o = np.array([9.5, 10.5, 10.5])
    h = np.array([11.0, 11.0, 11.0])
    lo = np.array([9.0, 9.8, 10.2])
    c = np.array([10.4, 10.4, 10.9])
    fill, ret, filled = K.fill_and_return(P, o, h, lo, c, "marketable")
    assert filled.tolist() == [True, True, False]
    assert fill[0] == pytest.approx(9.5)     # 成交于 open(更优)
    assert fill[1] == pytest.approx(10.0)    # open>P,回踩到 P 成交
    assert np.isnan(fill[2])                  # low>P 未触发
    assert ret[0] == pytest.approx(10.4 / 9.5 - 1)
    assert ret[1] == pytest.approx(10.4 / 10.0 - 1)


def test_fill_and_return_doc_model():
    # doc: low<=P<=high 取 P;否则未触发(不取 open 优势)
    P = np.array([10.0, 10.0])
    o = np.array([9.5, 10.5])
    h = np.array([11.0, 9.9])
    lo = np.array([9.0, 9.0])
    c = np.array([10.4, 9.5])
    fill, ret, filled = K.fill_and_return(P, o, h, lo, c, "doc")
    assert filled.tolist() == [True, False]   # 第二个 P>high 未触发
    assert fill[0] == pytest.approx(10.0)      # doc 恒取 P(即使 open 更优)


def test_entry_prices():
    feat = {"c": np.array([10.0, 20.0, 30.0]), "o": np.array([9.0, 19.0, 29.0])}
    t = np.array([0, 1])
    Ps = K.entry_prices(feat, t)
    assert Ps["open"].tolist() == [19.0, 29.0]           # D+1 open
    assert Ps["limit_pc_0.0"].tolist() == [10.0, 20.0]   # 昨收
    assert Ps["limit_pc_0.01"] == pytest.approx([9.9, 19.8])  # 昨收*0.99


def test_limit_up_unbuyable_boards():
    # 主板 10%:close=10 → 阈值 10*(1+0.10-0.005)=10.95;open>=10.95 判不可买
    assert K.board_limit("600000") == 0.10
    assert K.board_limit("300001") == 0.20
    assert K.board_limit("688001") == 0.20
    close = np.array([10.0, 10.0])
    open_n = np.array([10.96, 10.90])
    unb = K.limit_up_unbuyable("600000", close, open_n)
    assert unb.tolist() == [True, False]
    # 创业板 20%:阈值 10*(1.195)=11.95
    unb_gem = K.limit_up_unbuyable("300001", np.array([10.0, 10.0]), np.array([11.96, 11.90]))
    assert unb_gem.tolist() == [True, False]


def test_net_return_cost():
    r = np.array([0.02, -0.01])
    net = K.net_return(r, 10.0)   # 10bps round-trip
    assert net[0] == pytest.approx((1.02) * (1 - 0.001) - 1)
    assert net[1] == pytest.approx((0.99) * (1 - 0.001) - 1)


# ─────────────────────────── 合成宇宙:evaluate 语义 ───────────────────────────
def _write_kline(root, code, rows):
    d = os.path.join(root, "data", "master", "kline")
    os.makedirs(d, exist_ok=True)
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"])
    df["volume"] = 1e6; df["amount"] = 1e7; df["turnover"] = 1.0
    df["pct_chg"] = df["close"].pct_change().fillna(0) * 100
    df.to_parquet(os.path.join(d, f"{code}.parquet"), index=False)


def _mk_universe(root):
    # 60 根历史让 MA/β 有定义,再接 3 根用于评估
    base = [(f"2026-06-{i+1:02d}", 10, 10.2, 9.8, 10.0) for i in range(28)]
    base += [(f"2026-07-{i+1:02d}", 10, 10.2, 9.8, 10.0) for i in range(31)]
    tail_A = [("2026-08-03", 10.0, 10.5, 9.5, 10.0),   # D=决策日(idx 59)
              ("2026-08-04", 9.9, 11.0, 9.8, 10.5),    # D+1 执行日
              ("2026-08-05", 10.5, 10.6, 10.4, 10.5)]  # D+2(篡改用)
    tail_B = [("2026-08-03", 10.0, 10.5, 9.5, 10.0),
              ("2026-08-04", 10.0, 10.2, 9.6, 9.8),
              ("2026-08-05", 9.8, 9.9, 9.7, 9.8)]
    _write_kline(root, "600001", base + tail_A)
    _write_kline(root, "600002", base + tail_B)
    return root


def test_evaluate_exec_date_and_alpha(tmp_path):
    root = _mk_universe(str(tmp_path))
    uni = nx.Universe(root, min_date="2026-06-01")
    picks = pd.DataFrame([("2026-08-03", "s", "600001")], columns=["date", "strategy", "code"])
    ev = uni.evaluate(picks, cost_bps=0.0, rules=("limit_pc_0.01", "open"))
    row = ev[ev.rule == "open"].iloc[0]
    assert row.exec_date == "2026-08-04"          # D+1
    # open 档:成交于 D+1 open=9.9,收盘 10.5 → ret=10.5/9.9-1
    assert row.ret == pytest.approx(10.5 / 9.9 - 1)
    # α = ret − 全A等权 oc 均值(执行日 D+1):A oc=10.5/9.9-1, B oc=9.8/10.0-1
    oc_A = 10.5 / 9.9 - 1
    oc_B = 9.8 / 10.0 - 1
    mkt = (oc_A + oc_B) / 2
    assert row.mkt == pytest.approx(mkt)
    assert row.alpha == pytest.approx(oc_A - mkt)


def test_no_future_leak(tmp_path):
    """篡改 D+2 及以后的数据,不改变 D(决策)→D+1(执行) 的事件结果。"""
    root = str(tmp_path)
    _mk_universe(root)
    picks = pd.DataFrame([("2026-08-03", "s", "600001")], columns=["date", "strategy", "code"])
    ev1 = nx.Universe(root, min_date="2026-06-01").evaluate(picks, cost_bps=0.0, rules=("open",))
    # 篡改 600001 的 D+2(2026-08-05)为极端值
    d = os.path.join(root, "data", "master", "kline")
    df = pd.read_parquet(os.path.join(d, "600001.parquet"))
    df.loc[df["date"] == "2026-08-05", ["open", "high", "low", "close"]] = [99, 99, 99, 99]
    df.to_parquet(os.path.join(d, "600001.parquet"), index=False)
    ev2 = nx.Universe(root, min_date="2026-06-01").evaluate(picks, cost_bps=0.0, rules=("open",))
    assert ev1[ev1.rule == "open"].iloc[0].ret == pytest.approx(ev2[ev2.rule == "open"].iloc[0].ret)


def test_pick_on_last_bar_dropped(tmp_path):
    """决策日为该票最后一根(无 D+1)→ 事件被丢弃(不可执行)。"""
    root = str(tmp_path)
    _mk_universe(root)
    picks = pd.DataFrame([("2026-08-05", "s", "600001")], columns=["date", "strategy", "code"])
    ev = nx.Universe(root, min_date="2026-06-01").evaluate(picks, rules=("open",))
    assert len(ev) == 0


# ─────────────────────────── picks_loader ───────────────────────────
def test_loader_dump_mode_and_topn(tmp_path):
    base = os.path.join(str(tmp_path), "data", "analysis", "2026-08-12")
    os.makedirs(base)
    # 合议 dump 模式:top_n=1997、top 给 2000 行 → 应截断到 20(排名 top20)
    dump = {"top_n": 1997, "top": [{"code": f"{600000+i:06d}"} for i in range(2000)]}
    json.dump(dump, open(os.path.join(base, "策略0合议.json"), "w"))
    # 正常入选清单 + 声明入选数
    normal = {"入选数": 3, "入选清单": [{"code": "000001"}, {"code": "000002"},
                                    {"code": "000003"}, {"code": "000004"}]}
    json.dump(normal, open(os.path.join(base, "最强选股.json"), "w"))
    df = pl.load_picks(str(tmp_path), strategies=["策略0合议", "最强选股"])
    hy = df[df.strategy == "策略0合议"]
    assert len(hy) == 20                    # dump-mode 复原 top20
    zq = df[df.strategy == "最强选股"]
    assert len(zq) == 3                     # 入选数=3 截断(尽管清单有4)
    assert list(zq.code) == ["000001", "000002", "000003"]


def test_loader_md_anchor(tmp_path):
    d = os.path.join(str(tmp_path), "docs", "每日分析", "选股")
    os.makedirs(d)
    open(os.path.join(d, "2026-09-11.md"), "w").write(
        "<!-- PICKS: 601872,002913,300124 -->\n# 正文\n")
    # 变体文件不应被采纳
    open(os.path.join(d, "日内_2026-09-11.md"), "w").write("<!-- PICKS: 000001 -->\n")
    df = pl.load_md_picks(str(tmp_path))
    assert set(df.code) == {"601872", "002913", "300124"}
    assert set(df.date) == {"2026-09-11"}
