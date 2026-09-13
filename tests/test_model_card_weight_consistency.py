"""模型卡 ↔ 代码 组权重一致性单测(防文档漂移)。

锁语义(硬红线):market_forecast 的四维组权重,**三方必须一致**——
  1. 代码真源常量:tools/config/strategy.py:370 的 THRESHOLDS["大盘预测"]["因子权重"];
  2. 模型卡记载值:docs/参考/模型卡/market_forecast.md 内 WEIGHTS_TABLE 机读小表;
  3. 本测试内 EXPECTED 常量(显式意图,改它=有意改语义)。

为什么锁:组权重是唯一真源常量,但无机制保证叙事文档跟着改。改代码忘改文档(或反之)
= 静默漂移。任一方与其余不符,本测试立即红——把"文档=代码"从口头约定变成会红的断言
(呼应约法第 6 条:测试锁"为什么改"的语义)。

改组权重的正确姿势:**同时**改 strategy.py:370、market_forecast.md 的 WEIGHTS_TABLE、
本文件 EXPECTED,并在 market_forecast.md §7 追加变更日志。三处不一致测试即红。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.config.strategy import THRESHOLDS

# 显式意图:market_forecast 四维组权重的"应然值"。资金流=0.0 是 kill-switch(2026-09-02 关)。
# 改这里 = 有意改语义,必须同步 strategy.py:370 与模型卡 WEIGHTS_TABLE。
EXPECTED = {"技术": 1.0, "广度": 1.0, "消息面": 1.0, "资金流": 0.0}

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CARD = _REPO_ROOT / "docs" / "参考" / "模型卡" / "market_forecast.md"

# WEIGHTS_TABLE 机读块:BEGIN/END 注释之间的 markdown 两列表 `| 维 | 值 |`。
_BEGIN = "WEIGHTS_TABLE:BEGIN"
_END = "WEIGHTS_TABLE:END"
# 表行:| 技术 | 1.0 |  —— 值必须是数字(-?\d+(.\d+)?),天然跳过表头(组权重现值)与分隔行(---)。
_ROW = re.compile(r"^\|\s*([^\s|]+)\s*\|\s*(-?\d+(?:\.\d+)?)\s*\|\s*$")


def _parse_card_weights() -> dict:
    """从模型卡 WEIGHTS_TABLE 块解析 {维: float}。找不到块/解析空 → 抛(视为红)。"""
    text = _CARD.read_text(encoding="utf-8")
    assert _BEGIN in text and _END in text, (
        f"模型卡缺 WEIGHTS_TABLE 机读块({_BEGIN}/{_END}):{_CARD}")
    block = text.split(_BEGIN, 1)[1].split(_END, 1)[0]
    out: dict = {}
    for line in block.splitlines():
        m = _ROW.match(line.strip())
        if not m:
            continue
        dim, val = m.group(1), m.group(2)
        if dim in ("维",):          # 表头
            continue
        out[dim] = float(val)
    assert out, f"WEIGHTS_TABLE 块解析不到任何权重行:{_CARD}"
    return out


def test_model_card_weights_match_code():
    """模型卡 WEIGHTS_TABLE == 代码真源(strategy.py:370)== EXPECTED。"""
    code_weights = THRESHOLDS["大盘预测"]["因子权重"]
    card_weights = _parse_card_weights()

    assert code_weights == EXPECTED, (
        f"代码组权重(strategy.py:370)与 EXPECTED 不符:\n"
        f"  代码={code_weights}\n  EXPECTED={EXPECTED}\n"
        "→ 若确为有意改动,请同步本测试 EXPECTED 与模型卡 WEIGHTS_TABLE + 变更日志。")

    assert card_weights == EXPECTED, (
        f"模型卡 WEIGHTS_TABLE 与 EXPECTED 不符(文档漂移):\n"
        f"  模型卡={card_weights}\n  EXPECTED={EXPECTED}\n"
        f"→ 请更新 {_CARD} 的 WEIGHTS_TABLE 块。")

    # 冗余但直白:直接锁"文档==代码"(即便将来 EXPECTED 被改错也能定位到底哪两方不一致)。
    assert card_weights == code_weights, (
        f"模型卡与代码组权重不一致:模型卡={card_weights} vs 代码={code_weights}")


def test_fundflow_kill_switch_is_off():
    """资金流维 kill-switch 现值=0.0(2026-09-02 关);翻回 0.3=provisional 激活需有意改。"""
    ff = THRESHOLDS["大盘预测"]["因子权重"]["资金流"]
    assert ff == 0.0, (
        f"资金流组权重现值应为 0.0(kill-switch 关),实为 {ff}。"
        "若确为激活(0.3 provisional),请同步 EXPECTED、模型卡 WEIGHTS_TABLE 与变更日志。")
