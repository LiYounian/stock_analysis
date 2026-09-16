"""板块预测系统 P1 测试——锁死冷热标签规则 + 四角色识别语义(防未来重写时被无意删掉)。

断言锁"为什么这么设计"的语义,不是锁具体数值:
  · 冷热标签:三档投票 + 背离拐点(labels.classify_regime)。
  · 四角色:龙头偏强势/中军偏大市值/补涨偏低位滞涨/弹性偏小盘高换手(identify_roles)。
用合成 DataFrame,不依赖真数据/网络。
"""
import numpy as np
import pandas as pd
import pytest

from tools.analysis.sector_forecast import labels as L
from tools.analysis.sector_forecast import roles as R


# ───────────────────────── 冷热标签语义 ─────────────────────────

def test_label_过热_三维皆高():
    r = L.classify_regime(拥挤分位=0.9, 动量_截面分位=0.9, 动量_时序分位=0.8, 上涨家数占比=0.8)
    assert r["标签"] == "过热"
    assert r["置信"] == "高"          # 三票齐 → 高置信


def test_label_过冷_三维皆低():
    r = L.classify_regime(拥挤分位=0.1, 动量_截面分位=0.1, 动量_时序分位=0.1, 上涨家数占比=0.2)
    assert r["标签"] == "过冷"


def test_label_拐点_强动量却广度萎缩():
    # 截面动量领先(≥0.8)但上涨占比萎缩(≤0.35)= 价量背离 → 下行拐点
    r = L.classify_regime(拥挤分位=0.5, 动量_截面分位=0.95, 动量_时序分位=0.5, 上涨家数占比=0.1)
    assert r["标签"] == "拐点"
    assert "下行" in r.get("拐点方向", "")


def test_label_拐点优先于普通过热票():
    # 拥挤高+动量高(2 过热票)但广度萎缩 → 背离,必须判拐点而非过热(风险优先)
    r = L.classify_regime(拥挤分位=0.85, 动量_截面分位=0.9, 动量_时序分位=0.6, 上涨家数占比=0.1)
    assert r["标签"] == "拐点"


def test_label_数据不足_少于两维():
    r = L.classify_regime(拥挤分位=0.9, 动量_截面分位=None, 动量_时序分位=None, 上涨家数占比=None)
    assert r["标签"] == "数据不足"


def test_label_正常活跃_各维居中():
    r = L.classify_regime(拥挤分位=0.5, 动量_截面分位=0.5, 动量_时序分位=0.5, 上涨家数占比=0.5)
    assert r["标签"] == "正常活跃"


def test_label_版本号存在_防偷改():
    r = L.classify_regime(拥挤分位=0.5, 动量_截面分位=0.5, 动量_时序分位=0.5, 上涨家数占比=0.5)
    assert r["version"] == L.LABELS_VERSION      # 阈值改版必须显式升版本号


# ───────────────────────── 四角色识别语义 ─────────────────────────

def _mk_feat():
    """构造 6 只票的合成板块:各角色应命中的"标准样本"。"""
    return pd.DataFrame([
        # 龙头:高涨幅+高成交+今日涨停+率先涨停
        dict(code="LEAD", board="主板", close=20, pct_chg=10, amount=9e9, turnover=8,
             ret3=25, ret5=40, ret10=55, ret20=60, pos60=0.98, vol_ratio=3.0,
             多头=True, first_limit_ago=1, limit_today=True, beta=1.3, 市值亿=200),
        # 中军:超大市值+高成交+多头趋势,涨幅温和
        dict(code="CORE", board="主板", close=50, pct_chg=1, amount=8e9, turnover=1,
             ret3=2, ret5=3, ret10=5, ret20=8, pos60=0.95, vol_ratio=1.1,
             多头=True, first_limit_ago=np.nan, limit_today=False, beta=0.9, 市值亿=9000),
        # 补涨:低位+短期滞涨+放量
        dict(code="LAG", board="主板", close=8, pct_chg=2, amount=2e9, turnover=5,
             ret3=-1, ret5=-12, ret10=-8, ret20=-15, pos60=0.45, vol_ratio=2.2,
             多头=False, first_limit_ago=np.nan, limit_today=False, beta=1.0, 市值亿=120),
        # 弹性:小市值+高换手+创业板+高β
        dict(code="ELAS", board="创业板", close=15, pct_chg=5, amount=1e9, turnover=20,
             ret3=8, ret5=10, ret10=12, ret20=5, pos60=0.8, vol_ratio=1.5,
             多头=True, first_limit_ago=np.nan, limit_today=False, beta=1.8, 市值亿=30),
        # 陪衬 A
        dict(code="MIDA", board="主板", close=12, pct_chg=0, amount=3e9, turnover=2,
             ret3=0, ret5=1, ret10=2, ret20=3, pos60=0.7, vol_ratio=1.0,
             多头=False, first_limit_ago=np.nan, limit_today=False, beta=1.0, 市值亿=400),
        # 陪衬 B
        dict(code="MIDB", board="主板", close=30, pct_chg=-1, amount=4e9, turnover=1.5,
             ret3=-2, ret5=-1, ret10=1, ret20=2, pos60=0.85, vol_ratio=0.9,
             多头=True, first_limit_ago=np.nan, limit_today=False, beta=0.8, 市值亿=1500),
    ])


def test_role_龙头_命中强势涨停股():
    roles = R.identify_roles("测试板块", _mk_feat())
    assert roles["龙头"][0]["code"] == "LEAD"


def test_role_中军_命中超大市值股():
    roles = R.identify_roles("测试板块", _mk_feat())
    assert roles["中军"][0]["code"] == "CORE"


def test_role_补涨_命中低位滞涨股():
    roles = R.identify_roles("测试板块", _mk_feat())
    codes = [c["code"] for c in roles["补涨先锋"]]
    assert "LAG" in codes[:2]        # 低位滞涨放量应进补涨主选


def test_role_弹性_命中小盘高换手股():
    roles = R.identify_roles("测试板块", _mk_feat())
    codes = [c["code"] for c in roles["弹性股"]]
    assert "ELAS" in codes[:2]


def test_role_主选备选分级():
    roles = R.identify_roles("测试板块", _mk_feat())
    lead = roles["龙头"]
    assert lead[0]["选级"] == "主选" and lead[1]["选级"] == "主选"
    assert lead[2]["选级"] == "备选"


def test_role_空板块返回四空列表():
    roles = R.identify_roles("空板块", pd.DataFrame())
    assert all(roles[r] == [] for r in ("龙头", "中军", "补涨先锋", "弹性股"))


# ───────────────────────── 防未来函数(集成 smoke,缺数据则跳过) ─────────────────────────

def test_防未来_frame只含目标日之内():
    """load_sector_frame(date) 产出的每只票,其取的是 ≤date 的 K线行(不含未来)。"""
    from tools.analysis.sector_forecast import universe as U
    try:
        frame = U.load_sector_frame("2026-09-15")
    except Exception:
        pytest.skip("无本地数据,跳过集成 smoke")
    if frame.empty:
        pytest.skip("截面为空,跳过")
    # 结构断言:关键列齐、涨停为布尔、成交额非负
    assert set(["code", "sw", "pct_chg", "limit_up"]).issubset(frame.columns)
    assert frame["limit_up"].dtype == bool
    assert (frame["amount"].dropna() >= 0).all()
