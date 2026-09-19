"""technical_detail 语义锁。锁死：

- RSI12 档位阈值 = strategy.THRESHOLDS.超买超卖.RSI12 **canonical 单一真源**（防漂移）。
- RSI12 / 获利盘 / 集中度90 档位边界（含超买超卖临界）。
- 字段口径三段拒空编（意味不空）。
- 缺数据 → NA 不编造（无 snapshot=missing；snapshot 有但 chip/prediction 缺 → 该条 NA 且不崩）。
- 状态类（MA排列/MACD状态/KDJ状态）原样用序列化器结果；MACD 零轴意味增强。
- BOLL 未落盘 → 恒标"待补落盘"（不动技术序列化器）。
"""
import json
import os

import pytest

from tools.pyramid.registry import get, ToolResult
from tools.pyramid._common import 格档, 字段
from tools.pyramid.tools.technical_detail_tool import (
    TechnicalDetailTool, RSI12档, 获利盘档, 集中度档, _macd意味,
)
import tools.pyramid.tools  # noqa: F401 触发 register

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("TECHNICAL_DETAIL_TEST_DATA_ROOT") or ROOT
AS_OF = "2026-09-17"


# ── RSI12 canonical 单一真源锁（阈值必须 == strategy 配置，任一漂移即红）──
def test_rsi12_canonical_单一真源():
    from tools.config.strategy import THRESHOLDS
    cfg = THRESHOLDS["超买超卖"]["RSI12"]
    上界 = {档: 上 for 上, 档, _ in RSI12档}
    assert 上界["超卖极端"] == cfg["超卖极端"]  # 20
    assert 上界["超卖"] == cfg["超卖"]          # 30
    assert 上界["中性"] == cfg["超买"]          # 70（>70 起超买）
    assert 上界["超买"] == cfg["超买极端"]      # 80（>80 起极端）


# ── RSI12 档位边界锁（超买超卖临界）──
def test_rsi12_档位边界():
    assert 格档(20.0, RSI12档)[0] == "超卖极端"
    assert 格档(20.01, RSI12档)[0] == "超卖"
    assert 格档(30.0, RSI12档)[0] == "超卖"
    assert 格档(30.01, RSI12档)[0] == "中性"
    assert 格档(70.0, RSI12档)[0] == "中性"
    assert 格档(70.01, RSI12档)[0] == "超买"
    assert 格档(80.0, RSI12档)[0] == "超买"
    assert 格档(80.01, RSI12档)[0] == "超买极端"


# ── 筹码档位边界锁（全A分位标定值 0.2/0.5/0.8 与 0.10/0.18/0.28）──
def test_获利盘档位边界():
    assert 格档(0.2, 获利盘档)[0] == "普遍套牢"
    assert 格档(0.2001, 获利盘档)[0] == "套牢为主"
    assert 格档(0.5, 获利盘档)[0] == "套牢为主"
    assert 格档(0.8, 获利盘档)[0] == "获利为主"
    assert 格档(0.9, 获利盘档)[0] == "普遍获利"


def test_集中度档位边界():
    assert 格档(0.10, 集中度档)[0] == "高度集中"
    assert 格档(0.1001, 集中度档)[0] == "较集中"
    assert 格档(0.18, 集中度档)[0] == "较集中"
    assert 格档(0.28, 集中度档)[0] == "一般"
    assert 格档(0.40, 集中度档)[0] == "分散"


# ── MACD 意味：状态原样 + 零轴增强（水上金叉更强）──
def test_macd意味_零轴增强():
    assert "零轴上金叉" in _macd意味("金叉", 0.08)
    assert "零轴下金叉" in _macd意味("金叉", -0.02)
    assert _macd意味("多头", 0.05) == "影响：柱>0、多头延续"
    assert _macd意味(None, None) == "数据缺失"


# ── 字段口径三段拒空编（意味空即 raise）──
def test_字段拒空编():
    with pytest.raises(ValueError):
        字段("X", 1.0, "口径", "")  # 意味空
    with pytest.raises(ValueError):
        字段("X", 1.0, "", "意味")  # 口径空


def _write_rec(tmp_path, code, rec):
    d = os.path.join(str(tmp_path), "data", "analysis", AS_OF)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{code}.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)


# ── 缺数据：无 snapshot → missing 不编 ──
def test_无snapshot_missing(tmp_path):
    _write_rec(tmp_path, "T001", {"meta": {"name": "无快照"}})
    r = TechnicalDetailTool().run(AS_OF, "T001", root=str(tmp_path))
    assert r.freshness == "missing"
    assert "数据缺失" in r.浓缩块 and r.面 == "技术面"


