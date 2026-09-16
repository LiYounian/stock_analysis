"""冷热标签(过冷/正常/过热/拐点)—— **预注册阈值,写死后只读不改**(改版 = 显式版本号)。

依据需求文档 §6:板块状态至少用 成交拥挤 / 价格强度 / 广度情绪 / 资金位置 / 新闻催化 五维。
P1 只用**可离线因果计算**的四类客观量(成交拥挤·价格强度·广度·动量);新闻催化(D)/资金位置
(北向游资)留 P2 增量。标签是**规则合成**(非模型),口径公开、可复核。

标签语义(需求文档 §6 原文对齐):
  过冷:成交萎缩、涨幅落后、补涨不启动、中军走弱 → 只"观察"不追。
  正常活跃:龙头领涨、中军趋势、成交稳定 → 可持续跟踪。
  过热:拥挤分位高、连涨过快、普涨、龙头加速缩量/放量滞涨 → 提示回撤风险。
  拐点:价量背离 + 中军转弱 + 龙头失位(P1 用"动量档与拥挤档背离"近似;新闻反向留 P2)。
"""
from __future__ import annotations

from typing import Optional

LABELS_VERSION = "v1-2026-09-16"

# —— 预注册阈值(写死·只读不改)——
# 拥挤分位(时序,vs 自身历史):≥ HOT 判过热侧,≤ COLD 判过冷侧。
CROWD_HOT = 0.80
CROWD_COLD = 0.20
# 动量截面分位(当日 vs 其它板块):≥ HOT 强动量,≤ COLD 弱动量。
MOM_CS_HOT = 0.80
MOM_CS_COLD = 0.20
# 广度:板块内上涨家数占比。
BREADTH_HOT = 0.70
BREADTH_COLD = 0.35


def classify_regime(*, 拥挤分位: Optional[float], 动量_截面分位: Optional[float],
                    动量_时序分位: Optional[float], 上涨家数占比: Optional[float]) -> dict:
    """规则合成冷热标签。返回 {标签, 依据, 置信}。缺关键维 → 标"数据不足"。

    合成机制(投票 + 背离检测,预注册):
      · 过热票 = 拥挤≥HOT + 截面动量≥HOT + 广度≥HOT,任二 → 过热。
      · 过冷票 = 拥挤≤COLD + 截面动量≤COLD + 广度≤COLD,任二 → 过冷。
      · 拐点 = 强动量却广度萎缩(价量/广度背离),或 拥挤高位而当日截面动量转弱。
      · 其余 = 正常活跃。
    """
    have = [v for v in (拥挤分位, 动量_截面分位, 上涨家数占比) if v is not None]
    if len(have) < 2:
        return {"标签": "数据不足", "依据": "可用维度 < 2(拥挤/动量/广度缺失)",
                "置信": "低", "version": LABELS_VERSION}

    hot_votes, cold_votes, reasons = 0, 0, []
    if 拥挤分位 is not None:
        if 拥挤分位 >= CROWD_HOT:
            hot_votes += 1; reasons.append(f"拥挤分位{拥挤分位:.2f}≥{CROWD_HOT}(高位)")
        elif 拥挤分位 <= CROWD_COLD:
            cold_votes += 1; reasons.append(f"拥挤分位{拥挤分位:.2f}≤{CROWD_COLD}(萎缩)")
    if 动量_截面分位 is not None:
        if 动量_截面分位 >= MOM_CS_HOT:
            hot_votes += 1; reasons.append(f"截面动量{动量_截面分位:.2f}≥{MOM_CS_HOT}(领涨)")
        elif 动量_截面分位 <= MOM_CS_COLD:
            cold_votes += 1; reasons.append(f"截面动量{动量_截面分位:.2f}≤{MOM_CS_COLD}(落后)")
    if 上涨家数占比 is not None:
        if 上涨家数占比 >= BREADTH_HOT:
            hot_votes += 1; reasons.append(f"上涨占比{上涨家数占比:.2f}≥{BREADTH_HOT}(普涨)")
        elif 上涨家数占比 <= BREADTH_COLD:
            cold_votes += 1; reasons.append(f"上涨占比{上涨家数占比:.2f}≤{BREADTH_COLD}(普跌)")

    # 拐点:强动量 + 广度萎缩(背离),或 拥挤高位 + 截面动量弱
    背离 = (动量_截面分位 is not None and 上涨家数占比 is not None
            and 动量_截面分位 >= MOM_CS_HOT and 上涨家数占比 <= BREADTH_COLD)
    高位转弱 = (拥挤分位 is not None and 动量_截面分位 is not None
                and 拥挤分位 >= CROWD_HOT and 动量_截面分位 <= MOM_CS_COLD)
    if 背离 or 高位转弱:
        why = "强动量却广度萎缩(价量背离)" if 背离 else "拥挤高位而截面动量转弱"
        return {"标签": "拐点", "依据": why, "置信": "中",
                "拐点方向": "下行拐点(风险)", "version": LABELS_VERSION}

    if hot_votes >= 2:
        return {"标签": "过热", "依据": "；".join(reasons), "置信": "高" if hot_votes == 3 else "中",
                "version": LABELS_VERSION}
    if cold_votes >= 2:
        return {"标签": "过冷", "依据": "；".join(reasons), "置信": "高" if cold_votes == 3 else "中",
                "version": LABELS_VERSION}
    return {"标签": "正常活跃", "依据": "；".join(reasons) or "各维度居中",
            "置信": "中", "version": LABELS_VERSION}
