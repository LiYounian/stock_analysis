"""Web 数据访问层:经 store 只读 data/analysis / data/raw(按日期)。

Web 不做计算、不触网,只读离线 run.py 产出的数据。store 按日期分区存储,
本层把"要看哪一天"(date)透传给 store.get_*(date=...);date 缺省 "latest"=最新日期。
展示层只依赖 config + store(基座只读层),不 import 分析器。
"""
from __future__ import annotations

from tools.store import repo as store


def available_dates() -> list[str]:
    """所有可选分析日期,倒序(最新在前),供页面日期下拉。"""
    return list(reversed(store.list_dates("analysis")))


def as_of(date: str = "latest") -> str:
    """当前展示的数据日期(具体日期直接回显;latest / 非法 → 最新)。"""
    dates = store.list_dates("analysis")
    if date and date != "latest" and date in dates:
        return date
    return dates[-1] if dates else "-"


def _load_all(date: str = "latest") -> dict[str, dict]:
    """某日期(缺省最新)下全部个股中心记录 {code: rec}。"""
    return {r["meta"]["code"]: r for r in store.iter_records(date=date)}


def list_records(date: str = "latest") -> list[dict]:
    """全池记录,按趋势得分降序。"""
    recs = list(_load_all(date).values())
    recs.sort(key=lambda r: ((r.get("signals") or {}).get("trend") or {}).get("得分", -999),
              reverse=True)
    return recs


def get_record(code: str, date: str = "latest") -> dict | None:
    try:
        return store.get_record(code, date=date)
    except FileNotFoundError:
        return None


def get_kline(code: str, date: str = "latest") -> dict:
    """读预生成的 K线图表视图(analysis/<日期>/chart)。展示层只读、不算(§9.3)。"""
    try:
        return store.get_code_view("chart", code, date=date)
    except FileNotFoundError:
        return {"dates": [], "open": [], "high": [], "low": [], "close": [],
                "ma5": [], "ma20": [], "ma60": [], "volume": []}


# ————————————————————————————————————————————————
# 名称回退:自选记录 meta.name → config/code_name.json[code] → code
# code_name.json 是全A「代码→名称」映射(offline 产出),模块级只加载一次;
# web 不触网。文件缺失/损坏 → 空 dict,优雅退回「只用中心记录、再退 code」,不报错。
# ————————————————————————————————————————————————
_CODE_NAME_CACHE: dict[str, str] | None = None


def _code_name_map() -> dict[str, str]:
    """全A代码→名称映射(config/code_name.json),模块级只加载一次。文件缺失/损坏 → 空 dict。"""
    global _CODE_NAME_CACHE
    if _CODE_NAME_CACHE is None:
        try:
            import json
            from tools.config import settings
            path = settings.PROJECT_ROOT / "config" / "code_name.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            _CODE_NAME_CACHE = data if isinstance(data, dict) else {}
        except (FileNotFoundError, ValueError, OSError):
            _CODE_NAME_CACHE = {}
    return _CODE_NAME_CACHE


def _resolve_name(rec, code):
    """单记录版名称回退:rec.meta.name → code_name.json[code] → code。"""
    nm = ((rec or {}).get("meta") or {}).get("name")
    if nm:
        return nm
    return _code_name_map().get(code) or code


def _name(recs, code):
    """名称回退链(全页统一入口):自选记录 meta.name → code_name.json → code。"""
    return _resolve_name((recs or {}).get(code), code)


def max_range_page(date: str = "latest") -> dict:
    """最大范围选股专页：当日 view + 所有真实落盘日期的广度序列。"""
    view_name = "最大范围选股"
    target = as_of(date)
    try:
        view = store.get_view(view_name, date=target)
    except FileNotFoundError:
        view = None

    history = []
    for day in store.list_dates("analysis"):
        try:
            item = store.get_view(view_name, date=day)
        except FileNotFoundError:
            continue
        if isinstance(item, dict) and isinstance(item.get("占比%"), (int, float)):
            history.append({"date": day, "ratio": item["占比%"],
                            "selected": item.get("入选数", 0), "eligible": item.get("有效样本", 0)})

    recs = _load_all(target)
    rows = []
    for item in (view or {}).get("入选清单", []) or []:
        code = item.get("code")
        if not code:
            continue
        d = item.get("明细") or {}
        rows.append({"code": code, "name": _name(recs, code), "close": d.get("close"),
                     "high_ratio": d.get("距250日高点%"), "surge_count": d.get("32日涨超6%次数"),
                     "retrace": d.get("当日回撤%"), "ma": d.get("MA") or {}})
    return {"present": view is not None, "as_of": target, "view": view or {},
            "rows": rows, "history": history}