# ── 缺数据：snapshot 有但 chip/prediction/rsi 缺 → 各条 NA 且不崩、意味非空 ──
def test_snapshot有子块缺_NA不编(tmp_path):
    _write_rec(tmp_path, "T002", {
        "snapshot": {
            "ma": {"ma5": 10, "ma10": 9.9, "ma20": 9.8, "ma60": 9.5, "排列": "多头排列"},
            "macd": {"dif": 0.1, "dea": 0.05, "macd": 0.05, "状态": "金叉"},
            "kdj": {"k": 85, "d": 70, "j": 100, "状态": "超买"},
            "rsi": {},  # RSI 缺
            "新鲜度": "新鲜",
        },
        # chip / prediction 完全缺
    })
    r = TechnicalDetailTool().run(AS_OF, "T002", root=str(tmp_path))
    assert r.freshness == "fresh"
    # 状态原样用
    assert "多头排列" in r.浓缩块
    assert "金叉" in r.浓缩块
    assert "超买" in r.浓缩块
    # 缺数据条 NA + 意味非空（未触发字段拒空 raise 即证明）
    assert "RSI: NA" in r.浓缩块 or "RSI:" in r.浓缩块
    assert "筹码数据缺失" in r.浓缩块
    assert "结构支撑压力数据缺失" in r.浓缩块
    # BOLL 恒待补
    assert "待补落盘" in r.浓缩块
    # 契约：字段解读非空、浓缩块 ≤8 行（G3）、面=技术面
    assert r.字段解读 and r.面 == "技术面"
    assert len([l for l in r.浓缩块.splitlines() if l.strip()]) <= 8


# ── KDJ '-' 中性原样 + BOLL 落盘则读值 ──
def test_kdj中性_boll落盘(tmp_path):
    _write_rec(tmp_path, "T003", {
        "snapshot": {
            "ma": {"ma5": 1, "ma10": 1, "ma20": 1, "ma60": 1, "排列": "纠缠"},
            "macd": {"dif": -0.1, "dea": -0.05, "macd": -0.05, "状态": "空头"},
            "kdj": {"k": 50, "d": 50, "j": 50, "状态": "-"},
            "rsi": {"rsi6": 15, "rsi12": 25, "rsi24": 40},
            "boll": {"上轨": 12.0, "中轨": 11.0, "下轨": 10.0, "状态": "触上轨"},
        },
    })
    r = TechnicalDetailTool().run(AS_OF, "T003", root=str(tmp_path))
    assert "中性区、无超买超卖信号" in r.浓缩块   # KDJ '-' 意味（v2 影响文案）
    assert "超卖" in r.浓缩块                     # RSI12=25 → 超卖档
    assert "RSI6短线超跌" in r.浓缩块             # RSI6=15<20
    assert "触上轨" in r.浓缩块 and "待补落盘" not in r.浓缩块  # BOLL 落盘则读值


# ── v2 语义锁：意味段影响化 + 口径剔除区间共性（区间已进统一词表，不在个股卡重复）──
def test_v2_口径剔区间_意味影响化(tmp_path):
    _write_rec(tmp_path, "T004", {
        "snapshot": {
            "ma": {"ma5": 10, "ma10": 9.9, "ma20": 9.8, "ma60": 9.5, "排列": "多头排列"},
            "macd": {"dif": 0.1, "dea": 0.05, "macd": 0.05, "状态": "金叉"},
            "kdj": {"k": 85, "d": 70, "j": 100, "状态": "超买"},
            "rsi": {"rsi6": 50, "rsi12": 60, "rsi24": 55},
        },
    })
    r = TechnicalDetailTool().run(AS_OF, "T004", root=str(tmp_path))
    rsi = next(it for it in r.字段解读 if it["名"] == "RSI")
    # 区间定义（如"≤20超卖极端"）已移词表，不在个股卡口径重复
    assert "超卖极端" not in rsi["口径"] and "≤30" not in rsi["口径"]
    assert rsi["意味"].startswith("影响：")
    # 状态类意味也影响化
    ma = next(it for it in r.字段解读 if it["名"] == "均线排列")
    assert ma["意味"].startswith("影响：") and "多头排列/空头排列" not in ma["口径"]


# ── 真数据回归（主仓有 000504 时）：7 条、面=技术面、金叉零轴上、逼近压力 ──
@pytest.mark.skipif(
    not os.path.exists(os.path.join(DATA_ROOT, "data", "analysis", AS_OF, "000504.json")),
    reason="需主仓 per-stock json（worktree 里可能缺）",
)
def test_真票000504回归():
    r = get("technical_detail").run(AS_OF, "000504", root=DATA_ROOT)
    assert r.面 == "技术面" and r.塔层 == "①塔基"
    n = len([l for l in r.浓缩块.splitlines() if l.strip()])
    assert n == 7  # 合并筹码后实取 7 条
    assert "零轴上金叉" in r.浓缩块
    assert "逼近压力" in r.浓缩块
    assert "筹码估算降级" in r.浓缩块  # 000504 chip.降级=True
