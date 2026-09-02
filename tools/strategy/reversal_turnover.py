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

命名:经回测(达标于「可交易池+5-10日+TopK≤20」边界)+ 用户评测放行,已授面板编号
**「策略10」**,状态=**前向观测中**(非「已验证可用」):net 绝对水平存幸存者偏差水分,
以前向观测为准。@strategy 名保持「反转低换手组合」。见 docs/策略/策略总览 策略10 行。
"""
from __future__ import annotations

import math
from typing import Optional

from tools.config.strategy import THRESHOLDS
from tools.strategy import reversal_veto
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


# ————————————————————————————————————————————————————————————————
# 一·补 换手数据护栏(治本现算兜底 + 覆盖率熔断)——
#   需求源:docs/每日分析/策略建议/反转策略换手因子覆盖率退化.md
#   根因:盘后闭环 spot 增量(带 turnover/amount)失败 → 回退腾讯逐只(volume-only)
#         推进主档 → 近端 turnover/amount 整片 NaN → low_turnover_factor 近端有效点不足
#         → 有效样本崩塌且无告警(静默失效)。
#   两道防线均**纯函数**(不触 IO,可脱离数据独测);由薄管线在装载序列/组装视图时调用。
# ————————————————————————————————————————————————————————————————
def turnover_guard_cfg() -> dict:
    """读「反转低换手.换手数据护栏」配置(单一真源,缺键兜默认)。"""
    return (_CFG.get("换手数据护栏") or {})


def _median(xs: list[float]) -> Optional[float]:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return None
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def _mad_over_median(xs: list[float]) -> Optional[float]:
    """稳健离散度:MAD/|median|(比 std/mean 抗离群,防个别除权/坏点误判"不稳")。"""
    med = _median(xs)
    if med is None or med == 0:
        return None
    mad = _median([abs(x - med) for x in xs])
    if mad is None:
        return None
    return mad / abs(med)


def derive_missing_turnover_amount(closes, volumes, turnovers, amounts,
                                   cfg: Optional[dict] = None):
    """近端缺 turnover/amount 时按 volume 现算兜底(治本;自校验,不确定就不填)。

    输入:同一 kline 的等长时序(index 对齐;某列整缺时传空 → 视作整列 NaN)。
    返回:(turnovers, amounts, info)——info 记 {turnover_derived/turnover_refused/amount_derived}。

    换手率现算:换手率% = volume(股) / 流通股本 × 100 → 同一票 turnover/volume 近似常数。
      取最近「参考窗口」内 (turnover,volume) 均有效的点估其中位比率 r_med;仅当
      ①参考点数 ≥ 门槛 且 ②比率稳健离散度 MAD/median ≤ 上限(否则视为除权/单位漂移,不现算)
      才对该窗口内"有 volume 无 turnover"的 bar 现算 turnover = volume × r_med;
      且要求该 bar 的 volume 落在参考中位量的 [1/跳变上限, 跳变上限] 内(挡 volume 单位跳变),
      越界则拒填(留 NaN 交给覆盖率熔断),绝不注入量级失真的伪值。
    成交额现算:amount ≈ volume × close(VWAP 近似,实证中位相对误差 ~1%);仅填近端缺口。

    防未来函数:比率只用"缺口之前"的有效 turnover 点(近端缺失区永远是序列尾部,参考点必在其前);
      回测按 series[:t+1] 切片后调用,参考窗口天然 ≤ t,不引入未来信息。
    """
    cfg = turnover_guard_cfg() if cfg is None else cfg
    info = {"turnover_derived": 0, "turnover_refused": 0, "amount_derived": 0}
    n = len(closes) if closes is not None else 0
    if n == 0 or not cfg.get("启用", True):
        return turnovers, amounts, info

    turnovers = list(turnovers) if turnovers else [float("nan")] * n
    amounts = list(amounts) if amounts else [float("nan")] * n
    volumes = list(volumes) if volumes else [float("nan")] * n
    closes = list(closes)
    ref_win = int(cfg.get("现算_参考窗口", 60))
    lo = max(0, n - ref_win)

    # —— 换手率现算 ——
    if cfg.get("换手率现算兜底", True) and len(volumes) == n and len(turnovers) == n:
        min_ref = int(cfg.get("现算_最少参考点", 20))
        cv_max = float(cfg.get("现算_比率变异上限", 0.15))
        vjump = float(cfg.get("现算_成交量跳变上限", 5.0))
        ratios, ref_vols = [], []
        for i in range(lo, n):
            t, v = turnovers[i], volumes[i]
            if _finite(t) and t > 0 and _finite(v) and v > 0:
                ratios.append(t / v)
                ref_vols.append(v)
        if len(ratios) >= min_ref:
            cv = _mad_over_median(ratios)
            r_med = _median(ratios)
            v_med = _median(ref_vols)
            if cv is not None and cv <= cv_max and r_med and v_med:
                for i in range(lo, n):
                    t, v = turnovers[i], volumes[i]
                    if _finite(t) or not (_finite(v) and v > 0):
                        continue
                    if v_med / vjump <= v <= v_med * vjump:
                        turnovers[i] = v * r_med
                        info["turnover_derived"] += 1
                    else:
                        info["turnover_refused"] += 1

    # —— 成交额现算(amount ≈ volume × close)——
    if cfg.get("成交额现算兜底", True) and len(volumes) == n and len(amounts) == n:
        for i in range(lo, n):
            a, v, c = amounts[i], volumes[i], closes[i]
            if _finite(a) or not (_finite(v) and v > 0 and _finite(c) and c > 0):
                continue
            amounts[i] = v * c
            info["amount_derived"] += 1

    return turnovers, amounts, info


def coverage_gate(coverage: Optional[float], valid_samples: Optional[int],
                  cfg: Optional[dict] = None) -> dict:
    """换手覆盖率熔断决策(纯函数;供薄管线组装视图时决定 present / ⚠ / 本日不出)。

    返回 {present, level(正常|警示|不出), 熔断(bool), note, coverage}。
      · 有效样本 < zscore_最小样本 → 不出(极小样本 z-score 虚高造伪极值)。
      · 覆盖率 < 不出下限        → 不出(数据不足·本日不出;比输出"少而偏"更安全)。
      · 覆盖率 ∈ [不出下限,警示下限) → 仍出但打 ⚠(排序可信度下降)。
      · 否则                     → 正常。
    kill-switch:护栏.启用=False → 恒 present=True 正常(现状)。
    """
    cfg = turnover_guard_cfg() if cfg is None else cfg
    out = {"present": True, "level": "正常", "熔断": False, "note": None,
           "coverage": coverage}
    if not cfg.get("启用", True):
        return out
    not_out = float(cfg.get("覆盖率_不出下限", 0.30))
    warn = float(cfg.get("覆盖率_警示下限", 0.50))
    min_z = int(cfg.get("zscore_最小样本", 200))
    if valid_samples is not None and valid_samples < min_z:
        out.update(present=False, level="不出", 熔断=True,
                   note=(f"有效样本 {valid_samples} < zscore 最小样本 {min_z},"
                         "极小样本横截面 z-score 量级虚高会造伪极值,本日不出"))
        return out
    if coverage is not None and coverage < not_out:
        out.update(present=False, level="不出", 熔断=True,
                   note=(f"换手覆盖率 {coverage:.1%} < 不出下限 {not_out:.0%},"
                         "数据不足·本日不出(拒绝输出少而偏的小样本 TopK)"))
        return out
    if coverage is not None and coverage < warn:
        out.update(present=True, level="警示", 熔断=True,
                   note=(f"⚠ 换手覆盖率 {coverage:.1%} 偏低(< 警示下限 {warn:.0%}),"
                         "横截面排序可信度下降,谨慎采信"))
        return out
    return out


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
    apply_veto: Optional[bool] = None,
) -> dict:
    """反转低换手复合选股(候选策略)。

    每票读预算好的原始因子 record['反转低换手'] = {rev, turn, amount_wan};业务过滤后
    对 rev / turn 各自 winsorize+zscore,等权复合 score = w_rev·z(rev)+w_turn·z(turn),
    降序取 top_k。跳过原因(停牌/涨跌停/低流动性/剥离/因子缺失)分类计数,诚实降级。
    样本 <2 无法横截面标准化 → 空 + note。

    否决层(反转专属·基本面/消息面):record 若挂 record['风险特征'](薄管线 as-of 抽取,见
    reversal_veto.extract_features),且开关开(apply_veto=None 读 config 反转否决层.启用;
    True/False 显式覆盖,供 A/B 回测)→ 打分后按裁决**降级(综合分减罚分沉底、标"高风险博弈")**或
    **否决(剔除/强制沉底)**。无风险特征 / 开关关 → no-op(纯量价现状不回归)。⚠️ 非投资建议。
    """
    skip: dict[str, int] = {}

    def _skip(reason: str):
        skip[reason] = skip.get(reason, 0) + 1

    veto_cfg = reversal_veto.cfg()
    veto_on = reversal_veto.enabled(veto_cfg) if apply_veto is None else bool(apply_veto)

    scoped: list[tuple[str, float, float, float]] = []  # (code, rev, turn, amount_wan)
    feat_map: dict[str, dict] = {}                      # code → record['风险特征'](供否决层)
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
        if veto_on:
            feat_map[code] = (rec or {}).get("风险特征")

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

    # —— 否决层:逐票裁决 → 调整后排序分(降级减罚分 / 否决强制沉底);未开或无特征 → 恒等 no-op ——
    veto_hits = {"降级": 0, "否决": 0, "剔除": 0}
    veto_by_axis: dict[str, int] = {}
    verdicts: list[dict] = []
    adj_scores: list[float] = []
    for i, code in enumerate(codes):
        v = reversal_veto.veto_verdict(feat_map.get(code), veto_cfg) if veto_on else \
            {"触发": False, "否决": False, "剔除": False, "动作": None, "原因": [], "轴": {}, "罚分": 0.0}
        verdicts.append(v)
        adj_scores.append(reversal_veto.apply_to_score(scores[i], v))
        if veto_on and v.get("触发"):
            if v.get("否决"):
                veto_hits["否决"] += 1
                if v.get("剔除"):
                    veto_hits["剔除"] += 1
            else:
                veto_hits["降级"] += 1
            for ax, hit in (v.get("轴") or {}).items():
                if hit:
                    veto_by_axis[ax] = veto_by_axis.get(ax, 0) + 1

    ranked = sorted(
        zip(codes, scores, rev_raw, turn_raw, [a for *_, a in scoped],
            rev_z, turn_z, adj_scores, verdicts),
        key=lambda x: x[7], reverse=True,               # 按调整后分排序(否决层生效点)
    )
    detail = []
    for c, s, rv, tn, am, rz, tz, adj, v in ranked:
        row = {
            "code": c, "综合分": round(s, 4),
            "rev": round(rv, 4), "turn": round(tn, 4), "amount_wan": round(am, 1),
            "rev_z": round(rz, 4), "turn_z": round(tz, 4),
        }
        if veto_on and v.get("触发"):
            row["调整后分"] = round(float(adj), 4)
            row["否决层"] = {"动作": v.get("动作"), "剔除": bool(v.get("剔除")),
                            "罚分": v.get("罚分"), "原因": v.get("原因"),
                            "标签": "高风险博弈,不作反转买入候选"}
        detail.append(row)

    # 入选:剔除(否决且不保留展示)的票不进榜;其余按调整后分序取 top_k
    picked = [c for c, _s, _rv, _tn, _am, _rz, _tz, _adj, v in ranked
              if not v.get("剔除")][:top_k]

    out = {
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
    if veto_on:
        out["否决层"] = {
            "启用": True, "模式": veto_cfg.get("模式", "降级"),
            "命中数": veto_hits, "分轴命中": veto_by_axis,
            "有风险特征票数": sum(1 for c in codes if feat_map.get(c) is not None),
            "说明": "反转专属否决/降级层(基本面空心/事件博弈/治理风险/重组未完成);⚠️非投资建议",
        }
    return out
