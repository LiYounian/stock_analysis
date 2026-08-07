"""单事件判定:业绩超预期(PEAD)+ 公司行为(增减持/回购)。

纯函数、可独测;方向/强度只用**事件属性**(超预期幅度、规模占比),不用未来收益(防未来函数)。
口径源:docs/参考/选股与收益支点策略_网络调研.md §类别3;阈值真源 THRESHOLDS["事件驱动"]。
"""
from __future__ import annotations

from tools.config.strategy import THRESHOLDS

_C = THRESHOLDS["事件驱动"]


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, float(x)))


def judge_pead(forecast_growth: float | None, consensus: float | None = None,
               significant_line: float | None = None) -> dict:
    """业绩超预期判定(PEAD)。

    Args:
        forecast_growth: 预告/快报净利同比增速(%);为主判据。
        consensus: 可选一致预期增速(%);给了则用 (实际−预期)/|预期| 作超预期度,
                   否则用 forecast_growth 直接判(近似口径,见回传说明)。
        significant_line: 显著线(小数,如 0.20);缺省取配置 PEAD显著线。

    Returns:
        {方向: 看多/看空/中性, 超预期度: float, 达显著线: bool, 依据: str}
        超预期度为带符号小数(正=超预期上行)。
    """
    line = _C["PEAD显著线"] if significant_line is None else significant_line
    if forecast_growth is None:
        return {"方向": "中性", "超预期度": 0.0, "达显著线": False, "依据": "无业绩增速数据"}

    if consensus is not None and abs(consensus) > 1e-9:
        surprise = (forecast_growth - consensus) / abs(consensus)   # 相对一致预期
        basis = f"实际{forecast_growth}% vs 一致预期{consensus}% → 超预期{surprise:.0%}"
    else:
        # 近似:无一致预期时,用增速本身/100 作超预期度(±100% 增速≈满档),线性
        surprise = forecast_growth / 100.0
        basis = f"预告增速{forecast_growth}%(无一致预期,近似)"

    达显著线 = abs(surprise) >= line
    if surprise > 0:
        方向 = "看多"
    elif surprise < 0:
        方向 = "看空"
    else:
        方向 = "中性"
    return {"方向": 方向, "超预期度": round(_clamp(surprise), 6), "达显著线": bool(达显著线),
            "依据": basis}


def judge_corporate_action(action_type: str, scale_ratio: float | None = None,
                           threshold: float | None = None) -> dict:
    """公司行为判定:增持/回购(利多)、减持(利空);规模占比过小(象征性)→ 信号弱。

    Args:
        action_type: "增持"/"回购"/"减持"。
        scale_ratio: 规模/流通市值 等占比(小数);None=未知(不因未知而剔除,仅降强度)。
        threshold: 规模占比门槛;缺省取配置。低于门槛视为象征性 → 强度打折。

    Returns:
        {方向, 强度: [-1,1], 象征性: bool, 依据: str}
    """
    thr = _C["增持规模占比门槛"] if threshold is None else threshold
    base_dir = _C["公司行为方向"].get(action_type)
    if base_dir is None:
        return {"方向": "中性", "强度": 0.0, "象征性": False, "依据": f"未知行为类型 {action_type!r}"}

    方向 = "看多" if base_dir > 0 else "看空"
    if scale_ratio is None:
        强度 = 0.4 * base_dir            # 规模未知:给中低强度,方向仍有效
        依据 = f"{action_type}(规模未知)"
        象征 = False
    else:
        象征 = scale_ratio < thr
        # 规模占比映射到强度:门槛处约 0.3,10×门槛封顶 1.0
        mag = min(1.0, 0.3 + (scale_ratio / thr - 1.0) * 0.1) if scale_ratio >= thr else scale_ratio / thr * 0.3
        强度 = round(_clamp(mag) * base_dir, 6)
        依据 = f"{action_type}·占比{scale_ratio:.3%}" + ("(象征性,信号弱)" if 象征 else "")
    return {"方向": 方向, "强度": round(_clamp(强度), 6), "象征性": bool(象征), "依据": 依据}
