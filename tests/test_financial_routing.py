"""财报路由复活:锁"行业专家路由 + 金融业特判 + 板块轮动未被激活"语义。

背景:合议行业财报专家靠 analyzer._industry_key 把个股路由到申万一级行业专家,
_is_financial 决定银行/非银是否走金融业红旗特判;二者回退 board.board_of(证监会行业名)
→ industry_map.to_sw 对齐申万一级。board_membership 数据落地后本组用例锁住:
  ① 非半导体样本经 board_of 能拿到行业(此前 board_of 恒 None 全弃权);
  ② 银行/非银金融业特判能触发(此前从未触发);
  ③ 半导体池成分优先于 board_of(证监会误分 fabless→软件/机械 时仍落电子);
  ④ 板块轮动专家不因 board_membership 落地被误激活(无 RRG 指数数据仍确定性弃权);
     且合议默认专家组/权重里板块轮动启用态与主仓一致(未因本改动生效)。

纯逻辑用例:board_of / _in_semi_universe / rrg.industry_row 全部 monkeypatch,不触网不读盘。
"""
import pytest

from tools.analysis.financial import analyzer as az


@pytest.fixture(autouse=True)
def _clear_semi_cache():
    """每例前清半导体池缓存(避免跨例污染 monkeypatch)。"""
    az._SEMI_UNIVERSE_CACHE = None
    yield
    az._SEMI_UNIVERSE_CACHE = None


# ———————————— ① 非半导体样本经 board_of 拿到行业 ————————————
@pytest.mark.parametrize("csrc,expect", [
    ("C15酒、饮料和精制茶制造业", "食品饮料"),   # 白酒
    ("C27医药制造业", "医药生物"),
    ("C38电气机械和器材制造业", "电力设备"),
    ("K70房地产业", "房地产"),
    ("C36汽车制造业", "汽车"),
])
def test_non_semi_routes_via_board_of(monkeypatch, csrc, expect):
    from tools.collectors import board
    monkeypatch.setattr(board, "board_of", lambda c: csrc)
    monkeypatch.setattr(az, "_in_semi_universe", lambda c: False)
    assert az._industry_key("600000") == expect


def test_board_of_none_non_semi_abstains(monkeypatch):
    """board_of 缺(如退市/僵尸股)且非半导体 → None(退回通用兜底,不误路由)。"""
    from tools.collectors import board
    monkeypatch.setattr(board, "board_of", lambda c: None)
    monkeypatch.setattr(az, "_in_semi_universe", lambda c: False)
    assert az._industry_key("999999") is None


# ———————————— ② 金融业特判触发(银行/非银)————————————
@pytest.mark.parametrize("csrc,is_fin", [
    ("J66货币金融服务", True),    # 银行
    ("J67资本市场服务", True),    # 券商 → 非银金融
    ("J68保险业", True),          # 保险 → 非银金融
    ("C15酒、饮料和精制茶制造业", False),   # 食品饮料:非金融
    ("C27医药制造业", False),
])
def test_financial_special_case_via_board_of(monkeypatch, csrc, is_fin):
    from tools.collectors import board
    monkeypatch.setattr(board, "board_of", lambda c: csrc)
    assert az._is_financial("600000") is is_fin


def test_financial_none_board_of_not_triggered(monkeypatch):
    """board_of 缺 + 无传入 industry → 不特判(不误伤非金融)。"""
    from tools.collectors import board
    monkeypatch.setattr(board, "board_of", lambda c: None)
    assert az._is_financial("999999") is False


# ———————————— ③ 半导体池优先于 board_of(核心修复)————————————
@pytest.mark.parametrize("csrc_misclass", [
    "I65软件和信息技术服务业",   # fabless 芯片设计被证监会误分软件 → 计算机
    "C35专用设备制造业",         # 半导体设备被误分 → 机械设备
    "I64互联网和相关服务",
])
def test_semi_universe_beats_board_of(monkeypatch, csrc_misclass):
    """半导体池成分即便 board_of 落到别的行业,仍路由到电子(申万二级 801081 权威)。"""
    from tools.collectors import board
    monkeypatch.setattr(board, "board_of", lambda c: csrc_misclass)
    monkeypatch.setattr(az, "_in_semi_universe", lambda c: True)
    assert az._industry_key("688111") == "电子"


def test_semi_universe_not_beats_explicit_industry(monkeypatch):
    """上游显式 industry 仍最优先(不被半导体池覆盖细分口径)。"""
    monkeypatch.setattr(az, "_in_semi_universe", lambda c: True)
    # 传入 industry='白酒' → 食品饮料,即使该 code 命中半导体池也按显式口径
    assert az._industry_key("600519", industry="白酒") == "食品饮料"


def test_semi_fallback_when_board_of_none(monkeypatch):
    """board_of 缺 + 半导体池命中 → 电子(主仓证据:board_membership 缺时半导体仍走 semi 兜底)。"""
    from tools.collectors import board
    monkeypatch.setattr(board, "board_of", lambda c: None)
    monkeypatch.setattr(az, "_in_semi_universe", lambda c: True)
    assert az._industry_key("688111") == "电子"


# ———————————— ④ 板块轮动未被 board_membership 落地误激活 ————————————
def test_board_membership_does_not_activate_sector_rotation(monkeypatch):
    """board_of 有值(如银行)但无 RRG 指数数据 → 板块轮动专家仍确定性弃权(置信度0)。

    锁住:复活财报路由(落 board_membership)本身不会让被证伪的板块轮动专家发声——
    它另需 board_kline/沪深300 指数数据(本改动不采集)。这与主仓证伪-安全态一致。
    """
    from tools.analysis import experts as ex
    from tools.analysis import rrg
    from tools.collectors import board
    monkeypatch.setattr(board, "board_of", lambda c: "J66货币金融服务")
    monkeypatch.setattr(rrg, "industry_row", lambda name: None)   # 无 RRG 指数数据
    v = ex.expert_板块轮动({"meta": {"code": "600000"}})
    assert v.置信度 == 0.0 and v.数据充分度 == "缺失"    # 弃权:净合议贡献=0


def test_sector_rotation_gating_unchanged():
    """合议默认专家组/权重里板块轮动启用态与主仓一致(未因财报路由改动而生效/移除)。"""
    from tools.config import strategy
    c = strategy.THRESHOLDS["合议"]
    assert "板块轮动" in c["默认专家组"]              # 仍在组(结构未动)
    assert c["默认权重"].get("板块轮动") == 1.0        # 权重未动
    # 弃权时净贡献=强度×置信度×权重=0(置信度0),不因权重非0而污染合议——由 ④ 上例锁定。
