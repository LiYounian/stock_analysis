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
    """选股结果页:当日全部记录一张表——是否达标(形态选股)+ 达标理由 + 合议综合方向/分 + 参与专家数。

    数据源 + 兜底:优先读 store view「形态选股」达标池(标达标/命中形态/正向确认);
    该视图缺失时**退化为"读当日全部记录、按合议综合分排序展示"**(页面永不空,dev 10 只也立即可见)。
    默认按合议综合分降序;每行带该票 council 专家信封 + 共享 config,供前端勾选实时重排(复用 council.js)。
    """
    recs = _load_all(date)
    # 达标池(形态选股 view);缺失 → 兜底
    qualified: dict[str, dict] = {}
    qualified_items: list[dict] = []          # 保序 + 带板块 hint(供区块②分组)
    near_items: list[dict] = []               # 接近达标(平盘无达标时区块②降级展示)
    view_present = False
    view_meta: dict = {}
    try:
        v = store.get_view("形态选股", date=date)
        view_present = True
        for item in v.get("达标清单", []):
            code = item.get("code")
            reason = {"命中形态": item.get("命中形态"), "正向确认依据": item.get("正向确认依据", [])}
            qualified[code] = reason
            # 行业/板块:优先 view 自带(未来 screen_pattern 可能补),否则回退中心记录 meta
            qualified_items.append({"code": code, **reason,
                                    "行业hint": item.get("行业") or item.get("板块") or item.get("sector")})
        # 接近达标:screen_pattern 产出为「{板块: [items]}」字典(每板块top3);拍平成扁平列表供下游用。
        # 兼容旧/异常形状:dict→拍平 values;list→原样;其它→空。每 item 自带「行业」字段可再分组。
        _near_raw = v.get("接近达标") or {}
        if isinstance(_near_raw, dict):
            near_items = [it for items in _near_raw.values() for it in (items or []) if isinstance(it, dict)]
        elif isinstance(_near_raw, list):
            near_items = [it for it in _near_raw if isinstance(it, dict)]
        else:
            near_items = []
        view_meta = {"扫描数": v.get("扫描数"), "有效样本": v.get("有效样本"),
                     "达标数": v.get("达标数"), "达标占比": v.get("达标占比"),
                     "纪律": v.get("纪律"), "RS模式": v.get("RS模式")}
    except FileNotFoundError:
        view_present = False

    config = None
    rows = []
    for code, r in recs.items():
        cs = council_summary(r) or {}
        cblk = (r.get("council") or {})
        experts_env = cblk.get("experts") or []
        if config is None and cblk.get("config"):
            config = cblk["config"]
        q = qualified.get(code)
        rows.append({
            "code": code, "name": _name(recs, code),
            "sector": (r.get("meta") or {}).get("sector"),
            "industry": (r.get("meta") or {}).get("industry"),
            "qualified": (code in qualified) if view_present else None,
            "达标理由": q,                               # {命中形态, 正向确认依据} 或 None
            "council_dir": cs.get("综合方向"),
            "council_score": cs.get("综合分"),
            "council_conflict": cs.get("是否冲突", False),
            "参与专家数": len(cblk.get("default", {}).get("参与专家", []) or []),
            "experts": experts_env,                      # 供前端勾选重合成
            "stops": stops_view(r),                      # 5日止盈止损+上涨概率(L1 展示,已防空)
            "anchor": anchor_stops(r),                   # L3 结构位锚定止盈止损(缺则回退 5日ATR,已防空)
        })
    # 默认按合议综合分降序;无合议分(None)沉底
    rows.sort(key=lambda x: (x["council_score"] is not None, x["council_score"] or 0), reverse=True)

    total = len(rows)
    qualified_n = (view_meta.get("达标数") if view_present else None)
    if qualified_n is None and view_present:
        qualified_n = sum(1 for x in rows if x["qualified"])

    # 区块①「自选股」只展示当前自选池成员(区块②每日筛选/③Top/S01 仍用全A/达标票);
    # total/qualified 仍为全量扫描口径(描述"本页共分析")。pool 代码当日无记录自然跳过,不报错。
    pool = _pool_codes()
    pool_rows = [x for x in rows if x["code"] in pool]

    daily = _daily_sections(qualified_items, near_items, recs, view_present, view_meta, qualified_n)
    top_picks = _top_picks(qualified_items, near_items, recs)
    s01 = _s01_section(recs, date)
    return {"rows": pool_rows, "total": total, "qualified": qualified_n,
            "view_present": view_present, "view_meta": view_meta,
            "daily": daily, "top_picks": top_picks, "s01": s01,
            "config": config or {}, "as_of": as_of(date)}


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


