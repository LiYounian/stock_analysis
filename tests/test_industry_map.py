"""F8 后续单测:行业名口径映射(任意口径 → 申万一级)。

锁语义:
  - 申万一级直通;证监会门类前缀码 → 申万;常见细分别名 → 申万;子串兜底;无匹配→None(不硬凑);
  - 映射表自洽:证监会 83 门类全覆盖、所有目标值都是合法申万一级名;
  - 申万独立而证监会不单列的 家用电器/美容护理 至少能经别名到达(否则永远弃权);
  - rrg.industry_row 取数前先对齐(用申万名加载 board_kline)。
纯数据 + 纯函数,不触网、不读盘。
"""
import pytest

from tools.analysis import industry_map as im
from tools.analysis.industry_map import SW_INDUSTRIES, CSRC_CODE_TO_SW, ALIAS_TO_SW, to_sw


# ———————————— 申万一级直通 ————————————
def test_all_sw_pass_through():
    assert len(SW_INDUSTRIES) == 31
    for sw in SW_INDUSTRIES:
        assert to_sw(sw) == sw


# ———————————— 证监会门类码 → 申万 ————————————
@pytest.mark.parametrize("name,expect", [
    ("C39计算机、通信和其他电子设备制造业", "电子"),
    ("C38电气机械和器材制造业", "电力设备"),
    ("J66货币金融服务", "银行"),
    ("J67资本市场服务", "非银金融"),
    ("K70房地产业", "房地产"),
    ("A01农业", "农林牧渔"),
    ("D44电力、热力生产和供应业", "公用事业"),
    ("C27医药制造业", "医药生物"),
    ("C15酒、饮料和精制茶制造业", "食品饮料"),
    ("B06煤炭开采和洗选业", "煤炭"),
])
def test_csrc_code_maps(name, expect):
    assert to_sw(name) == expect


# ———————————— 常见细分别名 → 申万 ————————————
@pytest.mark.parametrize("name,expect", [
    ("半导体", "电子"), ("白酒", "食品饮料"), ("光伏", "电力设备"),
    ("券商", "非银金融"), ("医疗器械", "医药生物"), ("水泥", "建筑材料"),
    ("新能源车", "汽车"), ("军工", "国防军工"), ("游戏", "传媒"),
])
def test_alias_maps(name, expect):
    assert to_sw(name) == expect


def test_alias_case_insensitive():
    assert to_sw("CXO") == "医药生物" and to_sw("cxo") == "医药生物"


# ———————————— 子串兜底 ————————————
@pytest.mark.parametrize("name,expect", [
    ("半导体设备", "电子"),        # 含"半导体"
    ("白酒龙头", "食品饮料"),
    ("锂电池材料", "电力设备"),
    ("光伏产业链", "电力设备"),
])
def test_substring_fallback(name, expect):
    assert to_sw(name) == expect


# ———————————— 无匹配 → None(绝不硬凑) ————————————
@pytest.mark.parametrize("name", ["某某概念", "ST摘帽", "", "   ", None, 123, "热点题材"])
def test_unmappable_returns_none(name):
    assert to_sw(name) is None


# ———————————— 映射表自洽 ————————————
def test_csrc_table_covers_83_and_targets_valid():
    assert len(CSRC_CODE_TO_SW) == 83                 # baostock 全部门类
    for code, sw in CSRC_CODE_TO_SW.items():
        assert sw in SW_INDUSTRIES, f"{code}→{sw} 非法申万一级"


def test_alias_targets_valid():
    for k, sw in ALIAS_TO_SW.items():
        assert sw in SW_INDUSTRIES, f"别名 {k}→{sw} 非法申万一级"


def test_granularity_loss_industries_reachable_via_alias():
    # 家用电器 / 美容护理:证监会不单列,只能经别名到达;确保不永久弃权
    assert to_sw("家电") == "家用电器"
    assert to_sw("化妆品") == "美容护理"


# ———————————— rrg.industry_row 取数前先对齐 ————————————
def test_industry_row_aligns_to_sw_before_load(monkeypatch):
    from tools.analysis import rrg
    rrg.clear_cache()
    calls = []

    class _DF:
        columns = ["close"]
        def __getitem__(self, k): return self
        def tolist(self): return [100.0 + i for i in range(80)]

    def fake_load(name):
        calls.append(name)
        return _DF()

    monkeypatch.setattr(rrg, "_bench_closes", lambda: [100.0] * 80)
    from tools.collectors import board
    monkeypatch.setattr(board, "load_board_kline", fake_load)

    row = rrg.industry_row("半导体")                  # 细分文本 → 应以"电子"加载 board_kline
    assert calls == ["电子"]
    assert row is not None and row["方向"] in ("看多", "看空", "中性")


def test_industry_row_unmappable_abstains(monkeypatch):
    from tools.analysis import rrg
    rrg.clear_cache()
    called = []
    from tools.collectors import board
    monkeypatch.setattr(board, "load_board_kline", lambda n: called.append(n))
    assert rrg.industry_row("某某妖股概念") is None    # 对不齐 → None
    assert called == []                                # 且未触碰 board 取数
