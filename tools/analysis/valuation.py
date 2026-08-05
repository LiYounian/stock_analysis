"""估值适用性判定(PE 开关)。

实践发现 PE 在亏损/周期底/微利标的大面积失效(芯原 PE 负、晶合 131 因折旧)。
本模块给定基本面 → 判 PE 是否适用 + 推荐替代估值基准。阈值读 strategy 配置。
公式见 strategy.FORMULAS['PE适用性开关']。
"""
from __future__ import annotations

from tools.config.strategy import THRESHOLDS


def pe_switch(fundamental: dict) -> dict:
    """输入 fundamental(含 PE_TTM/净利/净利增速/毛利率/PB);输出估值适用性判定。

    返回 {pe_valid: bool, mode: str, basis: str}。
    """
    t = THRESHOLDS["PE开关"]
    pe = fundamental.get("PE_TTM")
    net = fundamental.get("净利")
    growth = fundamental.get("净利增速")
    gm = fundamental.get("毛利率")

    # 1. 亏损 / PE 非正 → 失效
    if pe is None or pe <= 0 or (net is not None and net < 0):
        return {"pe_valid": False, "mode": "亏损/PE失效",
                "basis": "改用 PB + PS(市销率)+ 在手订单 + 扭亏进度"}

    # 2. PE 异常低 + 净利暴增 → 疑一次性损益(如资产处置),扣非后失真
    if pe < t["低PE告警"] and growth is not None and growth > t["净利暴增阈值"]:
        return {"pe_valid": False, "mode": "低PE_疑一次性损益",
                "basis": "PE 异常低疑非经常损益抬高净利,改看扣非 PE + 主业盈利趋势"}

    # 3. 高 PE 且伴随负增长或低毛利 → 折旧/周期底扰动
    high_pe = pe > t["高PE告警"]
    weak = (growth is not None and growth < 0) or (gm is not None and gm < t["低毛利阈值"])
    if high_pe and weak:
        return {"pe_valid": False, "mode": "高PE_折旧/周期底扰动",
                "basis": "PE 被压制,参考 PB + 毛利率趋势/稼动率 + 周期位置"}

    # 4. PE 适用
    return {"pe_valid": True, "mode": "PE适用",
            "basis": "PE(TTM) 可作横向对比,结合增速看 PEG"}