def _top_picks(qualified_items: list[dict], near_items: list[dict], recs: dict,
               top_n: int = 15) -> list[dict]:
    """区块③「今日精选(数据策略综合)」:达标清单 ∪ 接近达标 ∪ 自选池,按合议综合分降序 Top N。

    纯数据策略(技术趋势/超买超卖/拐点/资金流/多因子/板块轮动的合议),未用新闻/大模型。
    取数来源三并集去重;合议分优先取中心记录 council.default.综合分,无记录(全A接近达标票)取契约「合议分」。
    每行带 anchor(L3 止盈止损,已防空回退)+ experts(供前端勾选实时重排 Top N)。
    达标池 view 缺失时 qualified_items/near_items 为空,仍以自选池(recs)兜底出 Top N。
    """
    near_score = {it.get("code"): it.get("合议分") for it in (near_items or []) if it.get("code")}
    qualified_codes = {it.get("code") for it in (qualified_items or []) if it.get("code")}
    codes: set[str] = set(recs.keys())
    codes |= qualified_codes
    codes |= {it.get("code") for it in (near_items or []) if it.get("code")}
    rows = []
    for code in codes:
        r = recs.get(code)
        cs = council_summary(r) or {}
        score = cs.get("综合分")
        if score is None:
            score = near_score.get(code)
        meta = (r or {}).get("meta") or {}
        cblk = (r or {}).get("council") or {}
        a = anchor_stops(r)
        rows.append({
            "code": code, "name": _resolve_name(r, code),
            "industry": meta.get("industry") or meta.get("sector"),
            "情景": a["情景"], "止损位": a["止损位"], "止盈位": a["止盈位"],
            "盈亏比": a["盈亏比"], "突破": a["突破"], "区间位置%": a["区间位置%"],
            "council_dir": cs.get("综合方向"), "council_score": score,
            "council_conflict": cs.get("是否冲突", False),
            "qualified": code in qualified_codes,
            "experts": cblk.get("experts") or [],
        })
    # 合议综合分降序;无合议分(None)沉底
    rows.sort(key=lambda x: (x["council_score"] is not None, x["council_score"] or 0), reverse=True)
    return rows[:top_n]


def _group_by_board(items: list[dict], recs: dict, row_builder, top_n: int) -> list[dict]:
    """通用:按板块分组 → 组内按合议分降序取 top_n → 板块按组内最高分降序(并列看 count)。

    row_builder(item, rec) → (板块名, 行字典);行字典须含 council_score(可 None,沉底)。
    """
    from collections import defaultdict
    buckets: dict[str, list] = defaultdict(list)
    for it in items:
        board, row = row_builder(it, recs.get(it.get("code")))
        buckets[board].append(row)
    groups = []
    for board, rows in buckets.items():
        rows.sort(key=lambda x: (x["council_score"] is not None, x["council_score"] or 0), reverse=True)
        groups.append({"板块": board, "count": len(rows), "rows": rows[:top_n]})
    groups.sort(key=lambda g: (max((r["council_score"] or -9 for r in g["rows"]), default=-9), g["count"]),
                reverse=True)
    return groups


def _qual_row(it: dict, rec: dict | None):
    """达标票行:命中形态 + 正向确认 + 该票 council(复用中心记录)+ 专家信封(供前端重排)。"""
    code = it["code"]
    meta = (rec or {}).get("meta") or {}
    board = it.get("行业hint") or meta.get("industry") or meta.get("sector") or "未分类"
    cs = council_summary(rec) or {}
    cblk = (rec or {}).get("council") or {}
    return board, {
        "code": code, "name": _resolve_name(rec, code),
        "industry": meta.get("industry") or meta.get("sector"),
        "命中形态": it.get("命中形态"), "正向确认依据": it.get("正向确认依据", []),
        "council_dir": cs.get("综合方向"), "council_score": cs.get("综合分"),
        "council_conflict": cs.get("是否冲突", False),
        "experts": cblk.get("experts") or [],
    }


def _near_row(it: dict, rec: dict | None):
    """接近达标行(仅提示,非达标信号):最接近形态 + 差距说明 + 合议分(优先契约自带,回退中心记录)。

    合议分取 view 契约的「合议分」(全A票可能无中心记录);有记录时补方向。**不带 experts**——
    静态提示,不参与前端勾选重排(前端只重排 tbody.daily-group,接近达标用 daily-near 类跳过)。
    """
    code = it.get("code")
    meta = (rec or {}).get("meta") or {}
    board = it.get("行业") or meta.get("industry") or meta.get("sector") or "未分类"
    cs = council_summary(rec) or {}
    score = it.get("合议分")
    if score is None:
        score = cs.get("综合分")
    return board, {
        "code": code, "name": _resolve_name(rec, code),
        "industry": it.get("行业") or meta.get("industry") or meta.get("sector"),
        "最接近形态": it.get("最接近形态"), "差距说明": it.get("差距说明"),
        "council_dir": cs.get("综合方向"), "council_score": score,
        "council_conflict": cs.get("是否冲突", False),
    }


def _daily_sections(qualified_items: list[dict], near_items: list[dict], recs: dict,
                    view_present: bool, view_meta: dict, qualified_n,
                    top_n: int = 5, near_top_n: int = 3) -> dict:
    """区块②「每日筛选」:全市场按行业/板块分组,永不空页。三态:

    - view 缺失          → present=False(前端"待扫描生成")。
    - 达标数>0           → mode=qualified,展示达标票(每板块 top_n)。
    - 达标数==0 有接近达标 → mode=near,展示各板块接近达标 top(前端加"仅提示"标注)。
    - 达标==0 且无接近达标 → mode=qualified、groups 空(前端"本日无达标且无接近达标")。

    行业来源:view item 自带 → 中心记录 meta.industry/sector → 「未分类」(全A票可能无记录)。
    """
    if not view_present:
        return {"present": False, "mode": None, "total_scanned": None,
                "qualified_n": None, "near_n": None, "board_count": 0,
                "top_n": top_n, "groups": []}
    near_items = near_items or []
    near_n = len(near_items)
    if qualified_n and qualified_n > 0:
        groups, mode, used_top = _group_by_board(qualified_items, recs, _qual_row, top_n), "qualified", top_n
    elif near_items:
        groups, mode, used_top = _group_by_board(near_items, recs, _near_row, near_top_n), "near", near_top_n
    else:
        groups, mode, used_top = [], "qualified", top_n
    return {"present": True, "mode": mode, "total_scanned": view_meta.get("扫描数"),
            "qualified_n": qualified_n, "near_n": near_n, "board_count": len(groups),
            "top_n": used_top, "groups": groups}


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
