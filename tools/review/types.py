"""大复盘数据契约：Pick（选股票的"当时依据"）/ ModelALabels（事后收益）/ Attribution（归因）。

三者是模块间**冻结的 I/O 边界**（loaders→model_a→attribution→render）。字段一经下游依赖即勿删改，
新增走末尾追加（向后兼容）。所有"未知/未到期/算不到" 一律 None（有声缺失，不猜、不填 0）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Pick:
    """一只选股票的"当时依据"（≤信号日 D 的信息，防未来）。来源 = 今日选股_<date>.json 逐票记录
    （+ 板块级消息面）。统筹精选 v2/v3 只解析出 code 清单，字段多为 None（对照样本）。"""

    date: str                       # 信号日 D（选股产物日期）
    code: str
    name: str = ""
    version_tag: str = "今日选股"    # 产物/版本：今日选股 | 统筹精选v2 | 统筹精选v3 | ...
    # —— 归因来源（哪条线选出）——
    来源: Optional[str] = None       # 策略直选 | 板块催化（今日选股.个股.来源）
    角色: Optional[str] = None       # 龙头 | 中军 | 跟涨 | 策略（塔尖金字塔位置）
    档: Optional[str] = None         # 推荐 | 观察 | 中性观察 | 偏弱观察 | 剔除
    建议分: Optional[float] = None
    策略命中: list[str] = field(default_factory=list)
    board: Optional[str] = None
    # —— 结构化依据块（原样透传，判据在 attribution 里读）——
    council: dict[str, Any] = field(default_factory=dict)   # 综合方向/财报红旗数/龙虎榜否决/财报风险/行业
    形态: dict[str, Any] = field(default_factory=dict)       # 现价/ma5/当日涨跌/位置pos60/距60高/涨停不可买/均线多头...
    板块消息面: dict[str, Any] = field(default_factory=dict)  # board 级 tag/强弱/关键事件[]（催化证据）
    board_regime: dict[str, Any] = field(default_factory=dict)  # sector_regime join by board：冷热标签/拥挤档/动量_截面档
    # —— 自由文本（P_entry 自报敏感性 & 记录）——
    入场_text: Optional[str] = None
    止损_text: Optional[str] = None
    硬纪律命中: Optional[str] = None


@dataclass
class ModelALabels:
    """Model-A 统一口径事后收益（跨版/跨线唯一可比）。撮合复用 sector_news_forward._entry_labels。

    限价=D 收盘；D+1 low≤限价→成交@min(限价,D+1 open)，否则未触发（不进胜率分母）。
    r_d1=成交价→D+1 收盘%；r_exit=按卖出规则实际了结（D+1 收盘为正且未破 MA5→持到 D+2；否则 D+1 离场）。
    """

    filled: Optional[bool] = None    # True=成交 | False=未触发(高开未回踩,final) | None=D+1 未到期(pending)
    limit: Optional[float] = None    # 限价 = D 收盘
    entry_price: Optional[float] = None   # 成交价 = min(限价, D+1 open)
    r_d1: Optional[float] = None     # 成交价→D+1 收盘 绝对收益%（主胜率指标）
    r_d2: Optional[float] = None     # 成交价→D+2 收盘 绝对收益%
    r_exit: Optional[float] = None   # 实际了结收益%（主收益榜）
    exit_horizon: Optional[str] = None    # "d1"（当日离场）| "d2"（持到 D+2）| None
    close_positive: Optional[bool] = None # r_d1>0；None=未触发（compute_daily_row 据此剔出分母）
    stop_flag: bool = False          # 盘中破 MA5（D+1 low < 形态.ma5@D）→ 触发当日离场
    hold_to_d2: bool = False         # 持有到 D+2（r_d1>0 且未破 MA5）
    alpha_d1: Optional[float] = None      # r_d1 − 全A等权同期（D→D+1）
    alpha_exit: Optional[float] = None    # r_exit − 全A等权同期（按 exit_horizon）
    untriggered: bool = False        # = (filled is False)
    runaway_up: bool = False         # 未触发且 D+1 收盘 > 限价（票高开冲走没回踩）→ 踏空候选
    status: str = "pending"          # settled | partial | pending | not_entered
    p_entry_self: Optional[float] = None  # 自报回踩价（MA5/MA20，从 入场_text 解析）；次列敏感性
    note: Optional[str] = None


@dataclass
class Attribution:
    """规则归因结果。规则判不了 → None（有声缺失，不猜）。LLM 层（可选）另填 llm_* 字段。"""

    correct_bucket: Optional[str] = None   # 板块-龙头催化 | 板块-联动跟涨 | 策略 | 财报 | 形态
    wrong_bucket: Optional[str] = None     # 踏空 | 假利好 | 追高 | 财报红旗漏判 | 踏错板块基调 | 踏错大盘基调
    evidence: str = ""                     # 触发该桶的字段证据（人读）
    llm_bucket: Optional[str] = None       # LLM 归因（--llm-attr 开时）
    llm_reason: Optional[str] = None
