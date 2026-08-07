"""市场状态识别 Market Regime(V1 模块一)。

五因子**平权**(#4 已定)→ 情绪分 0–100 → 五档标签(冰点/熊市共振/震荡/分化/牛市共振)。
因子:① 指数多头(沪深300 MA5>10>20 试错 / 反之 避险)② 科技共振(核心龙头池同步性)
     ③ 量能(成交额环比 + 相对历史天量)④ 宽度(**模块二全市场达标占比**,经 store 读 view,不重算)
     ⑤ 涨跌停(反向股性/活跃度)。
诚实降级(F1 可追溯):缺数据/未标定的因子不参与平权、其余照算,并在「降级」字段声明;
五档边界/龙头池/宽度参考占比均为 Config 占位,**待策略端标定**。

依赖方向:分析层,纯计算(输入由编排层 pipeline/regime.py 备好);参数走 Config THRESHOLDS["市场状态"]。
需求见 docs/计划/V1_形态选股与市场状态系统.md 模块一 F1.1–F1.5。
"""
from __future__ import annotations

from tools.config.strategy import THRESHOLDS

_CFG = THRESHOLDS["市场状态"]


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, float(x)))


def _closes(index_df):
    if index_df is None or "close" not in getattr(index_df, "columns", []):
        return []
    return [float(x) for x in index_df["close"].tolist()]


# ———————————————————— 五因子(各返回 (子分[0,1]|None, 依据))————————————————————
def factor_指数多头(index_df, cfg=None) -> tuple:
    """沪深300 MA5>MA10>MA20 → 1.0(试错);反排列 → 0.0(避险);纠缠 → 0.5。样本不足→None。"""
    c = _closes(index_df)
    if len(c) < 20:
        return None, "指数样本不足(<20)"
    ma = lambda n: sum(c[-n:]) / n
    m5, m10, m20 = ma(5), ma(10), ma(20)
    if m5 > m10 > m20:
        return 1.0, f"多头排列 MA5>{m10:.0f}>{m20:.0f}(试错)"
    if m5 < m10 < m20:
        return 0.0, f"空头排列 MA5<{m10:.0f}<{m20:.0f}(避险)"
    return 0.5, "均线纠缠(中性)"


def factor_量能(index_df, cfg=None) -> tuple:
    """成交额(缺则量)环比放缩 + 相对历史天量比值,合成 [0,1]。无量→None。"""
    cfg = cfg or _CFG
    if index_df is None:
        return None, "无指数数据"
    col = "amount" if "amount" in getattr(index_df, "columns", []) \
        and index_df["amount"].notna().any() else "volume"
    if col not in getattr(index_df, "columns", []):
        return None, "无量能字段"
    vals = [float(x) for x in index_df[col].tolist() if x == x]      # 去 NaN
    if len(vals) < 2:
        return None, "量能样本不足"
    win = int(cfg["量能_历史天量窗口"])
    peak = max(vals[-win:]) or 1.0
    ratio_peak = _clamp(vals[-1] / peak)
    chg = vals[-1] / vals[-2] if vals[-2] else 1.0
    sub = _clamp(0.6 * ratio_peak + 0.4 * _clamp(chg / 2.0))         # 环比封顶 2×
    return round(sub, 4), f"{col}环比{chg:.2f}·相对天量{ratio_peak:.2f}"


def factor_宽度(达标占比, cfg=None) -> tuple:
    """宽度 = 模块二全市场达标占比 / 参考满档(占位),归一 [0,1]。缺 view→None。"""
    cfg = cfg or _CFG
    if 达标占比 is None:
        return None, "无达标占比(先跑模块二形态选股)"
    ref = float(cfg.get("宽度参考占比", 0.05)) or 0.05
    return round(_clamp(float(达标占比) / ref), 4), f"达标占比{达标占比}/参考{ref}"


def factor_科技共振(leader_pcts, cfg=None) -> tuple:
    """核心龙头池当日涨跌同步性 = 上涨占比 [0,1]。龙头池空/无数据→None(降级)。"""
    if not leader_pcts:
        return None, "核心龙头池未配置(待策略端)"
    ups = sum(1 for p in leader_pcts if isinstance(p, (int, float)) and p > 0)
    return round(ups / len(leader_pcts), 4), f"龙头 {ups}/{len(leader_pcts)} 上涨"


def factor_涨跌停(涨跌停, cfg=None) -> tuple:
    """反向股性:0.5 + (涨停−跌停)/(涨停+跌停)×0.5 → [0,1]。无家数数据→None(降级)。"""
    if not 涨跌停:
        return None, "无涨跌停家数(未接宽度采集)"
    up, dn = float(涨跌停.get("涨停", 0)), float(涨跌停.get("跌停", 0))
    if up + dn == 0:
        return 0.5, "涨跌停均0"
    return round(_clamp(0.5 + (up - dn) / (up + dn) * 0.5), 4), f"涨停{int(up)}/跌停{int(dn)}"


_FACTORS = {
    "指数多头": lambda inp, cfg: factor_指数多头(inp["index_df"], cfg),
    "科技共振": lambda inp, cfg: factor_科技共振(inp.get("leader_pcts"), cfg),
    "量能": lambda inp, cfg: factor_量能(inp["index_df"], cfg),
    "宽度": lambda inp, cfg: factor_宽度(inp.get("达标占比"), cfg),
    "涨跌停": lambda inp, cfg: factor_涨跌停(inp.get("涨跌停"), cfg),
}


def label_of(score, cfg=None) -> str:
    """情绪分 → 五档标签(读 Config 五档;score≤上界即该档,末档兜顶)。"""
    for label, upper in (cfg or _CFG)["五档"]:
        if score <= upper:
            return label
    return (cfg or _CFG)["五档"][-1][0]


def analyze(index_df=None, 达标占比=None, leader_pcts=None, 涨跌停=None,
            cfg=None) -> dict:
    """五因子平权 → 情绪分 0–100 → 五档标签。缺因子降级不崩,贡献可追溯。

    返回 {情绪分, 标签, 因子贡献{因子:{子分,权重,可用,依据}}, 达标占比, 降级[], 口径}。
    """
    cfg = cfg or _CFG
    inp = {"index_df": index_df, "达标占比": 达标占比,
           "leader_pcts": leader_pcts, "涨跌停": 涨跌停}
    贡献 = {}
    available = []
    降级 = []
    n = len(cfg["因子"])
    for name in cfg["因子"]:
        sub, why = _FACTORS[name](inp, cfg)
        可用 = sub is not None
        贡献[name] = {"子分": (round(sub, 4) if 可用 else None),
                      "权重": round(1.0 / n, 4), "可用": 可用, "依据": why}
        if 可用:
            available.append(sub)
        else:
            降级.append(f"{name}:{why}")

    情绪分 = round(sum(available) / len(available) * 100, 2) if available else 0.0
    标签 = label_of(情绪分, cfg)
    降级.append("五档边界/龙头池/宽度参考占比为占位默认,待策略端标定")
    return {
        "情绪分": 情绪分, "标签": 标签,
        "因子贡献": 贡献,
        "达标占比": 达标占比,
        "有效因子数": len(available), "总因子数": n,
        "降级": 降级,
        "口径": f"五因子平权(有效{len(available)}/{n})·情绪分=有效因子子分均值×100·五档读Config",
    }
