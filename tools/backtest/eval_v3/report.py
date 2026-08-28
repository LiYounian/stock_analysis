"""渲染双轨 · 六维 · T+1 口径的 docs/策略成绩报告.md。

列名一律**显式写清口径**:基准是哪天(T+1 入场)、期末 vs 期内触及、触及是"任意触及即算(宽松)"。
两条评测轨分开报:live 观测轨(线上落盘,薄样本)/ replay 回放轨(历史复现,长样本)。
"""
from __future__ import annotations

from .schema import DIRECTIONAL, RANKING


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
        "> 一句话:某策略**信号日 T 收盘后**选出的票,现实**最早 T+1 才能买入**,本报告统一按 "
        "**T+1 入场价(默认 T+1 开盘 open[T+1];无开盘价则用 T+1 收盘 close[T+1],单元格标注)** "
        "重算它往后 1 / 5 个交易日的**方向命中、收益质量、超额、显著性**。**命中率=方向准确率,不是收益、更不是 alpha。**", "",
        "### 两条评测轨(分开报,不混)", "",
        "| 轨 | 数据源 | 答什么 | 样本 |",
        "|---|---|---|---|",
        "| **live 观测轨** | `data/analysis/<日期>/` 各策略当天**实际落盘**的预测 | 线上系统跑对没 | 上线仅约 14 交易日,**薄** |",
        "| **replay 回放轨** | 本地 kline 历史**复现**确定性/纯技术策略的历史预测(复用各策略 `signal_at` 纯 as-of 筛选) | 策略信号本身的真实统计力 | 覆盖近5日/近1月/近1季/近1年及全史,**厚** |", "",
        "> **不可回放的策略**(策略0多专家含新闻/LLM/情绪、策略9最强选股含 Tushare 筹码)历史无外部数据快照,"
        "强行回放会引入未来函数/数据缺口 → **仅 live 观测、不进回放轨**,报告如实标注。", "",
        "### T+1 入场口径(会动所有旧数字)", "",
        "> 旧口径用信号日收盘 close[T] 当入场价(现实买不到)。新口径:",
        "> - **入场价 = T+1 入场价**(open[T+1] 默认);**实现收益 r_h = close[T+h] / 入场价 − 1**(退出点仍锚信号日 T+h)。",
        "> - **隔夜跳空 = 入场价 / close[T] − 1 单列**,**不算策略功劳**(信号日收盘到次日开盘的跳空是拿不到的)。",
        "> - 总收益 close[T+h]/close[T]−1 = (1+隔夜跳空)(1+r_h)−1,本报告只把 **r_h 记作策略成绩**。", "",
        "### 六维指标 · 列名逐条", "",
        "| 列 | 精确含义(口径写死) |",
        "|---|---|",
        "| **命中%(期末)** | T+1 入场后,**T→T+h 期末**(close[T+h]/入场价−1)方向 = 预测方向 的占比。最严格,看\"h 天后真站住没\" |",
        "| **命中%(期内触及·宽松)** | T+1 入场后窗口 [T+1,T+h] 内**任意一天触及**预测方向即算(看多=区间最高价>入场价)。门槛低,天然远高于期末 |",
        "| **均收益% / 中位数%** | 逐票 r_h 的均值 / 中位数(③收益质量,不是命中率;负=平均在跌) |",
        "| **胜率% / 盈亏比** | r_h>0 占比 / 赢家均盈÷|输家均亏|。**高命中也可能赢小输大**,故盈亏比与胜率并看 |",
        "| **P10 / P90%** | r_h 分布的 10/90 分位(尾部风险与弹性) |",
        "| **隔夜跳空均值%** | 入场价/close[T]−1 的均值,**单列剔除**、不计入策略成绩 |",
        "| **策略均收益%_组合日均 / 基准_全市场均收益%** | 每日等权组合 r_h、再跨日等权(**组合口径,与聚类显著性同单元**)/ 同预测日集合、同 h、同样只算已到期、同 T+1 口径的全市场等权 r_h 日均。注:③的『均收益%』是逐票池化(每票期望),此处是每日组合口径,二者问的问题不同 |",
        "| **超额收益%_vs全市场** | = 策略均收益_组合日均 − 全市场均收益(④幅度超额,>0 才叫跑赢大盘同期) |",
        "| **超额_聚类CI% / 聚类p值** | ⑤按**交易日聚类**(每日超额为独立单元)bootstrap 的 95% 区间与双边 p(H0:平均超额=0)。CI 跨 0 / p 大 = 不显著 |",
        "| **随机基准均收益% / 优于随机p值** | 同预测日、同持仓数,从全市场**随机重采样**组合的收益分布;p=随机≥策略的占比,**p 小才是显著优于随机** |",
        "| **命中率_聚类CI% / naiveWilson%** | 命中率的按日聚类 bootstrap 区间(诚实)/ 逐票 Wilson 区间(**高估独立性、偏窄,仅对照**) |",
        "| **rank-IC / ICIR**(排序型专用) | 每日 rank_score 与未来 r_h 的截面 Spearman 相关=当日 IC;均值=mean-IC,ICIR=mean/std,配 t 检验 p。**排序型不套方向命中** |", "",
        "> **为什么排序型走 rank-IC**:策略11(指标条件化)天然中性、方向命中口径下样本≈0 是**口径错配**;"
        "排序型看的是\"分数高的票是否未来收益也高\"(截面单调性),用 IC 而非命中率。", "",
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