def strong_page(date: str = "latest") -> dict:
    target=as_of(date)
    try: view=store.get_view("最强选股",date=target)
    except FileNotFoundError: view=None
    rows=[]
    for x in (view or {}).get("入选清单",[]):
        d=x.get("明细",{}); rows.append({"code":x["code"],"name":_name(_load_all(target),x["code"]),**d})
    dates=[]; history=[]
    for day in store.list_dates("analysis"):
        try:
            item=store.get_view("最强选股",date=day)
            if item:
                dates.append(day); history.append({"date":day,"ratio":item.get("占比%",0),"selected":item.get("入选数",0),"eligible":item.get("有效样本",0)})
        except FileNotFoundError: pass
    return {"present":view is not None,"as_of":target,"view":view or {},"rows":rows,"dates":dates,"history":history}

def strategy_page(view_name: str, date: str = "latest") -> dict:
    """通用策略页数据，新增日线策略只需传 view 名称。"""
    target=as_of(date)
    try: view=store.get_view(view_name,date=target)
    except FileNotFoundError: view=None
    rows=[]
    for x in (view or {}).get("入选清单",[]):
        d=x.get("明细",{}); rows.append({"code":x["code"],"name":_name(_load_all(target),x["code"]),**d})
    history=[]
    for day in store.list_dates("analysis"):
        try:
            v=store.get_view(view_name,date=day); history.append({"date":day,"ratio":v.get("占比%",0),"selected":v.get("入选数",0),"eligible":v.get("有效样本",0)})
        except FileNotFoundError: pass
    return {"present":view is not None,"as_of":target,"view":view or {},"rows":rows,"history":history,"dates":[x["date"] for x in history]}


STRATEGY_CATALOG = {
    "max-range": {"name": "最大范围选股", "label": "最大范围选股"},
    "strong": {"name": "最强选股", "label": "最强选股"},
    "rub": {"name": "拉揉搓", "label": "拉揉搓"},
    "single-volume": {"name": "单日放量", "label": "单日放量"},
    "low-volume": {"name": "低位单日放量", "label": "低位单日放量"},
    "continuous-volume": {"name": "连续放量", "label": "连续放量"},
}


def multi_strategy_page(keys: list[str], date: str = "latest") -> dict:
    """同一交易日多个策略入选清单的交集；仅使用已经落盘的回放结果。"""
    target = as_of(date)
    selected = [(k, STRATEGY_CATALOG[k]) for k in dict.fromkeys(keys) if k in STRATEGY_CATALOG]
    views, code_sets = [], []
    for key, info in selected:
        try:
            view = store.get_view(info["name"], date=target)
        except FileNotFoundError:
            continue
        codes = {x.get("code") for x in (view.get("入选清单") or []) if x.get("code")}
        views.append({"key": key, "label": info["label"], "selected": len(codes), "eligible": view.get("有效样本", 0)})
        code_sets.append(codes)
    common = set.intersection(*code_sets) if code_sets else set()
    recs = _load_all(target)
    return {"as_of": target, "keys": [x[0] for x in selected], "views": views,
            "rows": [{"code": c, "name": _name(recs, c)} for c in sorted(common)],
            "first_strategy": selected[0][0] if selected else None}


def _pool_codes() -> set[str]:
    """当前自选池代码集合(区块①「自选股」过滤用)。读失败 → 空集合(区块① 空,不炸页)。"""
    try:
        from tools.config import stock_pool
        return set(stock_pool.get_codes())
    except Exception:
        return set()


