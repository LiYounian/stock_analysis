"""渲染双轨 · 六维 · T+1 口径的 docs/策略成绩报告.md。

列名一律**显式写清口径**:基准是哪天(T+1 入场)、期末 vs 期内触及、触及是"任意触及即算(宽松)"。
两条评测轨分开报:live 观测轨(线上落盘,薄样本)/ replay 回放轨(历史复现,长样本)。
"""
from __future__ import annotations

from .schema import DIRECTIONAL, RANKABLE, REFERENCE

TOPN_LEVELS = (5, 10, 20)


def _fmt(v, dash="—"):
    return dash if v is None else v


def _ci(pair):
    if not pair or pair[0] is None:
        return "—"
    return f"[{pair[0]}, {pair[1]}]"


# ────────────────────── 列名说明 ──────────────────────
def legend() -> list[str]:
    return [
        "## 〇、怎么读这张报告(先读这里)", "",
        "> 一句话:某策略**信号日 T 收盘后**选出的票,现实**最早 T+1 才能买入**。本报告对每票用 **两个并列基准** "
        "回答两个不同问题:", "",
        "> - **命中 / 触及基准 = 预测日 T 收盘 close[T]**:衡量**预测本身对不对**(从预测那一刻算方向站住没),"
        "与能不能买到无关。",
        "> - **收益基准 = T+1 入场价(默认开盘 open[T+1];无开盘价则用 T+1 收盘 close[T+1],单元格标注)**:"
        "衡量**可交易收益**(现实最早次日开盘才买得到)。",
        "> - 二者**答不同问题、并列报、别混**:命中率=**方向准确率**(基准 close[T]),不是收益、更不是 alpha;"
        "均收益 / 超额 / 盈亏比 / 分布等收益维度全部基于 **T+1 入场基准**。", "",
        "### 两条评测轨(分开报,不混)", "",
        "| 轨 | 数据源 | 答什么 | 样本 |",
        "|---|---|---|---|",
        "| **live 观测轨** | `data/analysis/<日期>/` 各策略当天**实际落盘**的预测 | 线上系统跑对没 | 上线仅约 14 交易日,**薄** |",
        "| **replay 回放轨** | 本地 kline 历史**复现**确定性/纯技术策略的历史预测(复用各策略 `signal_at` 纯 as-of 筛选) | 策略信号本身的真实统计力 | 覆盖近5日/近1月/近1季/近1年及全史,**厚** |", "",
        "> **不可回放的策略**(策略0多专家含新闻/LLM/情绪、策略9最强选股含 Tushare 筹码)历史无外部数据快照,"
        "强行回放会引入未来函数/数据缺口 → **仅 live 观测、不进回放轨**,报告如实标注。", "",
        "### 双基准口径(命中看 close[T]、收益看 T+1 入场)", "",
        "> **命中口径**(衡量预测对不对,基准=预测日 T 收盘 close[T]):",
        "> - **期末命中** = sign(close[T+h] / close[T] − 1) == 预测方向(相对 close[T],非相对 T+1 入场)。",
        "> - **期内触及** = T 之后窗口 [T+1,T+h] 内**任意一天**越过 close[T](看多=最高价>close[T];看空=最低价<close[T])。", "",
        "> **收益口径**(衡量可交易收益,基准=T+1 入场价):",
        "> - **入场价 = T+1 入场价**(open[T+1] 默认;缺→close[T+1]);**实现收益 r_h = close[T+h] / 入场价 − 1**(退出点锚信号日 T+h)。",
        "> - **隔夜跳空 = 入场价 / close[T] − 1 单列**,**不算策略功劳**(信号日收盘到次日开盘的跳空是拿不到的)。",
        "> - 总收益 close[T+h]/close[T]−1 = (1+隔夜跳空)(1+r_h)−1,本报告只把 **r_h 记作策略成绩**。", "",
        "### 六维指标 · 列名逐条", "",
        "| 列 | 精确含义(口径写死) |",
        "|---|---|",
        "| **命中%(期末)** | **基准=close[T]**。**T→T+h 期末**(close[T+h]/**close[T]**−1)方向 = 预测方向 的占比。最严格,看\"h 天后真站住没\"(衡量预测对不对,非可交易收益) |",
        "| **命中%(期内触及·宽松)** | **基准=close[T]**。T 之后窗口 [T+1,T+h] 内**任意一天触及**预测方向即算(看多=区间最高价>**close[T]**;看空=区间最低价<**close[T]**)。门槛低,天然远高于期末 |",
        "| **均收益% / 中位数%** | 逐票 r_h 的均值 / 中位数(③收益质量,不是命中率;负=平均在跌) |",
        "| **胜率% / 盈亏比** | r_h>0 占比 / 赢家均盈÷|输家均亏|。**高命中也可能赢小输大**,故盈亏比与胜率并看 |",
        "| **P10 / P90%** | r_h 分布的 10/90 分位(尾部风险与弹性) |",
        "| **隔夜跳空均值%** | 入场价/close[T]−1 的均值,**单列剔除**、不计入策略成绩 |",
        "| **策略均收益%_组合日均 / 基准_全市场均收益%** | 每日等权组合 r_h、再跨日等权(**组合口径,与聚类显著性同单元**)/ 同预测日集合、同 h、同样只算已到期、同 T+1 口径的全市场等权 r_h 日均。注:③的『均收益%』是逐票池化(每票期望),此处是每日组合口径,二者问的问题不同 |",
        "| **超额收益%_vs全市场** | = 策略均收益_组合日均 − 全市场均收益(④幅度超额,>0 才叫跑赢大盘同期) |",
        "| **超额_聚类CI% / 聚类p值** | ⑤按**交易日聚类**(每日超额为独立单元)bootstrap 的 95% 区间与双边 p(H0:平均超额=0)。CI 跨 0 / p 大 = 不显著 |",
        "| **随机基准均收益% / 优于随机p值** | 同预测日、同持仓数,从全市场**随机重采样**组合的收益分布;p=随机≥策略的占比,**p 小才是显著优于随机** |",
        "| **命中率_聚类CI% / naiveWilson%** | 命中率的按日聚类 bootstrap 区间(诚实)/ 逐票 Wilson 区间(**高估独立性、偏窄,仅对照**) |",
        "| **rank-IC / ICIR**(可排序型专用) | 每日 rank_score 与未来 r_h 的截面 Spearman 相关=当日 IC;均值=mean-IC,ICIR=mean/std,配 t 检验 p。看『分数高的票未来收益是否也高』(截面单调性) |", "",
        "### 按策略类型分流(三类,评法不同)", "",
        "| 类型 | 谁 | 用哪套指标 |",
        "|---|---|---|",
        "| **广筛型**(布尔达标全上) | S02/S03/S04/最强9/形态(S01/箱体3 已因显著负下线仅存档) | **全部入选票等权** vs 市场基准:命中率+收益质量+超额+按日聚类显著性 |",
        "| **可排序型**(有连续打分) | 策略0合议(综合分)/4动量(动量分)/5半导体/10反转低换手(综合分)/SEPA趋势模板(RPS250) | 广筛全量指标 **＋ Top-N(5/10/20)精度 ＋ rank-IC/ICIR**——排序信息不浪费 |",
        "| **参考·非alpha**(伪排序) | 策略11 指标条件化状态排序 | 打分为**离散状态格、个股间无真实区分**(代码自认非alpha);**只列全量指标作参考,不计排序榜、不跑 rank-IC/Top-N** |", "",
        "> **Top-N 精度列**(可排序型专用,与全量指标同表并列对比):",
        "> - **Top{5,10,20}**:每预测日按该策略打分**降序**取前 N 只(某日不足 N 取当日全部)。",
        "> - **命中%(期末)/期望收益%(池化)**:选中 Top-N 票池化的期末命中率 / 逐票平均实现收益(基准仍=T+1 入场)。",
        "> - **期望收益%(组合日均)/超额%vs全市场/超额聚类p**:每日 Top-N 等权组合收益跨日等权,及其相对当日全市场等权的超额与按日聚类 p(与全量单元同口径,可直接对比)。",
        "> - **每日不足N%**:入选票不够 N 只的交易日占比(高=该策略每日出票少,Top-N 与全量趋同)。",
        "> - **怎么用**:若 Top5/10 的命中/超额**显著高于**同策略『全部票等权』,说明分数确有选择性(排序信息有用);若与全量趋同甚至更差,说明打分区分度弱。", "",
        "> **为什么策略11 归『参考·非alpha』而非跑 rank-IC**:其打分是离散状态分层(相似样本上涨概率%),"
        "个股间无连续区分、代码本身标注非 alpha;强跑 rank-IC/Top-N 会得『看着有排序其实是噪声』的误导结论,"
        "故只按广筛口径列全量指标供参考,不计入排序榜。", "",
        "> **样本 <30 仍薄**,但真正判据是 **CI 宽度 / p 值**,不再只靠 <30 硬阈值。历史观测≠未来保证。**非投资建议。**", "",
    ]


