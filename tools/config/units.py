"""量价字段单位口径的**单一真源**(当前只管 `turnover` 换手率)。

## 为什么需要这个文件

`turnover` 由多个采集源写入主档,而**各源原始口径本来就不一致**:有的返回百分数
(3.46 = 换手 3.46%),有的返回小数(0.0346 = 同一天同一只票)。合并落主档时没有归一
→ 同一列里百分数与小数混存、相差 100 倍,任何直读 `master.turnover` 的代码都会把
"换手 3.46%"误判成"换手 0.03% 的极低换手日"(反转低换手策略的核心因子恰好吃这个)。

本模块把口径收拢成一处,**避免各消费侧各自打补丁**(项目已有教训:"代码→交易所"曾有
12 处独立实现,后收拢为 `tools/config/exchange.py` 单一真源)。

## 口径契约(唯一口径)

**`master.turnover` 一律是百分数**:`3.46` 表示换手 3.46%。
采集源若原始给小数,**必须在写入前**由 `to_percent()` 归一;新增源必须在
`TURNOVER_UNIT_BY_SOURCE` 里登记口径,否则 `to_percent()` 会记 ERROR。

## 各源真实口径(2026-09-03 逐源真调核实,非照文档假设)

同日同票 603161 / 2026-09-02 实调对照:

| 源 | 取数入口 | turnover 原值 | 口径 |
|---|---|---|---|
| baostock | `baostock_src.fetch_one`(字段 `turn`) | `2.7058` | 百分数 |
| sina | `akshare.stock_zh_a_daily` | `0.027058` | **小数** |
| tencent | `web.ifzq.gtimg.cn` fqkline | 该端点**不返回** amount/turnover | 缺列 |
| eastmoney | `akshare.stock_zh_a_hist`「换手率」 | 本机被 TLS 指纹墙,列义为% | 百分数 |
| akshare_spot | `akshare.stock_zh_a_spot_em`「换手率」 | 本机被墙,列义为% | 百分数 |
| tushare_daily | `daily_basic.turnover_rate` | 无 token 未实调 | 百分数 |

sina 之所以是小数:akshare 的 `stock_zh_a_daily` 内部是
`turnover = volume / outstanding_share`(**比值,不乘 100**)。这就是 100 倍差的根因。

## 自动检测(靠人扫不可靠,闸门必须是自动化断言)

`turnover ≈ volume / 流通股 × 100` ⇒ 同一票 `turnover / volume` 在流通股不变的区间里
**近似常数**。于是"某一行的 `turnover/volume` 相对邻域中位数骤降到 ~1/100"就是单位混用
的**可证据判据**(而不是"值小就可疑"——真实极低换手值的比值仍 ≈1,不会被误判)。

全量实证(2026-09-03,`data/master/kline/` 5552 只、129 万行有效比值)的 `rel` 分布:

    rel ∈ [0.005, 0.011]   666 行   ← 单位混用簇(≈1/100)
    rel ∈ (0.011, 0.30)      0 行   ← **完全空档**,判据阈值落在这里
    rel ∈ [0.30, +∞)     其余全部   ← 真值(≈1.0)

阈值 `_ANOMALY_REL` = 0.05 落在这条 ~30 倍宽的空档正中,两侧都有巨大余量。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("config.units")

# —— 口径常量 ——
PERCENT = "percent"      # 3.46 表示 3.46%
FRACTION = "fraction"    # 0.0346 表示 3.46%
ABSENT = "absent"        # 该源不提供 turnover(缺列,由 _normalize 补 NA)

#: master.turnover 的唯一口径
MASTER_TURNOVER_UNIT = PERCENT

#: 各采集源 turnover 的**原始**口径。新增源必须在此登记(见模块 docstring 的实调表)。
TURNOVER_UNIT_BY_SOURCE: dict[str, str] = {
    "baostock": PERCENT,
    "sina": FRACTION,          # akshare.stock_zh_a_daily = volume / outstanding_share
    "tencent": ABSENT,         # fqkline 端点不给 amount/turnover
    "tencent_hk": ABSENT,
    "eastmoney": PERCENT,
    "akshare_spot": PERCENT,
    "tushare_daily": PERCENT,
    "gtimg_quote": PERCENT,
    "index": ABSENT,           # 指数无换手率
}

# —— 单位混用自动检测参数(阈值依据见模块 docstring 的全量分布)——
_REL_WIN = 41          # rel 参考窗口(交易日,居中)
_REL_MIN_REF = 8       # 窗口内最少有效比值点;不足则不判(宁可漏报,不误报)
_ANOMALY_REL = 0.05    # rel < 此 → 判为单位异常(真值 rel ≥ 0.30,空档 ~6 倍)
_REPAIR_NEIGHBORS = 5  # 修复时取最近 N 个"未被标记"的比值点做参考
_REPAIR_TOL = 2.0      # ×100 后 rel 必须落进 [1/tol, tol] 才认为"×100 假设成立"


def turnover_unit(source: str | None) -> str | None:
    """返回该采集源 turnover 的原始口径;未登记返回 None。"""
    if not source:
        return None
    return TURNOVER_UNIT_BY_SOURCE.get(source)


def turnover_scale(source: str | None) -> float:
    """把该源的 turnover 归一到百分数所需的乘数。

    未登记的源按 1.0(维持现状、不放大风险),但记 ERROR 提醒登记——沉默地猜口径
    正是这个 bug 的成因。
    """
    unit = turnover_unit(source)
    if unit == FRACTION:
        return 100.0
    if unit is None and source:
        logger.error("采集源 %r 未在 units.TURNOVER_UNIT_BY_SOURCE 登记 turnover 口径,"
                     "按百分数原样透传;请补登记(单一真源:tools/config/units.py)", source)
    return 1.0


def to_percent(df, source: str | None):
    """把 df["turnover"] 按源口径归一到**百分数**(就地改列,返回同一 df)。

    这是**全项目唯一**给 turnover 乘 100 的地方。source=None(口径未知,如测试直接
    构造规整帧)时不动数据。
    """
    if df is None or "turnover" not in getattr(df, "columns", []):
        return df
    scale = turnover_scale(source)
    if scale != 1.0:
        import pandas as pd
        df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce") * scale
    return df


# ————————————————————————————————————————————————
# 单位混用自动检测 / 可证据修复
# ————————————————————————————————————————————————
def _rel_series(df):
    """逐行算 rel = (turnover/volume) / 邻域中位数(turnover/volume)。

    流通股不变的区间里 turnover/volume 是常数 ⇒ 真值行 rel ≈ 1.0,
    口径错(小数当百分数存)的行 rel ≈ 0.01。volume≤0 / turnover 缺失 → NaN(不判)。
    """
    import numpy as np
    import pandas as pd
    t = pd.to_numeric(df["turnover"], errors="coerce")
    v = pd.to_numeric(df["volume"], errors="coerce") if "volume" in df.columns else None
    if v is None:
        return pd.Series(np.nan, index=df.index)
    r = (t / v).replace([np.inf, -np.inf], np.nan)
    r = r.mask((v <= 0) | (t <= 0))
    loc = r.rolling(_REL_WIN, center=True, min_periods=_REL_MIN_REF).median()
    return (r / loc).replace([np.inf, -np.inf], np.nan)


def turnover_unit_anomaly_mask(df):
    """返回布尔 Series:该行 turnover 与本票 volume→turnover 映射不自洽(疑单位混用)。

    需要 `turnover` + `volume` 两列;缺列 / 参考点不足 → 全 False(不判)。
    """
    import pandas as pd
    cols = getattr(df, "columns", [])
    if df is None or len(df) == 0 or "turnover" not in cols or "volume" not in cols:
        return pd.Series(False, index=getattr(df, "index", []), dtype=bool)
    # 刻意不做"值小就可疑"的便宜预筛:实测低换手票(多年中位 0.55%)在换手放大的月份里,
    # 错行绝对值(0.036)相对**全局**中位数只小 18 倍,任何全局阈值都会漏掉它们
    # (漏 62 只 / 124 行)。判据必须落在**邻域**比值上,故直接算 rolling(全量 5552 只
    # 含 parquet 读盘也只 ~14s,单票一次调用 ms 级,不值得为此牺牲正确性)。
    rel = _rel_series(df)
    return (rel < _ANOMALY_REL).fillna(False)


def scan_turnover_unit(df, code: str | None = None) -> dict:
    """扫描一张 K 线表的 turnover 单位异常。返回 {rows, dates}(dates 为 YYYY-MM-DD 列表)。"""
    import pandas as pd
    mask = turnover_unit_anomaly_mask(df)
    n = int(mask.sum())
    dates: list[str] = []
    if n and "date" in df.columns:
        dates = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in df.loc[mask, "date"]]
    return {"rows": n, "dates": dates, "code": code}


def repair_turnover_unit(df, *, blank_unresolved: bool = True) -> tuple[object, dict]:
    """把被判为「小数当百分数存」的行 ×100 修回百分数(返回 (新 df, 报告))。

    **只改能被证明的行**:先用 `turnover_unit_anomaly_mask` 标记不自洽行;再对每个标记行
    取两个**互相独立**的参考比值——①时间上最近的 `_REPAIR_NEIGHBORS` 个**未被标记**比值点
    的中位数;②最近的**单个**未被标记比值点(流通股是阶跃变化的,解禁日附近中位数会跨越
    两个流通股水平,单点更贴近)——只要任一参考能让 `turnover×100 / volume / r_ref` 落进
    `[1/_REPAIR_TOL, _REPAIR_TOL]`(即「×100」这一假设确实把该行拉回本票自身的
    volume→turnover 映射),就改;两个都解释不通 → **不改**,计入 refused。

    **对「无法区分真假的中间值」的处理**:判据不是「值小」而是「与本票 volume 不自洽」。
    真实的极低换手行(如 603161/2019-11-21 的 0.4976%,已用 baostock 交叉核实为真值)
    rel ≈ 1.0,**从一开始就不会被标记**,所以中间值不存在被误纠的可能。

    **refused 行怎么办**(`blank_unresolved=True`,默认):置 NaN。这类行**已被证明**与本票
    自身的量-换手映射矛盾(实测 3 行,都是「错行恰好落在解禁日、流通股同日阶跃」),值一定
    是错的,只是修正倍数无法自证。留着它 = 下游看到「换手 0.012%」的极端伪信号(反转低换手
    策略正好吃这个);置 NaN = 诚实标记「未知」,交给已上线的覆盖率熔断 + 现算兜底处理。
    传 `blank_unresolved=False` 则原样保留、只报告不动数据。
    """
    import numpy as np
    import pandas as pd
    out = df.copy()
    mask = turnover_unit_anomaly_mask(out)
    rep = {"repaired": 0, "refused": 0, "dates": [], "refused_dates": []}
    if not mask.any():
        return out, rep

    t = pd.to_numeric(out["turnover"], errors="coerce")
    v = pd.to_numeric(out["volume"], errors="coerce")
    r = (t / v).replace([np.inf, -np.inf], np.nan).mask((v <= 0) | (t <= 0))
    clean = r.mask(mask).dropna()             # 未被标记的比值点(参考池)
    pos = {ix: i for i, ix in enumerate(out.index)}
    clean_pos = np.array([pos[ix] for ix in clean.index])
    clean_val = clean.to_numpy()
    lo, hi = 1.0 / _REPAIR_TOL, _REPAIR_TOL

    def _label(ix):
        return (pd.Timestamp(out.at[ix, "date"]).strftime("%Y-%m-%d")
                if "date" in out.columns else str(ix))

    for ix in out.index[mask]:
        order = np.argsort(np.abs(clean_pos - pos[ix])) if len(clean_pos) else []
        refs = []
        if len(order):
            refs.append(float(np.median(clean_val[order[:_REPAIR_NEIGHBORS]])))
            refs.append(float(clean_val[order[0]]))
        ok = any(r_ref and pd.notna(r[ix]) and lo <= (r[ix] * 100.0 / r_ref) <= hi
                 for r_ref in refs)
        if ok:
            out.at[ix, "turnover"] = float(t[ix]) * 100.0
            rep["repaired"] += 1
            rep["dates"].append(_label(ix))
        else:
            if blank_unresolved:
                out.at[ix, "turnover"] = np.nan
            rep["refused"] += 1
            rep["refused_dates"].append(_label(ix))
    return out, rep