def council_summary(rec: dict) -> dict | None:
    """从中心记录抽合议默认组的摘要 {综合方向, 综合分, 是否冲突};无 council 块返回 None(向后兼容旧数据)。"""
    c = (rec or {}).get("council") or {}
    d = c.get("default")
    if not isinstance(d, dict):
        return None
    return {"综合方向": d.get("综合方向"), "综合分": d.get("综合分", 0.0),
            "是否冲突": bool(d.get("是否冲突"))}


def stops_view(rec: dict) -> dict:
    """从中心记录抽 5 日止盈止损 + 上涨概率,供选股页/首页榜单 L1 展示。

    防空口径(同 dashboard.html / stock.html):prediction 缺失或为 error 块(次新股 K线<30)时,
    全字段返回 None → 前端渲染「—」,绝不抛 UndefinedError。字段口径同个股页(predict.py 产出)。
    上涨概率% 即使 prediction 有效也可能为 None(样本不足),原样透传。
    """
    empty = {"现价": None, "止损位": None, "最大亏损%": None,
             "止盈位": None, "目标盈利%": None, "风险收益比": None, "上涨概率%": None}
    p = (rec or {}).get("prediction")
    if not p or p.get("error"):
        return empty
    hold5 = (p.get("持有期建议") or {}).get("5日") or {}
    scen5 = (p.get("情景预测") or {}).get("5日") or {}
    return {
        "现价": p.get("现价"),
        "止损位": hold5.get("止损位"), "最大亏损%": hold5.get("最大亏损%"),
        "止盈位": hold5.get("止盈位"), "目标盈利%": hold5.get("目标盈利%"),
        "风险收益比": hold5.get("风险收益比"),
        "上涨概率%": scen5.get("上涨概率%"),
    }


def structure_view(rec: dict) -> dict | None:
    """L3 结构位/情景锚定完整视图(供个股页小卡)。只读透传,不计算。

    防空:prediction 缺失 / 为 error 块(次新股 K线<30)/ 无「结构位」子块(老数据)→ None。
    有则原样透传 结构位(含 支撑/压力/距离%/区间位置%/量比/放量/突破/趋势/bias20/锚定)。
    """
    p = (rec or {}).get("prediction")
    if not p or p.get("error"):
        return None
    s = p.get("结构位")
    return s if isinstance(s, dict) else None


def anchor_stops(rec: dict) -> dict:
    """L3 止盈止损(区块①/③ 统一口径)。回退链:结构位.锚定 → 5日 ATR(stops_view)→ 全 None。

    优先「结构位.锚定」(真实盈亏比由点位算,带情景/突破/区间位置%);
    结构位缺失但有 5日 ATR 建议 → 退回 stops_view 的止损/止盈/风险收益比(带最大亏损%/目标盈利%);
    再缺 → 全 None(前端渲染「—」)。source 标注取数来源,便于前端区分展示。
    """
    empty = {"情景": None, "止损位": None, "止盈位": None, "盈亏比": None,
             "突破": None, "区间位置%": None, "最大亏损%": None, "目标盈利%": None,
             "source": None}
    p = (rec or {}).get("prediction")
    if not p or p.get("error"):
        return empty
    s = p.get("结构位")
    if isinstance(s, dict) and isinstance(s.get("锚定"), dict):
        a = s["锚定"]
        return {"情景": a.get("情景"), "止损位": a.get("止损位"),
                "止盈位": a.get("止盈位"), "盈亏比": a.get("盈亏比"),
                "突破": s.get("突破"), "区间位置%": s.get("区间位置%"),
                "最大亏损%": None, "目标盈利%": None, "source": "结构位"}
    st = stops_view(rec)
    if st.get("止损位") is not None:
        return {"情景": None, "止损位": st["止损位"], "止盈位": st["止盈位"],
                "盈亏比": st["风险收益比"], "突破": None, "区间位置%": None,
                "最大亏损%": st["最大亏损%"], "目标盈利%": st["目标盈利%"],
                "source": "5日ATR"}
    return empty


