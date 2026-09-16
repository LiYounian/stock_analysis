"""S1 板块 universe 维护 测试——锁"为什么改"的语义:

1. 全申万一级覆盖(不再只 10 板块):有多少 sw 进 frame,清单就有多少条。
2. 热门规则(预注册阈值):活跃前 N / 涨停≥3 / 均涨幅≥2% 任一 → 热门;分档 热/温/冷。
3. 活跃度排名:成交占比大 + 涨停多 + 换手高 → 排名靠前。
4. 周度 diff 留痕:新增/剔除板块、转热/转冷、排名大变 能被检出。
5. honest degrade:温度计取不到(动量缺失)也能出清单,不抛错。
6. 防未来函数:build 只吃传入 frame,不隐式拉今天实时价。
"""
import pandas as pd
import pytest

from tools.analysis.sector_forecast import sector_universe as SU
from tools.analysis.sector_forecast import universe as U


def _mk_frame():
    """构造 4 个板块的合成截面:电子(热·大成交多涨停)/军工(温)/银行(大成交低换手)/纺服(冷)。"""
    rows = []

    def add(sw, code, pct, amount, turnover, limit_up):
        rows.append({"code": code, "sw": sw, "board": "主板",
                     "close": 10.0, "high": 10.5, "low": 9.8,
                     "amount": amount, "turnover": turnover,
                     "pct_chg": pct, "limit_up": limit_up})

    # 电子:高成交、3 涨停、均涨幅高 → 必热
    for i in range(6):
        add("电子", f"E{i}", 5.0 if i < 3 else 3.0, 5e8, 12.0, i < 3)
    # 军工:中等成交、1 涨停
    for i in range(5):
        add("国防军工", f"J{i}", 2.0 if i == 0 else 1.0, 2e8, 6.0, i == 0)
    # 银行:大市值大成交但低换手、0 涨停、均涨幅低
    for i in range(4):
        add("银行", f"B{i}", 0.3, 4e8, 0.6, False)
    # 纺服:小成交、0 涨停、下跌 → 冷
    for i in range(3):
        add("纺织服饰", f"T{i}", -1.0, 2e7, 1.5, False)
    return pd.DataFrame(rows, columns=U.FRAME_COLS)


@pytest.fixture(autouse=True)
def _no_thermometer(monkeypatch):
    # 断网:温度计置空(honest degrade 分支),不触发数据加载
    monkeypatch.setattr(SU, "_thermometer", lambda date: {})


def test_covers_all_sw_not_just_seed():
    uni = SU.build_sector_universe("2026-09-16", frame=_mk_frame())
    names = {x["板块"] for x in uni["板块清单"]}
    assert names == {"电子", "国防军工", "银行", "纺织服饰"}
    assert uni["n_板块"] == 4


def test_hot_by_limit_and_meanpct():
    uni = SU.build_sector_universe("2026-09-16", frame=_mk_frame())
    m = {x["板块"]: x for x in uni["板块清单"]}
    # 电子:3 涨停 + 均涨幅高 → 热
    assert m["电子"]["热门"] is True and m["电子"]["热度档"] == "热"
    # 纺服:0 涨停、下跌、小成交 → 非热
    assert m["纺织服饰"]["热门"] is False


def test_activity_rank_electronics_top():
    uni = SU.build_sector_universe("2026-09-16", frame=_mk_frame())
    m = {x["板块"]: x for x in uni["板块清单"]}
    # 电子活跃度综合应领先纺服
    assert m["电子"]["活跃度排名"] < m["纺织服饰"]["活跃度排名"]
    assert m["电子"]["涨停数"] == 3


def test_low_turnover_not_inflated():
    """银行成交大但换手极低——换手维度不让它虚高到与电子同档。"""
    uni = SU.build_sector_universe("2026-09-16", frame=_mk_frame())
    m = {x["板块"]: x for x in uni["板块清单"]}
    assert m["银行"]["板块换手中位"] < m["电子"]["板块换手中位"]


def test_week_diff_detects_changes():
    prev = {"date": "2026-09-09", "板块清单": [
        {"板块": "电子", "活跃度排名": 1, "热门": True},
        {"板块": "银行", "活跃度排名": 2, "热门": True},
        {"板块": "煤炭", "活跃度排名": 3, "热门": False},   # 本周剔除
    ]}
    cur = {"date": "2026-09-16", "板块清单": [
        {"板块": "电子", "活跃度排名": 1, "热门": True},
        {"板块": "银行", "活跃度排名": 9, "热门": False},   # 转冷 + 排名大变
        {"板块": "国防军工", "活跃度排名": 2, "热门": True},  # 新增
    ]}
    d = SU._week_diff(cur, prev)
    assert d["基线"] == "2026-09-09"
    assert "国防军工" in d["新增板块"]
    assert "煤炭" in d["剔除板块"]
    assert "银行" in d["转冷"]
    assert any(x["板块"] == "银行" for x in d["活跃度排名大变(≥5名)"])


def test_week_diff_no_baseline():
    d = SU._week_diff({"板块清单": []}, None)
    assert d["基线"] is None


def test_degrade_without_thermometer_ok():
    # autouse fixture 已把温度计置空;能出清单即证明降级不抛错
    uni = SU.build_sector_universe("2026-09-16", frame=_mk_frame())
    assert uni["n_板块"] == 4
    for x in uni["板块清单"]:
        assert x["动量_时序分位"] is None       # 缺失如实为 None,不编造