# ────────────────────── 表格渲染 ──────────────────────
def _dir_row(sid, e, h) -> str:
    c = e.get(f"{h}日", {})
    rq = c.get("收益质量", {})
    return (f"| {sid} {e['策略名']} | {c.get('已到期样本',0)} | {c.get('预测日数',0)} | "
            f"{_fmt(c.get('命中率%_期末'))} | {_fmt(c.get('命中率%_期内触及'))} | "
            f"{_fmt(rq.get('均值%'))} | {_fmt(rq.get('中位数%'))} | {_fmt(rq.get('盈亏比'))} | "
            f"{_fmt(rq.get('P10%'))}/{_fmt(rq.get('P90%'))} | {_fmt(c.get('隔夜跳空均值%'))} | "
            f"{_fmt(c.get('超额收益%_vs全市场'))} | {_ci(c.get('超额_聚类CI%'))} | "
            f"{_fmt(c.get('超额_聚类p值'))} | {_fmt(c.get('优于随机p值'))} | {_ci(c.get('命中率_聚类CI%'))} |")


def _dir_table(strat: dict, h: int, types) -> list[str]:
    """全量指标表(命中/收益质量/超额),渲染 types 里的策略(广筛型/可排序型/参考型共用同表结构)。"""
    rows = [f"**{h}日 horizon(命中/触及基准=close[T] · 收益基准=T+1 入场 → T+{h} 退出)**", "",
            "| 策略 | 已到期样本 | 预测日 | 命中%(期末) | 命中%(期内触及·宽松) | 均收益% | 中位% | 盈亏比 | "
            "P10/P90% | 隔夜跳空% | 超额%vs全市场 | 超额聚类CI% | 超额p | 优于随机p | 命中率聚类CI% |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    n = 0
    for sid, e in sorted(strat.items()):
        if e.get("类型") in types:
            rows.append(_dir_row(sid, e, h))
            n += 1
    return rows + [""] if n else []


def _topn_table(strat: dict, h: int) -> list[str]:
    """可排序型 Top-N(5/10/20)精度表:按打分降序取前 N 只,看'分数越高是否越准/越赚'。"""
    items = [(sid, e) for sid, e in sorted(strat.items()) if e.get("类型") == RANKABLE]
    # 仅当至少一个策略该 horizon 有 Top-N 数据才渲染。
    has = any(e.get(f"{h}日", {}).get("Top-N精度") for _sid, e in items)
    if not has:
        return []
    rows = [f"**{h}日 · Top-N 精度(按策略打分降序取前 N 只;vs 上表『全部票等权』看选择性)**", "",
            "| 策略 | 档位 | 选中样本 | 预测日 | 命中%(期末) | 期望收益%(池化) | 期望收益%(组合日均) | "
            "超额%vs全市场 | 超额聚类p | 每日不足N% |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    any_row = False
    for sid, e in items:
        cell = e.get(f"{h}日", {})
        topn = cell.get("Top-N精度") or {}
        if not topn:
            why = ("本档无已到期样本" if cell.get("已到期样本", 0) == 0
                   else "无连续打分字段,Top-N不适用")
            rows.append(f"| {sid} {e['策略名']} | — | — | — | — | — | — | — | — | — |"
                        f"  <!-- {why} -->")
            continue
        for N in TOPN_LEVELS:
            c = topn.get(N)
            if not c:
                continue
            any_row = True
            rows.append(f"| {sid} {e['策略名']} | Top{N} | {c.get('选中样本',0)} | {c.get('预测日数',0)} | "
                        f"{_fmt(c.get('命中率%_期末'))} | {_fmt(c.get('期望收益%_池化'))} | "
                        f"{_fmt(c.get('期望收益%_组合日均'))} | {_fmt(c.get('超额%_vs全市场'))} | "
                        f"{_fmt(c.get('超额_聚类p值'))} | {_fmt(c.get('每日不足N占比%'))} |")
    return rows + [""] if any_row else []


def _rank_table(strat: dict, horizons) -> list[str]:
    """可排序型 rank-IC / ICIR(参考型策略11 不入此表)。"""
    items = [(sid, e) for sid, e in sorted(strat.items()) if e.get("类型") == RANKABLE]
    if not items:
        return []
    rows = ["**可排序型 rank-IC / ICIR(截面单调性;不套方向命中;参考·非alpha型不计入)**", "",
            "| 策略 | " + " | ".join(f"{h}日 mean-IC / ICIR / p / 天数" for h in horizons) + " |",
            "|---|" + "|".join("---" for _ in horizons) + "|"]
    any_row = False
    for sid, e in items:
        cells = []
        for h in horizons:
            ic = e.get(f"{h}日", {}).get("rank_ic", {}) or {}
            if ic.get("mean_ic") is not None:
                any_row = True
            cells.append(f"{_fmt(ic.get('mean_ic'))} / {_fmt(ic.get('icir'))} / "
                         f"{_fmt(ic.get('p_value'))} / {_fmt(ic.get('n_days'),0)}")
        rows.append(f"| {sid} {e['策略名']} | " + " | ".join(cells) + " |")
    return rows + [""] if any_row else []


def _window_block(wname: str, w: dict, horizons) -> list[str]:
    tag = "" if w.get("数据充足") else " ⚠️数据不足"
    lines = [f"### {wname}(N={_fmt(w.get('窗口交易日数N'),'全部')} 交易日{tag})", "",
             f"> {w.get('说明','')}", ""]
    strat = w.get("策略", {})
    if not strat:
        return lines + ["该窗内暂无已到期样本。", ""]
    has_dir = any(e.get("类型") == DIRECTIONAL for e in strat.values())
    has_rankable = any(e.get("类型") == RANKABLE for e in strat.values())
    has_ref = any(e.get("类型") == REFERENCE for e in strat.values())

    if has_dir:
        lines += ["#### 广筛型(布尔达标·全部票等权 vs 市场基准)", ""]
        for h in horizons:
            lines += _dir_table(strat, h, {DIRECTIONAL})
    if has_rankable:
        lines += ["#### 可排序型(有连续打分:全量指标 + Top-N 精度 + rank-IC)", "",
                  "> 全量指标同广筛口径(全部入选票等权);Top-N 表按各策略打分降序取前 5/10/20 只,"
                  "与全量对比即看『排序信息有没有用』——若 Top-N 命中/超额显著高于全量,说明分数确有选择性。", ""]
        for h in horizons:
            lines += _dir_table(strat, h, {RANKABLE})
            lines += _topn_table(strat, h)
        lines += _rank_table(strat, horizons)
    if has_ref:
        lines += ["#### 参考·非alpha(伪排序:离散状态格、个股间无真实区分)", "",
                  "> 打分为离散状态分层、代码自认非 alpha;**不计入排序榜、不跑 rank-IC/Top-N**"
                  "(强跑会得『看着有排序其实是噪声』的误导结论)。方向多为中性 → 命中率不适用(—);"
                  "另附『全部已到期票』收益分布作纯参考。", ""]
        for h in horizons:
            lines += _dir_table(strat, h, {REFERENCE})
        lines += _ref_return_table(strat, horizons)
    return lines


def _ref_return_table(strat: dict, horizons) -> list[str]:
    """参考·非alpha 的收益分布(不问方向,纯参考,非 alpha 判据)。"""
    items = [(sid, e) for sid, e in sorted(strat.items()) if e.get("类型") == REFERENCE]
    if not items:
        return []
    rows = ["**参考收益分布(全部已到期票 · 不问方向 · 纯参考非alpha)**", "",
            "| 策略 | " + " | ".join(
                f"{h}日 样本/均值%/中位%/胜率%/P10P90" for h in horizons) + " |",
            "|---|" + "|".join("---" for _ in horizons) + "|"]
    for sid, e in items:
        cells = []
        for h in horizons:
            c = e.get(f"{h}日", {})
            rq = c.get("参考收益分布_全部已到期", {}) or {}
            cells.append(f"{c.get('参考已到期样本',0)} / {_fmt(rq.get('均值%'))} / "
                         f"{_fmt(rq.get('中位数%'))} / {_fmt(rq.get('胜率%'))} / "
                         f"{_fmt(rq.get('P10%'))}~{_fmt(rq.get('P90%'))}")
        rows.append(f"| {sid} {e['策略名']} | " + " | ".join(cells) + " |")
    return rows + [""]


def _track(agg: dict, title: str, horizons, note: str = "") -> list[str]:
    lines = [f"## {title}", ""]
    if note:
        lines += [f"> {note}", ""]
    wins = agg.get("窗口", {})
    if not wins:
        return lines + ["(无数据)", ""]
    # 全史优先展示,再各滚动窗。
    order = ["全史", "近一周", "近一月", "近一季", "近一年"]
    for wname in order:
        if wname in wins:
            lines += _window_block(wname, wins[wname], horizons)
    return lines


# ────────────────────── 差生名单 ──────────────────────
def flag_laggards(replay_agg: dict, live_agg: dict, horizons=(1, 5)) -> list[dict]:
    """T+1 口径下差生:回放全史 5日超额<0 且聚类 p<0.1(方向型),或 live 也弱作佐证。

    以**回放轨全史**为主判据(样本厚、有显著性);live 仅佐证。排序型看 IC≤0。
    """
    out = []
    rwins = replay_agg.get("窗口", {}).get("全史", {}).get("策略", {})
    lwins = live_agg.get("窗口", {}).get("全史", {}).get("策略", {})
    for sid, e in rwins.items():
        # 广筛型与可排序型均有全量超额指标可判;参考·非alpha(策略11)不判死。
        if e.get("类型") not in (DIRECTIONAL, RANKABLE):
            continue
        c5 = e.get("5日", {})
        ex = c5.get("超额收益%_vs全市场")
        p = c5.get("超额_聚类p值")
        n = c5.get("已到期样本", 0)
        if ex is None or n < 30:
            continue
        weak = ex < 0
        sig = p is not None and p < 0.10
        live5 = lwins.get(sid, {}).get("5日", {})
        out.append({"strategy_id": sid, "策略名": e["策略名"], "回放5日超额%": ex,
                    "回放超额p": p, "回放样本": n, "显著负": bool(weak and sig),
                    "live5日超额%": live5.get("超额收益%_vs全市场"),
                    "live5日命中%": live5.get("命中率%_期末")})
    return sorted(out, key=lambda x: (not x["显著负"], x["回放5日超额%"]))


def _laggard_section(lag: list[dict]) -> list[str]:
    lines = ["## 三、差生名单(T+1 口径 · 以回放轨全史为主判据)", "",
             "> 判据:回放全史 **5日超额<0** 且**按日聚类 p<0.1(显著负)**为坐实;仅超额<0 但不显著 → 提示不判死。"
             "live 超额/命中作佐证(样本薄)。排序型见 rank-IC 表(IC≤0 即无 edge)。", ""]
    if not lag:
        return lines + ["回放轨暂无坐实差生。", ""]
    lines += ["| 策略 | 回放5日超额% | 回放超额聚类p | 回放样本 | 显著负? | live5日超额% | live5日命中% |",
              "|---|---|---|---|---|---|---|"]
    for l in lag:
        lines.append(f"| {l['strategy_id']} {l['策略名']} | {l['回放5日超额%']} | "
                     f"{_fmt(l['回放超额p'])} | {l['回放样本']} | {'是' if l['显著负'] else '否(提示)'} | "
                     f"{_fmt(l['live5日超额%'])} | {_fmt(l['live5日命中%'])} |")
    return lines + [""]


# ────────────────────── 自审 + 顶层 ──────────────────────
def _self_audit(replay_meta: dict, done: str, undone: str, assumptions: list[str]) -> list[str]:
    lines = ["## 四、自审要点", "",
             "- **① 防未来函数**:回放复用各策略 `signal_at(kdf,t)` 纯 as-of 筛选(只用 ≤t 数据,等价性由各 backtest_* 单测锁死);"
             "基准/bootstrap 与策略侧**同一 T+1 口径、同样只算已到期**,不用未来价选票。",
             "- **② 双基准,命中≠收益**:**命中/触及基准=close[T]**(期末=sign(close[T+h]/close[T]−1)==方向;期内触及=[T+1,T+h] 任意日越过 close[T]),"
             "衡量预测对不对;**收益基准=T+1 入场价**(入场=open[T+1],缺→close[T+1] 并标注;退出=close[T+h];r_h 分母=入场价),衡量可交易收益;"
             "隔夜跳空 = 入场价/close[T]−1 **单列剔除**,不计策略成绩。两基准并列、绝不串。",
             "- **③ 双轨不混**:live(线上落盘)与 replay(历史复现)分节渲染、source 字段区分,绝不合并统计。",
             "- **④ 显著性按日聚类**:独立单元=**交易日批次**(同日选票高度相关),bootstrap 对天重采样;"
             "Wilson 为逐票 naive 仅作对照(会高估独立性、区间偏窄)。",
             "- **⑤ 按类型分流**:广筛型(S02/S03/S04/最强/形态;S01/箱体3 已因显著负下线仅存档)全部票等权评;可排序型(策略0/4/5/10/SEPA)"
             "追加按打分降序的 Top-N(5/10/20)精度 + 截面 rank-IC/ICIR;**策略11 重归『参考·非alpha』**——其打分为离散状态格、"
             "个股间无真实区分,**移出排序型 rank-IC、不跑 Top-N**,仅按广筛口径列全量指标作参考(纠正 v3 旧版把它当排序型的口径错配)。",
             "- **⑥ 列名口径写死**:期末 vs 期内触及、触及=任意触及即算(宽松)、基准=同预测日 T+1 全市场等权、"
             "Top-N=按打分降序取前 N 只(vs 全量等权看选择性),均在列名说明写明。",
             f"- **回放元信息**:{replay_meta}", "",
             "## 五、完成情况与假设", "",
             f"- **已完成维度**:{done}",
             f"- **未完成/降级**:{undone}"]
    for a in assumptions:
        lines.append(f"- **假设**:{a}")
    return lines + [""]


def render(live_agg: dict, replay_agg: dict, replay_meta: dict, generated: str,
           horizons=(1, 5), laggards=None, done="", undone="", assumptions=None) -> str:
    lines = ["# 全策略成绩报告 v3(双轨 · 六维 · 双基准:命中=close[T] / 收益=T+1入场)", "",
             f"> 生成于 {generated}。历史观测≠未来保证;命中率=**方向准确率**(非 alpha)。**非投资建议。**", ""]
    lines += legend()
    lines += _track(live_agg, "一、live 观测轨(线上实际落盘 · 上线以来)", horizons,
                    "验线上系统跑对没;上线仅约 14 交易日,长窗一律数据不足,薄样本以 CI/p 判读。")
    lines += _track(replay_agg, "二、replay 回放回测轨(本地历史复现 · 长样本)", horizons,
                    "对确定性/纯技术策略复现历史预测,给真实统计力;不可回放策略不在此。")
    lines += _laggard_section(laggards or [])
    lines += _self_audit(replay_meta, done, undone, assumptions or [])
    return "\n".join(lines) + "\n"