def screen_page(date: str = "latest") -> dict:
    """选股页数据:读 screen 视图 + 补每票关键字段。"""
    recs = _load_all(date)
    try:
        data = store.get_view("screen", date=date)
    except FileNotFoundError:
        return {"presets": {}, "aggregate": {}, "meta": {}, "as_of": as_of(date)}
    detail = {}
    for name, codes in data.get("presets", {}).items():
        rows = []
        for c in codes:
            r = recs.get(c, {})
            cs = council_summary(r) or {}
            rows.append({
                "code": c, "name": _name(recs, c),
                "sector": (r.get("meta") or {}).get("sector"),
                "trend": ((r.get("signals") or {}).get("trend") or {}).get("评级"),
                "tendency": ((r.get("prediction") or {}).get("买卖倾向") or {}).get("结论"),
                "flow": (r.get("fundflow") or {}).get("今日主力净流入"),
                "council_dir": cs.get("综合方向"),
                "council_score": cs.get("综合分"),
                "council_conflict": cs.get("是否冲突", False),
            })
        # 综合分参与排序(D9):有合议分的按分降序在前,无的(None)沉底
        rows.sort(key=lambda x: (x["council_score"] is not None, x["council_score"] or 0),
                  reverse=True)
        detail[name] = rows
    return {"presets": detail, "aggregate": data.get("aggregate", {}), "as_of": as_of(date)}



def selection_page(date: str = "latest") -> dict:
    """选股结果页(三区块 + 综合选股):
        ① 自选股(自选池成员,合议方向/分 + 止盈止损)
        →【综合选股】(勾选策略 → 各策略入选代码并集,前端实时重算)
        → 策略0 · 多专家合议(全A,读 view「策略0合议」top)
        → 策略1 · 趋势深跌反包(读 view「趋势深跌反包」)

    纯读离线 view + 中心记录;任何 view 缺失全部走兜底(present=False / 空列表),页面永不空、不报错。
    合议 config(tau/权重/分母模式)供前端勾选实时重合成(复用 council.js councilSynth)。
    """
    recs = _load_all(date)

    # 区块①「自选股」:只展示当前自选池成员,带合议方向/分 + 专家信封 + 止盈止损(已防空)
    config = None
    pool = _pool_codes()
    pool_rows = []
    for code in pool:
        r = recs.get(code)
        if not r:
            continue
        cs = council_summary(r) or {}
        cblk = (r.get("council") or {})
        if config is None and cblk.get("config"):
            config = cblk["config"]
        pool_rows.append({
            "code": code, "name": _name(recs, code),
            "sector": (r.get("meta") or {}).get("sector"),
            "industry": (r.get("meta") or {}).get("industry"),
            "council_dir": cs.get("综合方向"),
            "council_score": cs.get("综合分"),
            "council_conflict": cs.get("是否冲突", False),
            "参与专家数": len(cblk.get("default", {}).get("参与专家", []) or []),
            "experts": cblk.get("experts") or [],        # 供前端勾选重合成
            "stops": stops_view(r),                      # 5日止盈止损+上涨概率(L1 展示,已防空)
            "anchor": anchor_stops(r),                   # L3 结构位锚定止盈止损(缺则回退 5日ATR,已防空)
        })
    pool_rows.sort(key=lambda x: (x["council_score"] is not None, x["council_score"] or 0), reverse=True)

    # 策略0(全A合议)与策略1(趋势深跌反包):读各自 view,缺失走兜底
    strategy0 = _strategy0_section(recs, date)
    strategy1 = _s01_section(recs, date)
    # config 兜底:自选池无记录时,退用策略0 view 里带的 council config(前端合成口径真源)
    if not config and strategy0.get("config"):
        config = strategy0["config"]

    # 综合选股:各策略入选代码并集(前端按勾选实时重算;后端给全并集 + 每票命中来源)
    combined = _combined_section(strategy0, strategy1, recs)

    return {"rows": pool_rows, "total": len(recs),
            "combined": combined, "strategy0": strategy0, "strategy1": strategy1,
            "config": config or {}, "as_of": as_of(date)}