def _dir_table(strat: dict, h: int) -> list[str]:
    rows = [f"**{h}日 horizon(T+1 入场 → T+{h} 期末)**", "",
            "| 策略 | 已到期样本 | 预测日 | 命中%(期末) | 命中%(期内触及·宽松) | 均收益% | 中位% | 盈亏比 | "
            "P10/P90% | 隔夜跳空% | 超额%vs全市场 | 超额聚类CI% | 超额p | 优于随机p | 命中率聚类CI% |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    n = 0
    for sid, e in sorted(strat.items()):
        if e.get("类型") == DIRECTIONAL:
            rows.append(_dir_row(sid, e, h))
            n += 1
    return rows + [""] if n else []


def _rank_table(strat: dict, horizons) -> list[str]:
    items = [(sid, e) for sid, e in sorted(strat.items()) if e.get("类型") == RANKING]
    if not items:
        return []
    rows = ["**排序型(rank-IC / ICIR;不套方向命中)**", "",
            "| 策略 | " + " | ".join(f"{h}日 mean-IC / ICIR / p / 天数" for h in horizons) + " |",
            "|---|" + "|".join("---" for _ in horizons) + "|"]
    for sid, e in items:
        cells = []
        for h in horizons:
            c = e.get(f"{h}日", {})
            cells.append(f"{_fmt(c.get('mean_ic'))} / {_fmt(c.get('icir'))} / "
                         f"{_fmt(c.get('p_value'))} / {_fmt(c.get('n_days'),0)}")
        rows.append(f"| {sid} {e['策略名']} | " + " | ".join(cells) + " |")
    return rows + [""]


def _window_block(wname: str, w: dict, horizons) -> list[str]:
    tag = "" if w.get("数据充足") else " ⚠️数据不足"
    lines = [f"### {wname}(N={_fmt(w.get('窗口交易日数N'),'全部')} 交易日{tag})", "",
             f"> {w.get('说明','')}", ""]
    strat = w.get("策略", {})
    if not strat:
        return lines + ["该窗内暂无已到期样本。", ""]
    for h in horizons:
        lines += _dir_table(strat, h)
    lines += _rank_table(strat, horizons)
    return lines


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
        if e.get("类型") != DIRECTIONAL:
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
             "- **② T+1 口径全链一致**:入场=open[T+1](缺→close[T+1] 并标注),退出=close[T+h],分母全改 T+1 入场价;"
             "隔夜跳空 = 入场价/close[T]−1 **单列剔除**,不计策略成绩。",
             "- **③ 双轨不混**:live(线上落盘)与 replay(历史复现)分节渲染、source 字段区分,绝不合并统计。",
             "- **④ 显著性按日聚类**:独立单元=**交易日批次**(同日选票高度相关),bootstrap 对天重采样;"
             "Wilson 为逐票 naive 仅作对照(会高估独立性、区间偏窄)。",
             "- **⑤ 排序型用 rank-IC**:策略0/10/11 走截面 Spearman IC/ICIR,不套方向命中(避免口径错配致样本≈0)。",
             "- **⑥ 列名口径写死**:期末 vs 期内触及、触及=任意触及即算(宽松)、基准=同预测日 T+1 全市场等权,均在列名说明写明。",
             f"- **回放元信息**:{replay_meta}", "",
             "## 五、完成情况与假设", "",
             f"- **已完成维度**:{done}",
             f"- **未完成/降级**:{undone}"]
    for a in assumptions:
        lines.append(f"- **假设**:{a}")
    return lines + [""]


def render(live_agg: dict, replay_agg: dict, replay_meta: dict, generated: str,
           horizons=(1, 5), laggards=None, done="", undone="", assumptions=None) -> str:
    lines = ["# 全策略成绩报告 v3(双轨 · 六维 · T+1 入场口径)", "",
             f"> 生成于 {generated}。历史观测≠未来保证;命中率=**方向准确率**(非 alpha)。**非投资建议。**", ""]
    lines += legend()
    lines += _track(live_agg, "一、live 观测轨(线上实际落盘 · 上线以来)", horizons,
                    "验线上系统跑对没;上线仅约 14 交易日,长窗一律数据不足,薄样本以 CI/p 判读。")
    lines += _track(replay_agg, "二、replay 回放回测轨(本地历史复现 · 长样本)", horizons,
                    "对确定性/纯技术策略复现历史预测,给真实统计力;不可回放策略不在此。")
    lines += _laggard_section(laggards or [])
    lines += _self_audit(replay_meta, done, undone, assumptions or [])
    return "\n".join(lines) + "\n"
