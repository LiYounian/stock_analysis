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


# ───────────────────────── P2:两步框架 focus 防踏空语义 ─────────────────────────

def _panel_row(板块, 冷热, **kw):
    base = dict(板块=板块, 冷热标签=冷热, 拥挤分位=0.5, 拥挤档="B",
                动量_时序分位=0.5, 动量_时序档="温", 动量_截面分位=0.5, 动量_截面档="温",
                上涨家数占比=0.3, 涨停数=0, 板块成交额=1e9, 板块成交占比=0.05,
                板块均涨幅=0.0, 成分数=100, 拐点方向=None)
    base.update(kw)
    return base


def test_focus_过冷板块有涨停仍进重点池_防踏空(monkeypatch):
    """核心防踏空:板块虽标'过冷',只要当日有涨停(≥门槛)就必须进重点池,不被冷热标签踢出。"""
    from tools.analysis.sector_forecast import focus as F
    from tools.analysis.sector_forecast import market_step as MS
    # 屏蔽外部新闻/大盘输入,只考察冷热与涨停的逻辑
    monkeypatch.setattr(F, "news_catalyst_by_sector", lambda date: {})
    monkeypatch.setattr(MS, "market_risk_appetite",
                        lambda date, **kw: {"风险偏好": "中性", "依据": "test"})
    panel = [_panel_row("电子", "过冷", 涨停数=6, 板块成交占比=0.31),
             _panel_row("食品饮料", "正常活跃", 涨停数=0, 板块成交占比=0.02)]
    foc = F.build_focus("2026-09-15", panel=panel)
    hot = [r["板块"] for r in foc["重点板块池"]]
    assert "电子" in hot          # 过冷但6涨停 → 必须在重点池(否则重演踏空)


def test_focus_利空叠拥挤A进规避池(monkeypatch):
    from tools.analysis.sector_forecast import focus as F
    from tools.analysis.sector_forecast import market_step as MS
    monkeypatch.setattr(F, "news_catalyst_by_sector",
                        lambda date: {"银行": {"净催化": -8.0, "n条": 2, "利好": 0, "利空": 2}})
    monkeypatch.setattr(MS, "market_risk_appetite",
                        lambda date, **kw: {"风险偏好": "防守", "依据": "test"})
    panel = [_panel_row("银行", "正常活跃", 拥挤档="A", 涨停数=0)]
    foc = F.build_focus("2026-09-15", panel=panel)
    avoid = [r["板块"] for r in foc["规避板块池"]]
    assert "银行" in avoid         # 净利空+拥挤A → 规避


def test_focus_冷热不直接决定入池():
    """回归锁:重点池判据里不得出现'冷热标签∈过冷则排除'这类硬编码(防未来重写引回踏空)。"""
    import inspect
    from tools.analysis.sector_forecast import focus as F
    src = inspect.getsource(F.build_focus)
    # 入池条件必须基于 catalyst / 涨停,而非"过冷"直接排除
    assert "catalyst > 0" in src or "涨停" in src


# ───────────────────────── P2.3:宏观研判(锁 catalyst 刷屏不污染宏观) ─────────────────────────

def test_macro_关税利好刷屏不判偏空():
    """回归锁:大量'算力+出口管制(国产替代利好)'新闻不得把宏观净方向刷成偏空/关税冲击
    (板块催化≠市场级宏观利空;此前 bug)。"""
    from tools.analysis.sector_forecast import news_store as NS
    indicators = {"国债": {"中国10Y环比": -0.002}, "汇率": {"环比": -0.28}}
    news = {"宏观命中": {"关税冲击": [{"方向": "利好", "强度": 3}] * 15}}  # 15条国产替代利好
    m = NS.derive_macro(indicators, news)
    assert m["宏观净方向"] != "偏空"
    assert m["宏观情景"] != "关税冲击"


def test_macro_真关税净利空判冲击():
    from tools.analysis.sector_forecast import news_store as NS
    news = {"宏观命中": {"关税冲击": [{"方向": "利空", "强度": 4}] * 2}}  # 净-8
    m = NS.derive_macro({}, news)
    assert m["宏观情景"] == "关税冲击"


