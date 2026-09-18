"""入场口径统一（A8）：`entry_rule` 字段化，d3_score 记分与 model_a 撮合读同一口径。

## 为什么有这个模块

历史上三处撮合口径不一致，导致同一票在不同引擎结论矛盾：
  · `entry_price` 工具：回填 MA5 回踩限价；
  · `d3_score.forward_return`：D0 收盘 → D+K 收盘（恒成交，不含踏空）；
  · `model_a` / `sector_news_forward._entry_labels`：限价=D0 收盘、D+1 回踩才成交。
典型事故：300776 在 d3_score 记「命中」、在 model_a 记「未成交（高开踏空）」。

本模块把撮合口径抽成**纯函数 + 枚举字段**，让三处读同一个 `entry_rule`：

  · ``"close"``（收盘→收盘）：D0 收盘即入、**恒成交**。全票可比、不含踏空，
    是 d3_score 历史默认；用于横截面记分与因子 rank IC（不受撮合噪声干扰）。
  · ``"limit"``（回踩限价·不追高开）：限价 = D0 收盘；次日 D+1 若 ``low ≤ 限价`` → 成交，
    成交价 = ``min(限价, D+1 开)``（低开跳空按开盘，更保守）；否则**未成交**
    （高开未回踩，final，踏空剔出分母）。= model_a / sector_news_forward 撮合口径。

同一 ``entry_rule`` 下，「entry_price 定限价 / d3_score 记分 / model_a 撮合」三处撮合结论
一致——由 ``tests/pyramid/test_entry_rule.py`` 语义锁死（改口径必过测试）。
"""
from __future__ import annotations

from typing import Optional

# 枚举（唯一真源；新增口径须同步 match_entry 分支 + 语义锁测试）
ENTRY_RULES = ("close", "limit")
DEFAULT_ENTRY_RULE = "close"   # d3_score 横截面记分默认（恒成交·全票可比）


def normalize_rule(rule: Optional[str]) -> str:
    """非法/缺省 → 回落默认，保证下游拿到合法枚举（防上游拼写错静默漂移口径）。"""
    return rule if rule in ENTRY_RULES else DEFAULT_ENTRY_RULE


def match_entry(
    rule: str,
    limit: Optional[float],
    next_open: Optional[float] = None,
    next_low: Optional[float] = None,
) -> dict:
    """撮合裁决（纯函数·三处共用唯一真源）。

    入参：
      · ``rule`` ∈ ENTRY_RULES；
      · ``limit`` = 预注册限价（两套口径都取 D0 收盘）；
      · ``next_open`` / ``next_low`` = 次日 D+1 开盘 / 最低（"limit" 口径撮合用；"close" 口径忽略）。

    返回：``{filled, entry_price, rule, note}``。
      · filled=True → entry_price 为成交价；
      · filled=False → 未成交（高开未回踩），entry_price=None；
      · filled=None → pending / 数据不足（次日未到期或缺 open/low），entry_price=None。
    """
    rule = normalize_rule(rule)
    if not isinstance(limit, (int, float)) or limit <= 0:
        return {"filled": None, "entry_price": None, "rule": rule, "note": "限价缺/非正"}

    if rule == "close":
        # 收盘即入·恒成交：成交价 = 限价（=D0 收盘）
        return {"filled": True, "entry_price": float(limit), "rule": rule, "note": "收盘即入"}

    # rule == "limit"：回踩限价·不追高开
    if not isinstance(next_low, (int, float)) or not isinstance(next_open, (int, float)):
        return {"filled": None, "entry_price": None, "rule": rule, "note": "次日未到期/缺开低"}
    if next_low <= limit:                       # 回踩到限价 → 成交
        price = float(min(limit, next_open))    # 低开跳空按开盘成交（更保守）
        return {"filled": True, "entry_price": price, "rule": rule, "note": "回踩成交"}
    return {"filled": False, "entry_price": None, "rule": rule,
            "note": "高开未回踩(≥限价)、按纪律不追、未成交"}