def _strategy0_section(recs: dict, date: str = "latest") -> dict:
    """策略0「多专家合议(全A)」区块:读 store view「策略0合议」(screen_council 产出)。

    防空(同页其它区块口径):view 缺失 / 非法 → present=False(前端"策略0 待运行")。
    每行:code / name(走 code_name 回退)/ 行业 / 综合方向 / 综合分 / 冲突 / experts(供前端勾选重排)。
    名称优先中心记录 meta,回退 code_name.json,再回退 code。
    """
    empty = {"present": False, "as_of": as_of(date), "扫描数": None, "有效": None,
             "top_n": None, "rows": [], "config": None}
    try:
        v = store.get_view("策略0合议", date=date)
    except FileNotFoundError:
        return empty
    if not isinstance(v, dict):
        return empty
    config = None
    rows = []
    for item in v.get("top", []) or []:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        cblk = item.get("council") or {}
        if config is None and cblk.get("config"):
            config = cblk["config"]
        d = cblk.get("default") or {}
        rows.append({
            "code": code, "name": _name(recs, code),
            "industry": item.get("行业"),
            "council_dir": item.get("综合方向") or d.get("综合方向"),
            "council_score": item.get("综合分", d.get("综合分")),
            "council_conflict": bool(d.get("是否冲突")),
            "experts": cblk.get("experts") or [],
        })
    return {
        "present": True,
        "as_of": v.get("as_of") or as_of(date),
        "扫描数": v.get("扫描数"),
        "有效": v.get("有效", v.get("有效样本")),
        "top_n": v.get("top_n", len(rows)),
        "rows": rows,
        "config": config,
    }


def _combined_section(strategy0: dict, strategy1: dict, recs: dict) -> dict:
    """【综合选股】:各策略入选代码的并集(去重),每票标注命中来源(被哪几个策略选中)。

    后端产出**全并集**(所有可用策略入选代码);前端按勾选的策略实时过滤 + 重算命中来源
    (一个都没勾 → 前端显示"无")。默认全勾(展示全并集)。策略2 暂无 → available=False、codes 空。
    name 走 code_name 回退;行业优先中心记录 meta,再回退策略0 view 自带行业。
    """
    s0_codes = [r["code"] for r in strategy0.get("rows", []) if r.get("code")]
    s1_codes = [r["code"] for r in strategy1.get("rows", []) if r.get("code")]
    # 行业 hint:策略0 view 自带行业(全A票多无中心记录)
    s0_industry = {r["code"]: r.get("industry") for r in strategy0.get("rows", [])}

    sources: dict[str, list[str]] = {}
    order: list[str] = []
    for key, codes in (("策略0", s0_codes), ("策略1", s1_codes)):
        for c in codes:
            if c not in sources:
                sources[c] = []
                order.append(c)
            if key not in sources[c]:
                sources[c].append(key)

    rows = []
    for code in order:
        meta = (recs.get(code) or {}).get("meta") or {}
        rows.append({
            "code": code, "name": _name(recs, code),
            "industry": meta.get("industry") or meta.get("sector") or s0_industry.get(code),
            "sources": sources[code],                    # 前端按勾选过滤 + 拼「策略0+策略1」
        })

    strategies = [
        {"key": "策略0", "label": "策略0", "codes": s0_codes,
         "available": bool(strategy0.get("present"))},
        {"key": "策略1", "label": "策略1", "codes": s1_codes,
         "available": bool(strategy1.get("present"))},
        {"key": "策略2", "label": "策略2(暂无)", "codes": [], "available": False},
    ]
    return {"strategies": strategies, "rows": rows}


