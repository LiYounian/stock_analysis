"""反转 + 低换手 复合选股(候选策略;借鉴 alpha-skills 的 A 股因子实证)。

思路来源:VernonOY/alpha-skills 的 `knowledge/factor-taxonomy.md` 给出的 A 股实证——
**短期反转 = A 股最强 alpha,低换手率 = 第二强**,且两者低相关(一看价格、一看成交),
符合"两个低相关弱因子叠加 >> 单个强因子"。本策略取二者等权复合,纯量价、全 A 可跑。
设计文档:docs/计划/反转低换手复合选股策略_设计与预注册.md。

分层(与 semi_factor / momentum 一致):
  · 纯因子函数(本文件上半):输入单票时序 → 原始因子值;可脱离 IO 独测。
  · 选股策略(本文件下半 `combo_reversal_turnover_screen`):输入已预算原始因子的
    中心记录 → 横截面 winsorize+zscore → 等权复合 → 业务过滤 → 排序取 top_k。
  · 薄管线(tools/pipeline/screen_reversal_turnover.py):读一次 kline 算原始因子、
    建最小 record、落 view。

因子方向约定(与 alpha-skills 一致:值越大越看好):
  · 反转 rev_N   = -(close[-1]/close[-1-N] - 1)     跌得多 → 高分
  · 低换手 turn_N = -(近 N 日有效换手率均值)          冷门 → 高分

防未来函数:两因子只读所给序列的**尾部窗口**(反转仅用最后 N+1 根,换手仅用最后
N 根),不索引窗口之外;管线传"截至当日"序列,尾部即当日。回测(轮2)在历史日切片
`series[:t+1]` 后调用,天然不泄露未来。

命名:面板策略 0-9 已满,本策略**先作候选**,仅用 @strategy 名「反转低换手组合」;
回测达标且评测决定上线后,再由统筹授面板编号(策略10),不达标只留代码 + 报告。
"""
from __future__ import annotations

import math
from typing import Optional

from tools.config.strategy import THRESHOLDS
from tools.strategy._factor_util import winsorize_med, zscore
from tools.strategy.registry import strategy

# —— 默认参数(单一真源在 THRESHOLDS["反转低换手"];此处取值,缺键兜底)——
_CFG = THRESHOLDS.get("反转低换手", {})
_REV_N = int(_CFG.get("反转窗口", 5))
_TURN_N = int(_CFG.get("换手窗口", 20))
_W = _CFG.get("权重", {"反转": 0.5, "低换手": 0.5})
_W_REV = float(_W.get("反转", 0.5))
_W_TURN = float(_W.get("低换手", 0.5))
_TOP_K = int(_CFG.get("top_k", 20))
_MIN_AMOUNT_WAN = float(_CFG.get("流动性_最小成交额_万元", 5000))
_LIMIT_PCT = float(_CFG.get("涨跌停触板%", 9.7))
_EXCLUDE_BOARD_HEAD = bool(_CFG.get("剥离创业科创北交", False))
_FIELD = "反转低换手"                     # record 里存放原始因子的命名空间键


# ————————————————————————————————————————————————————————————————
# 一、纯因子函数(输入单票时序,输出原始因子值;None = 数据不足/非法)
# ————————————————————————————————————————————————————————————————
def reversal_factor(closes, n: int = _REV_N) -> Optional[float]:
    """短期反转:rev_N = -(close[-1]/close[-1-N] - 1)。跌得多 → 高分。

    只用最后 N+1 根(防未来函数:窗口外的值不影响结果)。
    不足 N+1 根 / 基准价 ≤0 / 含 NaN → None。
    """
    if closes is None or len(closes) < n + 1:
        return None
    c_now = closes[-1]
    c_base = closes[-1 - n]
    if not _finite(c_now) or not _finite(c_base) or c_base <= 0:
        return None
    return -(float(c_now) / float(c_base) - 1.0)