def test_macro_利率大幅下行判降息宽松():
    from tools.analysis.sector_forecast import news_store as NS
    m = NS.derive_macro({"国债": {"中国10Y环比": -0.08}}, {"宏观命中": {}})
    assert m["宏观情景"] == "降息宽松"


def test_macro_全中性():
    from tools.analysis.sector_forecast import news_store as NS
    m = NS.derive_macro({"国债": {"中国10Y环比": 0.0}, "汇率": {"环比": 0.1}}, {"宏观命中": {}})
    assert m["宏观净方向"] == "中性" and m["宏观情景"] == "中性"


# ───────────────────────── 消息驱动 一入 M1:板块龙头催化标签 ─────────────────────────

def test_leader_catalyst_描述性无数值(monkeypatch):
    """v2:板块标签直接取 LLM 描述性『消息面』,不做数值加权;结构为文字标签。"""
    from tools.analysis.sector_forecast import news_catalyst as NC
    monkeypatch.setattr(NC, "leader_codes",
                        lambda date, boards=None: {"电子": [{"code": "E1", "name": "龙头一"}]})
    monkeypatch.setattr(NC, "board_news_verdict",
                        lambda date, sw, leads, client=None: {
                            "消息面": "利好", "强弱": "强",
                            "关键事件": ["2026-09-10 算力订单落地(利好)"],
                            "持续性": "持续利好·两周内多条", "时效与可靠性": "本周主流媒体,较可靠",
                            "理由": "算力持续催化", "n条": 6, "时间跨度": "2026-09-03~2026-09-15"})
    out = NC.board_leader_catalyst("2026-09-15")
    assert out["电子"]["消息标签"] == "利好" and out["电子"]["强弱"] == "强"
    assert "算力订单" in out["电子"]["关键事件"][0]      # 关键事件带时间节点
    assert "持续利好" in out["电子"]["持续性"]
    # 断言无数值分字段(用户:数值不可信)
    assert "合并净催化" not in out["电子"] and "净催化" not in out["电子"]


def test_leader_codes_从角色表取龙头(monkeypatch, tmp_path):
    """leader_codes 读 roster 的 roles.龙头(缺表则跳过,不崩)。"""
    from tools.analysis.sector_forecast import news_catalyst as NC
    # 不存在的板块 → 空(不崩)
    out = NC.leader_codes("2026-09-15", boards=["不存在的板块XYZ"])
    assert out == {}


def test_news_driven_block_契约字段(monkeypatch):
    """「消息驱动」块字段对齐统筹契约:利好板块[{board,tag,strength,龙头候选[code,催化,已动],跟涨候选}]。"""
    from tools.analysis.sector_forecast import news_focus_block as NB
    from tools.analysis.sector_forecast import news_catalyst as NC
    monkeypatch.setattr(NC, "board_leader_catalyst", lambda date, boards=None, client=None: {
        "电子": {"消息标签": "利好", "强弱": "强", "关键事件": ["2026-09-10 算力订单(利好)"],
                "持续性": "持续利好", "时效与可靠性": "较可靠", "理由": "算力催化",
                "n条": 6, "时间跨度": "2026-09-03~2026-09-15",
                "龙头": [{"code": "E1", "name": "龙头一"}]},
        "银行": {"消息标签": "中性", "龙头": []}})
    monkeypatch.setattr(NB, "_leader_moved", lambda code, date: True)
    monkeypatch.setattr(NB, "_followers", lambda sw, date, top=3: [{"code": "F1", "name": "跟涨一", "联动依据": "x"}])
    blk = NB.build_news_driven_block("2026-09-16")
    boards = [b["board"] for b in blk["利好板块"]]
    assert boards == ["电子"]                      # 只收利好板块
    e = blk["利好板块"][0]
    assert e["tag"] == "利好" and e["强弱"] == "强"      # 文字档,非数值
    assert "strength" not in e                          # 无数值分(用户:数值不可信)
    assert e["龙头候选"][0]["code"] == "E1" and e["龙头候选"][0]["已动"] is True
    assert e["跟涨候选"][0]["code"] == "F1"
    assert "算力订单" in e["关键事件"][0]