def _s01_section(recs: dict, date: str = "latest") -> dict:
    """S01「趋势深跌反包」区块:读 store view「趋势深跌反包」(screen_s01 产出),逐票扁平化。

    防空(同页其它区块口径):view 缺失 → present=False(前端"S01 待运行");
    单票明细缺字段 → 对应值 None(前端渲染「—」),绝不抛异常。
    schema:{扫描数, 有效样本, 跳过数(历史不足), 入选数, 入选清单:[{code, 明细:{MA{5..200}, close,
    H52, 近强_涨/跌:[涨,跌], 当日跌幅(小数), 收阳}}]}。字段名兼容契约的两种写法(有效样本/有效 等)。
    个股名从中心记录 meta 取(_name),取不到显代码。
    """
    empty = {"present": False, "扫描数": None, "有效": None, "跳过": None,
             "入选数": None, "as_of": as_of(date), "rows": []}
    try:
        v = store.get_view("趋势深跌反包", date=date)
    except FileNotFoundError:
        return empty
    if not isinstance(v, dict):
        return empty

    rows = []
    for item in v.get("入选清单", []) or []:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        d = item.get("明细") or {}
        ma = d.get("MA") or {}
        seq = [ma.get(k) for k in ("5", "10", "20", "30", "60", "200")]
        close = d.get("close")
        # 均线完整多头:MA5>MA10>...>MA200 且 close>=MA5(缺 MA 时置 None→前端「—」)
        bull = None
        if all(x is not None for x in seq):
            desc = all(seq[i] > seq[i + 1] for i in range(len(seq) - 1))
            bull = bool(desc and (close is not None and close >= seq[0]))
        h52 = d.get("H52")
        # 是否突破前高:收盘超 52 周高(不含当日)→ 创新高
        broke = (close is not None and h52 is not None and close > h52)
        drop = d.get("当日跌幅")                       # 小数,如 -0.1257
        drop_pct = round(drop * 100, 2) if isinstance(drop, (int, float)) else None
        near = d.get("近强_涨/跌") or [None, None]
        up = near[0] if len(near) > 0 else None
        down = near[1] if len(near) > 1 else None
        rows.append({
            "code": code, "name": _name(recs, code),
            "close": close, "H52": h52, "突破前高": broke,
            "当日跌幅%": drop_pct, "近强_涨": up, "近强_跌": down,
            "均线多头": bull, "收阳": d.get("收阳"),
        })
    return {
        "present": True,
        "扫描数": v.get("扫描数"),
        "有效": v.get("有效样本", v.get("有效")),
        "跳过": v.get("跳过数(历史不足)", v.get("跳过(历史不足)")),
        "入选数": v.get("入选数"),
        "as_of": v.get("as_of") or as_of(date),
        "rows": rows,
    }


def pool_page(date: str = "latest") -> dict:
    """票池管理页数据:当前票池(按板块归组)+ 每票在该日期下是否已有分析数据。"""
    from tools.config import stock_pool
    recs = _load_all(date)
    rows = [{"code": s.code, "name": s.name, "industry": s.industry,
             "sector": s.sector, "has_data": s.code in recs}
            for s in stock_pool.get_pool()]
    rows.sort(key=lambda x: (x["sector"], x["code"]))
    return {"pool": rows, "count": len(rows), "as_of": as_of(date)}


def news_page(date: str = "latest") -> list[dict]:
    """新闻页数据:全池公司行为公告(利好/利空),按日期倒序。"""
    recs = _load_all(date)
    out = []
    for r in recs.values():
        for e in r.get("events", []):
            out.append({"code": r["meta"]["code"], "name": r["meta"]["name"],
                        "sector": r["meta"]["sector"], **e})
    out.sort(key=lambda x: x.get("date", ""), reverse=True)
    return out


# ————————————————————————————————————————————————
# 新闻(读统一「新闻+AI」视图 data/analysis/<日期>/news_ai/{code}.json,经 store)
# 每条:{title, time, source, url, content, ai:{方向, 强度, 与本股关系, 评论, 原因}}
# 缺 news_ai(未跑 enrich / LLM 未配置)→ 回退原始新闻,ai 置空(向后兼容不崩)。
# /news 列、个股页新闻块、详情页 共用此单一 reader,零重复逻辑。
# ————————————————————————————————————————————————
def _empty_ai() -> dict:
    """回退原始新闻时的空 ai 块(中性占位,前端可安全取 .ai.方向)。"""
    return {"方向": "中性", "强度": 0, "与本股关系": "", "评论": "", "原因": ""}


def news_list(code: str, date: str = "latest") -> list[dict]:
    """某票某日「新闻+AI」列表(时间倒序,生产时已排序)。

    优先读 news_ai 视图;缺失回退原始新闻并补空 ai。两源皆缺返回 []。
    """
    try:
        items = store.get_code_view("news_ai", code, date=date)
        if isinstance(items, list):
            return items
    except FileNotFoundError:
        pass
    try:
        raw = store.get_raw("news", code, date=date)
    except FileNotFoundError:
        return []
    if not isinstance(raw, list):
        return []
    return [{**n, "ai": _empty_ai()} for n in raw]