def low_turnover_factor(turnovers, n: int = _TURN_N,
                        min_valid: Optional[int] = None) -> Optional[float]:
    """低换手:turn_N = -(近 N 日**有效**换手率均值)。冷门 → 高分。

    近端常有采集滞后(新端点不给换手率,末几根为 NaN),故对窗口内有效值取均值,
    需有效点 ≥ min_valid(默认 max(1, N//2))否则 None。只用最后 N 根。
    """
    if turnovers is None or len(turnovers) == 0:
        return None
    window = turnovers[-n:]
    valid = [float(v) for v in window if _finite(v)]
    need = min_valid if min_valid is not None else max(1, n // 2)
    if len(valid) < need:
        return None
    return -(sum(valid) / len(valid))


def avg_amount_wan(amounts, n: int = _TURN_N,
                   min_valid: Optional[int] = None) -> Optional[float]:
    """流动性:近 N 日**有效**成交额均值(元)→ 万元。用于低流动性剔除,不进打分。

    amount 单位为元(主档 kline 口径),/1e4 换算万元。近端 NaN 同样按有效值处理。
    """
    if amounts is None or len(amounts) == 0:
        return None
    window = amounts[-n:]
    valid = [float(v) for v in window if _finite(v)]
    need = min_valid if min_valid is not None else max(1, n // 2)
    if len(valid) < need:
        return None
    return (sum(valid) / len(valid)) / 1e4


def pv_diverge_factor(closes, volumes, window: int = 20) -> Optional[float]:
    """量价背离(预留第三腿,策略默认不启用):pvd = -corr(日收益率, 成交量变化率)。

    价涨量缩(主力偷偷出货)→ corr 为负 → 取负后高分。只用最后 window+1 根。
    数据不足 / 方差为 0 / 含 NaN → None。
    """
    if closes is None or volumes is None:
        return None
    if len(closes) < window + 1 or len(volumes) < window + 1:
        return None
    c = [float(x) for x in closes[-(window + 1):]]
    v = [float(x) for x in volumes[-(window + 1):]]
    if any(not _finite(x) for x in c + v):
        return None
    rets = [c[i] / c[i - 1] - 1.0 for i in range(1, len(c)) if c[i - 1] != 0]
    vrets = [v[i] / v[i - 1] - 1.0 for i in range(1, len(v)) if v[i - 1] != 0]
    if len(rets) != len(vrets) or len(rets) < 2:
        return None
    corr = _pearson(rets, vrets)
    if corr is None:
        return None
    return -corr


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def _pearson(a: list[float], b: list[float]) -> Optional[float]:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va == 0 or vb == 0:
        return None
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / (va ** 0.5 * vb ** 0.5)


# ————————————————————————————————————————————————————————————————
# 二、选股策略(横截面标准化 + 等权复合 + 业务过滤 + 排序)
# ————————————————————————————————————————————————————————————————
def _code_head_excluded(code: str) -> bool:
    """剥离创业(30)/科创(68)/北交(8/4)/B或退(9),与策略C 同口径。"""
    return code.startswith(("30", "68", "8", "4", "9"))


_SCHEMA = {
    "records": "dict[code, 中心记录];每条含 record['反转低换手']={rev,turn,amount_wan} + snapshot(pct_chg)",
    "top_k": f"目标持仓数(默认 {_TOP_K})",
    "w_rev": f"反转权重(默认 {_W_REV})",
    "w_turn": f"低换手权重(默认 {_W_TURN})",
    "min_amount_wan": f"低流动性下限·万元(默认 {_MIN_AMOUNT_WAN})",
    "exclude_board_head": f"剥离创业/科创/北交/退(默认 {_EXCLUDE_BOARD_HEAD})",
}


@strategy("反转低换手组合", "选股", params_schema=_SCHEMA)
def combo_reversal_turnover_screen(
    records: dict[str, dict],
    top_k: int = _TOP_K,
    w_rev: float = _W_REV,
    w_turn: float = _W_TURN,
    min_amount_wan: float = _MIN_AMOUNT_WAN,
    exclude_board_head: bool = _EXCLUDE_BOARD_HEAD,
    limit_pct: float = _LIMIT_PCT,
) -> dict:
    """反转低换手复合选股(候选策略)。

    每票读预算好的原始因子 record['反转低换手'] = {rev, turn, amount_wan};业务过滤后
    对 rev / turn 各自 winsorize+zscore,等权复合 score = w_rev·z(rev)+w_turn·z(turn),
    降序取 top_k。跳过原因(停牌/涨跌停/低流动性/剥离/因子缺失)分类计数,诚实降级。
    样本 <2 无法横截面标准化 → 空 + note。
    """
    skip: dict[str, int] = {}

    def _skip(reason: str):
        skip[reason] = skip.get(reason, 0) + 1

    scoped: list[tuple[str, float, float, float]] = []  # (code, rev, turn, amount_wan)
    for code, rec in (records or {}).items():
        if exclude_board_head and _code_head_excluded(code):
            _skip("剥离板块头")
            continue
        snap = (rec or {}).get("snapshot")
        if not snap:                                    # 停牌 / 无快照
            _skip("停牌或无快照")
            continue
        pct = snap.get("pct_chg")
        if isinstance(pct, (int, float)) and abs(pct) >= limit_pct:
            _skip("涨跌停")
            continue
        f = (rec or {}).get(_FIELD) or {}
        rev, turn, amt = f.get("rev"), f.get("turn"), f.get("amount_wan")
        if not (_finite(rev) and _finite(turn)):
            _skip("因子缺失")
            continue
        if not _finite(amt) or amt < min_amount_wan:
            _skip("低流动性")
            continue
        scoped.append((code, float(rev), float(turn), float(amt)))

    if len(scoped) < 2:                                 # 少于 2 只无法做横截面标准化
        return {"codes": [], "candidates": [], "top_k": top_k,
                "有效样本": len(scoped), "跳过": skip, "因子明细": [],
                "权重": {"反转": w_rev, "低换手": w_turn},
                "note": "有效样本 <2,无法做横截面标准化(全A 闭环采集后才有足量样本)"}

    codes = [c for c, *_ in scoped]
    rev_raw = [r for _, r, _, _ in scoped]
    turn_raw = [t for _, _, t, _ in scoped]

    rev_z = zscore(winsorize_med(rev_raw))
    turn_z = zscore(winsorize_med(turn_raw))
    scores = [rev_z[i] * w_rev + turn_z[i] * w_turn for i in range(len(codes))]

    ranked = sorted(
        zip(codes, scores, rev_raw, turn_raw, [a for *_, a in scoped], rev_z, turn_z),
        key=lambda x: x[1], reverse=True,
    )
    detail = [{
        "code": c, "综合分": round(s, 4),
        "rev": round(rv, 4), "turn": round(tn, 4), "amount_wan": round(am, 1),
        "rev_z": round(rz, 4), "turn_z": round(tz, 4),
    } for c, s, rv, tn, am, rz, tz in ranked]

    picked = [c for c, *_ in ranked[:top_k]]
    return {
        "codes": picked,
        "candidates": picked,
        "top_k": top_k,
        "有效样本": len(scoped),
        "跳过": skip,
        "因子明细": detail,
        "权重": {"反转": w_rev, "低换手": w_turn},
        "参数": {"反转窗口": _REV_N, "换手窗口": _TURN_N,
                 "min_amount_wan": min_amount_wan},
    }
