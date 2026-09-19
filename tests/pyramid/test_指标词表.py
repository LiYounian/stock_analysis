"""统一指标词表 语义锁（体检卡 v2）。

锁死（守则6·防未来重写误删）：
- render_词表() 四面齐全（基本面/技术面/资金面/消息情绪面）+ 出现在决策包 prompt 开头一次。
- **区间单一真源·防漂移**：render_区间 从各工具档位表现渲，与 格档 的档名映射恒一致——
  档位表改则词表自动跟随，词表绝不手抄漂移。
- _num/_pct 边界数字格式。
"""
import pytest

from tools.pyramid._common import 格档
from tools.pyramid.指标词表 import render_词表, render_区间, render_消息面词表, render_大盘词表, _num, _pct
from tools.pyramid.tools.valuation_tool import _PE参考档, _PB档, _PEG档
from tools.pyramid.tools.growth_quality_tool import _增速档, _负债率档
from tools.pyramid.tools.financial_redflag_tool import _QUALITY档
from tools.pyramid.tools.price_volume_tool import 量比档, pos60档
from tools.pyramid.tools.fund_flow_tool import _净占比档, _换手率档, _竞品档
from tools.pyramid.tools.stock_sentiment_tool import 净情绪档, 覆盖率档
import tools.pyramid.tools  # noqa: F401 触发 register


# ── 边界数字格式 ──
def test_num格式():
    assert _num(15.0) == "15"
    assert _num(0.7) == "0.7"
    assert _num(-5.0) == "-5"
    assert _num(0.05) == "0.05"


def test_pct格式():
    assert _pct(0.2) == "20"
    assert _pct(0.05) == "5"
    assert _pct(0.8) == "80"


# ── render_区间：末档=顶档渲成 >上一档上界（兼容 inf/1e9/1.01 有限哨兵）──
def test_render区间_PE():
    assert render_区间(_PE参考档) == "亏损≤0 / 低≤15 / 中≤30 / 偏高≤60 / 高>60"


def test_render区间_pos60_有限哨兵():
    # pos60档 顶档上界=1.01（非 inf），仍须渲成 '高>0.7' 不泄漏哨兵
    assert render_区间(pos60档) == "低≤0.3 / 中≤0.7 / 高>0.7"
    assert "1.01" not in render_区间(pos60档)


def test_render区间_量比_1e9哨兵():
    assert render_区间(量比档) == "缩量≤0.7 / 平量≤1.2 / 放量≤2.5 / 爆量>2.5"


def test_render区间_百分号与百分数():
    assert render_区间(_换手率档, 单位="%") == "过低≤1% / 正常≤3% / 活跃≤7% / 过热炒作>7%"
    assert render_区间(_竞品档, fmt=_pct, 单位="%") == "龙头级≤20% / 前排≤40% / 中游≤70% / 尾部>70%"


def test_render区间_空表():
    assert render_区间([]) == ""


# ── 防漂移核心锁：render_区间 的每个非顶档边界，格档 到该边界须命中同一档名 ──
@pytest.mark.parametrize("表", [
    _PE参考档, _PB档, _PEG档, _增速档, _负债率档, _QUALITY档,
    量比档, pos60档, _净占比档, _换手率档, 净情绪档, 覆盖率档,
])
def test_区间对拍档位表_防漂移(表):
    区间 = render_区间(表)
    for i, (上界, 档名, _解) in enumerate(表):
        # 每个档名都进了区间串
        assert 档名 in 区间, f"{档名} 未出现在 render_区间 输出：{区间}"
        if i < len(表) - 1:
            # 非顶档：格档(上界) 命中该档名（区间边界 = 格档语义，单一真源不漂）
            assert 格档(上界, 表)[0] == 档名


# ── render_词表 四面齐全 + 关键区间在位 ──
def test_词表四面齐全():
    txt = render_词表()
    assert "## 统一指标词表" in txt
    for 面 in ("**基本面**", "**技术面**", "**资金面**", "**消息情绪面**"):
        assert 面 in txt
    # 关键指标 + 其区间（从档位表现渲，非手抄）
    assert "市盈率 PE(TTM)" in txt and "低≤15 / 中≤30" in txt
    assert "换手率" in txt and "过热炒作>7%" in txt
    assert "财报质量五维 & quality" in txt and "成长" in txt and "回报" in txt
    assert "消息覆盖与可信度" in txt
    # 命名对齐全流程图HTML：资金面段用"同板块换手相对"、不再是旧名"竞品"
    assert "同板块换手相对" in txt and "竞品" not in txt
    # 对手盘(席位级) 缺口诚实标注在位；同类走势映射注在位
    assert "对手盘(席位级)" in txt and "缺口后置" in txt
    assert "同类走势(板块内peer)" in txt
    # 龙虎榜措辞对齐 HTML：个股汇总净买方向·非席位对手拆解
    assert "非席位对手拆解" in txt
    # ROE 无区间（未年化·不设绝对档）——只有定义，无 "档位(全A横截面)"
    roe行 = next(l for l in txt.splitlines() if l.startswith("- ROE"))
    assert "档位(全A横截面)" not in roe行


# ── 消息面词表节：方向/可信度/来源/国际标签/风险偏好 定义在位（状态类无区间）──
def test_消息面词表节():
    txt = render_消息面词表()
    assert "消息面词表" in txt
    for 名 in ("方向", "强弱", "可信度", "影响程度", "执行度", "来源",
              "国际/宏观标签", "风险偏好/广度/宏观净方向"):
        assert 名 in txt, f"消息面词表缺条目：{名}"
    # 状态类无档位 → 不带"档位(全A横截面)"
    assert "档位(全A横截面)" not in txt
    # 关键口径句在位（利好/利空/中性 三态、一手/二手 分级）
    assert "利好/利空/中性" in txt
    assert "一手" in txt and "二手" in txt


# ── 大盘词表节：方向分位/方向档/分歧标记/净广度/情绪/两融 定义在位（状态类无区间）+ 效力约束进词表 ──
def test_大盘词表节():
    txt = render_大盘词表()
    assert "大盘词表" in txt
    for 名 in ("方向分位", "方向档", "分歧标记", "净广度", "情绪", "两融"):
        assert 名 in txt, f"大盘词表缺条目：{名}"
    # 状态类无档位 → 不带"档位(全A横截面)"
    assert "档位(全A横截面)" not in txt
    # 效力条把关键诚实约束带进词表（防未来精简掉·段尾原句仍在）
    assert "无经济alpha" in txt
    assert "勿把高概率读成能赚钱" in txt
    assert "非涨跌幅" in txt  # 方向口径诚实标注