def news_detail(code: str, idx: int, date: str = "latest") -> dict | None:
    """某票某日第 idx 条新闻(含完整正文+来源+链接+AI 评论)。越界返回 None。"""
    items = news_list(code, date)
    if 0 <= idx < len(items):
        return items[idx]
    return None


def news_flow(date: str = "latest") -> list[dict]:
    """全市场当日新闻流:遍历全池各票新闻拍平,按时间倒序。

    每项 = {code, name, sector} + 新闻字段(title/time/source/url/content) + ai。
    """
    recs = _load_all(date)
    out: list[dict] = []
    for code, r in recs.items():
        meta = r.get("meta") or {}
        for i, item in enumerate(news_list(code, date)):
            out.append({"code": code, "name": _resolve_name(r, code),
                        "sector": meta.get("sector", ""), "idx": i, **item})
    out.sort(key=lambda x: x.get("time") or "", reverse=True)
    return out


def dashboard(date: str = "latest") -> dict:
    """首页聚合:板块强弱、超买超卖、拐点榜、资金流榜、买卖倾向汇总、重要公告。"""
    recs = [r for r in _load_all(date).values() if r.get("signals")]

    # 板块强弱(趋势得分均值)
    sec: dict[str, list] = {}
    for r in recs:
        sec.setdefault(r["meta"]["sector"], []).append(r["signals"]["trend"]["得分"])
    sectors = sorted(({"板块": s, "均分": round(sum(v) / len(v), 1), "只数": len(v)}
                      for s, v in sec.items()), key=lambda x: x["均分"], reverse=True)

    def _meta(r):
        return {"code": r["meta"]["code"], "name": r["meta"]["name"],
                "sector": r["meta"]["sector"]}

    # 超买超卖(共振)
    oversold = [{**_meta(r), "verdict": r["signals"]["ob_os"].get("结论")}
                for r in recs if r["signals"]["ob_os"].get("结论") == "超卖"]
    overbought = [{**_meta(r), "verdict": r["signals"]["ob_os"].get("结论")}
                  for r in recs if r["signals"]["ob_os"].get("结论") == "超买"]

    # 拐点榜
    rev = [{**_meta(r), "标签": r["signals"]["reversal"].get("拐点标签"),
            "评分": r["signals"]["reversal"].get("拐点评分", 0)}
           for r in recs if r["signals"]["reversal"].get("拐点标签", "无") != "无"]
    rev.sort(key=lambda x: x["评分"], reverse=True)

    # 资金流榜(今日主力净流入)
    flow = [{**_meta(r), "主力净流入": (r.get("fundflow") or {}).get("今日主力净流入"),
             "连续天数": (r.get("fundflow") or {}).get("主力连续净流入天数", 0)}
            for r in recs if (r.get("fundflow") or {}).get("今日主力净流入") is not None]
    flow.sort(key=lambda x: x["主力净流入"] or 0, reverse=True)

    # 买卖倾向汇总
    tend = {"偏买入": [], "偏卖出": [], "观望": []}
    for r in recs:
        t = ((r.get("prediction") or {}).get("买卖倾向") or {}).get("结论")
        if t in tend:
            tend[t].append(_meta(r))

    # 重要公告(近 25 条)
    important = {"业绩预告", "业绩快报", "增持", "减持", "回购", "合同订单",
                 "诉讼仲裁", "权益变动", "股权激励", "再融资"}
    anns = []
    for r in recs:
        for e in r.get("events", []):
            if e.get("type") in important:
                anns.append({**_meta(r), **e})
    anns.sort(key=lambda x: x.get("date", ""), reverse=True)

    return {"sectors": sectors, "oversold": oversold, "overbought": overbought,
            "reversal": rev, "flow": flow[:10], "flow_out": flow[-5:][::-1],
            "tendency": tend, "announcements": anns[:25], "as_of": as_of(date),
            "total": len(recs)}
