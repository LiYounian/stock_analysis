"""Web 数据访问层:经 store 只读 data/analysis / data/raw(按日期)。

Web 不做计算、不触网,只读离线 run.py 产出的数据。store 按日期分区存储,
本层把"要看哪一天"(date)透传给 store.get_*(date=...);date 缺省 "latest"=最新日期。
展示层只依赖 config + store(基座只读层),不 import 分析器。
"""
from __future__ import annotations

import math

from tools.store import repo as store


def json_safe(obj):
    """把结构里的非法 JSON 浮点(NaN/Inf/-Inf)递归替换为 None。

    离线管线用 json.dumps(allow_nan=True) 落盘,pandas 算出的 NaN(如 kline.volume、
    资金流字段)会以字面量 `NaN` 存进 data/analysis,Python json.loads 能读回 float('nan'),
    但 FastAPI/严格 JSON 编码器会抛 ValueError(Out of range float values)导致接口 500。
    在返回 JSON 的边界统一净化,前端 NaN→null→渲染「—」,不炸接口。
    """
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    return obj


def _num(v, default: float = 0.0) -> float:
    """排序键净化:None / 非数 / NaN / Inf → default。

    得分/评分等字段在数据里可能显式为 null(样本不足/未算),.get(key, default) 只在键
    *缺失* 时兜底,键存在但值为 None 时仍返回 None,混进 sort key 会抛
    '>' not supported between NoneType and int/float。排序前统一折成可比数值。
    """
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)) and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return v
    return default


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
    recs.sort(key=lambda r: _num(((r.get("signals") or {}).get("trend") or {}).get("得分"), -999),
              reverse=True)
    return recs


def financial_page(date: str = "latest") -> dict:
    """财报分析页:所有带财报块的记录 → 评级/审计双闸门/红旗/LLM归纳摘要。

    排序:按评级(风险/差在前,便于排雷),同级按质地分升序。只列有财报数据的票
    (闭环里仅 news_subset=自选∪每策略前5 采财报,故通常几十只)。展示层只读、不算。
    """
    order = {"风险": 0, "差": 1, "中": 2, "良": 3, "优": 4}
    rows = []
    for r in _load_all(date).values():
        fin = r.get("financial")
        if not fin:
            continue
        v = fin.get("verdict") or {}
        rows.append({
            "code": r["meta"]["code"], "name": r["meta"].get("name"),
            "industry": r["meta"].get("industry"),
            "评级": fin.get("评级"), "质地分": fin.get("quality_score"),
            "报告期": fin.get("报告期"), "金融业口径": fin.get("金融业口径"),
            "审计意见闸门": fin.get("审计意见闸门"), "审计机构闸门": fin.get("审计机构闸门"),
            "审计机构": fin.get("审计机构"), "flags": fin.get("flags") or [],
            "LLM评级": v.get("综合评级"), "一句话": v.get("一句话结论"),
        })
    rows.sort(key=lambda x: (order.get(x["评级"], 9), _num(x["质地分"], 999)))
    return {"rows": rows, "count": len(rows), "date": date}


def financial_detail(code: str, date: str = "latest") -> dict | None:
    """单只票的**详细**财报分析页数据:分析+证据(带来源)+ AI 讲解 + 审计标准。

    组装:record.financial 块(评级/五维/红旗明细/审计双闸门/LLM verdict+qualitative)
    + config 审计标准(红旗阈值/五维标准化区间/评级映射/审计意见通过口径/名录家数)
    + 年报原文段落(MD&A/风险,LLM 定性的来源证据)。展示层只读、不算。
    """
    rec = get_record(code, date)
    if not rec or not rec.get("financial"):
        return None
    from tools.config import strategy
    from tools.analysis.financial import scoring
    cfg = strategy.THRESHOLDS.get("财报", {})
    fin = rec["financial"]
    # 资金/负债/年报节选:M2 起写进 financial 块(随记录同步远端;upload 不含 data/raw,故不再读 raw)
    cash = fin.get("现金流")
    balance = fin.get("资产负债")
    annual = fin.get("年报节选")
    # 审计名录家数/更新日期(闸门1 标准来源)
    firms_meta = {}
    try:
        import json as _json
        from pathlib import Path as _Path
        p = _Path(__file__).resolve().parent.parent / "tools" / "config" / "audit_firms.json"
        d = _json.loads(p.read_text(encoding="utf-8"))
        firms_meta = {"覆盖家数": d.get("覆盖家数"), "更新日期": d.get("更新日期"), "口径说明": d.get("口径说明")}
    except Exception:                                       # noqa: BLE001
        pass
    return {
        "code": str(code).zfill(6), "name": rec["meta"].get("name"),
        "industry": rec["meta"].get("industry"), "sector": rec["meta"].get("sector"),
        "fin": rec["financial"],
        "现金流": cash, "资产负债": balance,
        "annual": annual,
        "standards": {
            "红旗阈值": cfg.get("红旗", {}), "严重度": cfg.get("严重度", {}),
            "审计意见_通过": cfg.get("审计意见_通过", []),
            "金融业跳过红旗": cfg.get("金融业跳过红旗", []),
            "评分": cfg.get("评分", {}), "五维区间": scoring.dimension_specs(),
            "名录": firms_meta,
        },
    }


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
    """单记录版名称回退:rec.meta.name → code_name.json[code] → code。

    注:全A 选出票的旧口径 record 里 meta.name 被填成代码本身(serialize 无池名时 name=code),
    故 meta.name 等于 code 时视为"无有效名"、继续走 code_name.json,避免名称列显示成数字。
    """
    nm = ((rec or {}).get("meta") or {}).get("name")
    if nm and nm != code:
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
    """选股结果页(自选股 + 综合选股 + 5 策略):
        ① 自选股(自选池成员,合议方向/分 + 止盈止损)
        →【综合选股】(勾选策略0/1/2/3/4 → 各策略入选代码并集,前端实时重算)
        → 策略0 · 多专家合议(全A,读 view「策略0合议」top)
        → 策略1 · 趋势深跌反包(读 view「趋势深跌反包」,标「待验证」)
        → 策略2 · 放量后缩量回踩 S02(读 view「放量后缩量回踩」,标「待验证」)
        → 策略3 · 箱体形态(读 view「箱体形态」,标「待验证」)
        → 策略4 · 动量组合(读 view「动量组合」)

    纯读离线 view + 中心记录:策略0~4 全部读全A screener 预落盘 view(offline run.py 产出),
    web 不触网、不计算。任何 view 缺失 / pool 空全部走兜底(present=False / 空列表),
    页面永不空、不报错。合议 config(tau/权重/分母模式)供前端勾选实时重合成(复用 council.js councilSynth)。
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

    # 策略0~4 全部读全A screener 预落盘 view(策略0 top / 其余 入选清单)
    strategy0 = _strategy0_section(recs, date)
    strategy1 = _s01_section(recs, date)
    strategy2 = _strategy2_section(recs, date)     # 放量后缩量回踩 S02(全A view,待验证)
    strategy3 = _strategy3_section(recs, date)     # 箱体形态(全A view,待验证)
    strategy4 = _strategy4_section(recs, date)     # 动量组合(全A view,原策略2 改号 2→4)
    strategy5 = _strategy5_section(recs, date)     # 自选池小市值(web 层实时跑策略D,不读 view)
    strategy6 = _strategy6_section(recs, date)     # 半导体多因子(优先读 view「半导体多因子」,缺则实时兜底)
    # config 兜底:自选池无记录时,退用策略0 view 里带的 council config(前端合成口径真源)
    if not config and strategy0.get("config"):
        config = strategy0["config"]

    # 综合选股:7 策略入选代码并集(前端按勾选实时重算;后端给全并集 + 每票命中来源)
    combined = _combined_section(strategy0, strategy1, strategy2, strategy3,
                                 strategy4, strategy5, strategy6, recs)

    return {"rows": pool_rows, "total": len(recs),
            "combined": combined, "strategy0": strategy0, "strategy1": strategy1,
            "strategy2": strategy2, "strategy3": strategy3, "strategy4": strategy4,
            "strategy5": strategy5, "strategy6": strategy6,
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


def _view_picks_section(view_name: str, recs: dict, date: str = "latest",
                        cap: int = 30) -> dict:
    """通用:读全A screener 预落盘 view(策略2/3/4 同构),取 入选清单 → 补名称/行业 → top-N。

    schema(全A screener 统一口径,同 screen_s02/screen_s01):
      {as_of, 扫描数, 有效样本, 入选数, 入选清单:[{code, 行业?, 组合?, ...}]}。
    · 名称走 _name 回退(中心记录 meta → code_name.json → code);
    · 行业优先中心记录 meta,再回退 view item 自带「行业」;
    · 组合(策略4 用)读 item「组合」/「combos」,可为 str 或 list,缺则空 list;
    · picks 与展示 rows 对齐(都截到 cap):combined 并集口径 = 页面实际展示的票,不多不少。

    防空(同页其它区块口径):view 缺失 / 非法 → present=False(前端「待运行」),绝不抛。
    """
    empty = {"present": False, "as_of": as_of(date), "扫描数": None, "有效": None,
             "入选数": None, "rows": [], "picks": []}
    try:
        v = store.get_view(view_name, date=date)
    except FileNotFoundError:
        return empty
    if not isinstance(v, dict):
        return empty

    picks: list[str] = []
    rows: list[dict] = []
    for item in v.get("入选清单", []) or []:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        if not code or len(rows) >= cap:
            continue
        combos = item.get("组合") or item.get("combos") or []
        if isinstance(combos, str):
            combos = [combos]
        meta = (recs.get(code) or {}).get("meta") or {}
        picks.append(code)
        rows.append({"code": code, "name": _name(recs, code),
                     "industry": meta.get("industry") or meta.get("sector") or item.get("行业"),
                     "combos": list(combos)})
    return {
        "present": True,
        "as_of": v.get("as_of") or as_of(date),
        "扫描数": v.get("扫描数"),
        "有效": v.get("有效样本", v.get("有效")),
        "入选数": v.get("入选数", len(v.get("入选清单", []) or [])),
        "rows": rows,
        "picks": picks,
    }


def _strategy2_section(recs: dict, date: str = "latest") -> dict:
    """策略2「放量后缩量回踩(S02)」区块:读全A screener view「放量后缩量回踩」(screen_s02 产出)。

    schema:{as_of, 扫描数, 有效样本, 跳过数(历史不足), 入选数, 入选清单:[{code, 明细}]}。
    与策略0/1 一致:全A 预落盘 view;view 缺失 → present=False(前端「策略2 待运行」)。

    标「待验证」:S02 仅做过"信号日收盘机械基线"回测(edge 薄不足定论),买点未定,仅供观察。
    """
    return _view_picks_section("放量后缩量回踩", recs, date)


def _strategy3_section(recs: dict, date: str = "latest") -> dict:
    """策略3「箱体形态」区块:读全A screener view「箱体形态」(screen_box 产出)。

    schema:{as_of, 扫描数, 有效样本, 入选数, 入选清单:[{code, ...}]}。
    与策略0/1 一致:全A 预落盘 view;view 缺失 → present=False(前端「策略3 待运行」)。

    标「待验证」:箱体几何参数刚录入、未回测,仅供观察。
    """
    return _view_picks_section("箱体形态", recs, date)


def _strategy4_section(recs: dict, date: str = "latest") -> dict:
    """策略4「动量组合」区块(原策略2 改号 2→4):读全A screener view「动量组合」(screen_momentum 产出)。

    schema:{as_of, 扫描数, 有效样本, 入选数, 入选清单:[{code, 组合?, ...}]};
    「组合」标注该票命中"动量组合"/"红利动量组合"(可为 list 或 str),供展示「组合」列。
    与策略0/1 一致:全A 预落盘 view;view 缺失 → present=False(前端「策略4 待运行」)。
    动量入选可能达 top30,展示已截到 cap。
    """
    return _view_picks_section("动量组合", recs, date)


def _strategy6_section(recs: dict, date: str = "latest", top_k: int = 8) -> dict:
    """策略6「半导体多因子」区块:优先读全A screener view「半导体多因子」(screen_semi_factor
    产出),缺 view 才回退 web 层实时跑(限半导体池,records ∩ 池)。

    数据链闭环后(cmd_all/screenall):view 覆盖全A 半导体池 178 只真实结果;
    仅当 view 不存在时(如从未跑过 pipeline)才走实时算兜底,规避页面空。
    3 因子:研发/营收(权 0.6)+ 研发/市值(权 0.2)+ 营收增速(权 0.2);限池由策略自身完成
    (读 config/semi_universe.json 178 只)。
    """
    # 优先读预落盘 view(schema 与 screen_semi_factor 产出一致)
    try:
        v = store.get_view("半导体多因子", date=date)
    except FileNotFoundError:
        v = None
    if isinstance(v, dict) and v.get("入选清单"):
        rows = []
        picks = []
        for item in v.get("入选清单", []) or []:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            if not code:
                continue
            d = item.get("明细") or {}
            meta = (recs.get(code) or {}).get("meta") or {}
            picks.append(code)
            rows.append({
                "code": code, "name": _name(recs, code),
                "industry": meta.get("industry") or meta.get("sector") or item.get("行业"),
                "综合分": d.get("综合分"),
                "rd_rev": d.get("rd_rev"),
                "rd_mcap": d.get("rd_mcap"),
                "rev_yoy": d.get("rev_yoy"),
            })
        return {
            "present": True, "as_of": v.get("as_of") or as_of(date),
            "扫描数": v.get("扫描数"),
            "universe_size": v.get("universe_size"),
            "样本数": v.get("有效样本"),
            "入选数": v.get("入选数", len(rows)),
            "top_k": v.get("top_k", top_k),
            "权重": v.get("权重"),
            "note": None,
            "rows": rows,
            "picks": picks,
            "source": "view",
        }

    # 回退:实时跑(records ∩ 半导体池;数据链未跑通时兜底)
    from tools.strategy import registry as _reg
    from tools.strategy import semi_factor as _sf  # noqa: F401 触发注册

    empty = {"present": False, "as_of": as_of(date), "扫描数": 0, "入选数": 0,
             "rows": [], "picks": [], "universe_size": None, "note": None, "source": "live"}
    if not recs:
        return empty
    try:
        out = _reg.run("策略E_半导体多因子", recs, top_k=top_k)
    except Exception:                                    # noqa: BLE001
        return empty

    detail_by_code = {d["code"]: d for d in out.get("因子明细", [])}
    rows = []
    for code in out.get("codes", []):
        r = recs.get(code) or {}
        meta = r.get("meta") or {}
        d = detail_by_code.get(code, {})
        rows.append({
            "code": code, "name": _name(recs, code),
            "industry": meta.get("industry") or meta.get("sector"),
            "综合分": d.get("综合分"),
            "rd_rev": d.get("rd_rev"),
            "rd_mcap": d.get("rd_mcap"),
            "rev_yoy": d.get("rev_yoy"),
        })
    return {
        "present": True, "as_of": as_of(date),
        "扫描数": out.get("monthly_pool_size", 0),
        "入选数": len(rows),
        "top_k": out.get("top_k", top_k),
        "universe_size": out.get("universe_size"),
        "样本数": out.get("monthly_pool_size"),
        "权重": out.get("权重"),
        "note": out.get("note"),
        "rows": rows,
        "picks": list(out.get("codes") or []),
        "source": "live",
    }


def _strategy5_section(recs: dict, date: str = "latest", top_k: int = 3) -> dict:
    """策略5「自选池小市值组合」区块:web 层实时跑 tools.strategy.small_cap 策略D。

    与策略0~4 不同——**不读预落盘 view**,自选池版数据已在 records 里,
    直接调 strategy 函数拿结果(记不动 store)。传入 records = 自选池 ∩ recs。
    top_k=3(策略D 默认);市值缺失/触涨跌停/停牌 由策略过滤,embargo 单独标透传前端。

    输出与其他策略区块同构:{present, as_of, 扫描数, 入选数, rows:[{code, name, industry, ...}]}
    额外一个 embargo 字段供模板显示"空仓月"提示,但不代买 ETF。
    """
    from tools.strategy import registry as _reg
    from tools.strategy import small_cap as _sc  # noqa: F401 触发注册

    pool = _pool_codes()
    scoped = {c: r for c, r in (recs or {}).items() if c in pool}
    empty = {"present": False, "as_of": as_of(date), "扫描数": len(scoped), "入选数": 0,
             "rows": [], "picks": [], "embargo": False, "candidates": []}
    if not scoped:
        return empty

    try:
        out = _reg.run("策略D_自选池小市值组合", scoped, top_k=top_k)
    except Exception:                                    # noqa: BLE001
        return empty

    rows = []
    for code in out.get("codes", []):
        r = scoped.get(code) or {}
        meta = r.get("meta") or {}
        val = r.get("valuation") or {}
        snap = r.get("snapshot") or {}
        rows.append({
            "code": code, "name": _name(recs, code),
            "industry": meta.get("industry") or meta.get("sector"),
            "mktcap_yi": val.get("mktcap_yi"),
            "close": snap.get("close"), "pct_chg": snap.get("pct_chg"),
        })
    return {
        "present": True, "as_of": as_of(date),
        "扫描数": len(scoped), "入选数": len(rows),
        "top_k": out.get("top_k", top_k),
        "月度池": out.get("monthly_pool_size"),
        "候选池": out.get("candidates", []),
        "embargo": out.get("embargo", False),
        "rows": rows,
        "picks": list(out.get("codes") or []),
    }


def _combined_section(strategy0: dict, strategy1: dict, strategy2: dict,
                      strategy3: dict, strategy4: dict, strategy5: dict,
                      strategy6: dict, recs: dict) -> dict:
    """【综合选股】:7 策略入选代码的并集(去重),每票标注命中来源(被哪几个策略选中)。

    后端产出**全并集**(所有可用策略入选代码);前端按勾选的策略实时过滤 + 重算命中来源
    (一个都没勾 → 前端显示"无")。默认全勾(展示全并集)。
    name 走 code_name 回退;行业优先中心记录 meta,再回退策略0 view 自带行业。

    策略0~4 入选代码均来自各自全A screener 预落盘 view(与页面各区块展示口径一致,已截到 cap);
    策略5 = 自选池小市值(web 实时跑) · 策略6 = 半导体多因子(优先读 view「半导体多因子」,缺则实时兜底)。
    """
    s0_codes = [r["code"] for r in strategy0.get("rows", []) if r.get("code")]
    s1_codes = [r["code"] for r in strategy1.get("rows", []) if r.get("code")]
    s2_codes = list(strategy2.get("picks") or [])
    s3_codes = list(strategy3.get("picks") or [])
    s4_codes = list((strategy4 or {}).get("picks") or [])
    s5_codes = list((strategy5 or {}).get("picks") or [])
    s6_codes = list((strategy6 or {}).get("picks") or [])
    # 行业 hint:策略0 view 自带行业(全A票多无中心记录)
    s0_industry = {r["code"]: r.get("industry") for r in strategy0.get("rows", [])}

    sources: dict[str, list[str]] = {}
    order: list[str] = []
    for key, codes in (("策略0", s0_codes), ("策略1", s1_codes), ("策略2", s2_codes),
                       ("策略3", s3_codes), ("策略4", s4_codes), ("策略5", s5_codes),
                       ("策略6", s6_codes)):
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
            "sources": sources[code],                    # 前端按勾选过滤 + 拼「策略0+策略2」
        })

    # label = 人读名,title = 悬停 tooltip;key 保持"策略X"以兼容 sources 已落库口径
    strategies = [
        {"key": "策略0", "label": "多专家合议", "codes": s0_codes,
         "available": bool(strategy0.get("present")),
         "title": "全 A 5000+ 只由 6 类数据专家(技术 / 资金 / 情绪 / 事件 / 多因子 …)"
                  "分头打分,弃权项自动剔除后按综合分排序,取 Top N。"},
        {"key": "策略1", "label": "筛选低吸股票", "codes": s1_codes,
         "available": bool(strategy1.get("present")),
         "title": "识别庄家暴力洗盘后的低吸候选(只筛选、不含买入信号)。"
                  "买点低吸 / 追为后续主观决策。"},
        {"key": "策略2", "label": "放量后缩量回踩", "codes": s2_codes,
         "available": bool(strategy2.get("present")),
         "title": "周线放量后当日缩量回踩 10 日线的候选(待验证);"
                  "回测为信号日收盘机械基线、非最终买法,仅供观察。"
                  "全 A 筛选,读预落盘 view。"},
        {"key": "策略3", "label": "箱体形态", "codes": s3_codes,
         "available": bool(strategy3.get("present")),
         "title": "箱体整理突破候选(欧奈尔/墨菲经典形态),参数已录入待回测(待验证)。"
                  "全 A 筛选,读预落盘 view。"},
        {"key": "策略4", "label": "动量组合", "codes": s4_codes,
         "available": bool(strategy4.get("present")),
         "title": "移植自聚宽社区双策略:加权对数动量打分 + 拉普拉斯闸门(策略A提炼);"
                  "质地过滤 + BBI 站上 + 24 日动量排序(策略B红利腿提炼)。"
                  "全 A 筛选,读预落盘 view。"},
        {"key": "策略5", "label": "自选池小市值", "codes": s5_codes,
         "available": bool((strategy5 or {}).get("present")),
         "title": "移植自聚宽「价值选股与RSRS择时」:自选池内按市值升序,"
                  "剔除触涨跌停/停牌;空仓月(12-22~1-28、3-20~4-28)仅标记不代买 ETF。"
                  "web 层实时跑,不读预落盘 view。"},
        {"key": "策略6", "label": "半导体多因子", "codes": s6_codes,
         "available": bool((strategy6 or {}).get("present")),
         "title": "移植自聚宽「半导体板块多因子策略」:限申万二级 801081 半导体池 178 只 + "
                  "3 因子 winsor+zscore 加权——研发/营收(权 0.6)+ 研发/市值(权 0.2)+ 营收增速(权 0.2);"
                  "重研发投入 + 高增长。web 层实时跑,不读预落盘 view;"
                  "本机 records 常只覆盖自选池,需远端全A 闭环采到半导体票后才出结果。"},
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
             "sector": s.sector, "market": s.market, "has_data": s.code in recs}
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
        sec.setdefault(r["meta"]["sector"], []).append(_num(r["signals"]["trend"].get("得分")))
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
            "评分": _num(r["signals"]["reversal"].get("拐点评分"), 0)}
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


# ———— 选股分析报告(data/reports/选股分析/*.md,供 /selection-analysis 页展示)————
import re as _re
from pathlib import Path as _Path

from tools.config import settings as _settings

_REPORT_DIR = _settings.PROJECT_ROOT / "data" / "reports" / "选股分析"


def _report_title(text: str, fallback: str) -> str:
    """取 markdown 第一行 # 一级标题作标题;无则用文件名。"""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip().lstrip("⚠️ ").strip()
    return fallback


def _report_date(stem: str) -> str:
    """从文件名里的 8 位日期 YYYYMMDD 解析成 YYYY-MM-DD;无则空。"""
    m = _re.search(r"(20\d{6})", stem)
    if not m:
        return ""
    d = m.group(1)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


_REPORT_VIEW = "选股分析报告"    # store 容器视图(远端 DB 后端经此展示,见 tools/sync/publish_report.py)


def _store_reports() -> dict[str, dict]:
    """store 容器视图里的报告 {name: {name,title,date,md}}(远端/已上传);无则空。"""
    try:
        return (store.get_view(_REPORT_VIEW) or {}).get("reports", {})
    except FileNotFoundError:
        return {}


def _fs_report_text(name: str) -> str | None:
    """本地文件系统报告原文(名经白名单,防路径穿越);无则 None。"""
    p = _REPORT_DIR / f"{name}.md"
    if _REPORT_DIR.is_dir() and p.is_file() and p.parent == _REPORT_DIR:
        return p.read_text(encoding="utf-8")
    return None


def list_analysis_reports() -> list[dict]:
    """列出报告:本地 data/reports/选股分析/*.md ∪ store 容器视图(远端),按日期倒序。"""
    merged: dict[str, dict] = {}
    for name, r in _store_reports().items():         # 远端/已上传(DB 后端只有这个)
        merged[name] = {"name": name, "title": r.get("title") or name, "date": r.get("date") or ""}
    if _REPORT_DIR.is_dir():                          # 本地文件优先(覆盖标题/日期)
        for p in _REPORT_DIR.glob("*.md"):
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            merged[p.stem] = {"name": p.stem, "title": _report_title(text, p.stem),
                              "date": _report_date(p.stem)}
    out = list(merged.values())
    out.sort(key=lambda r: (r["date"], r["name"]), reverse=True)
    return out


def get_analysis_report(name: str) -> dict | None:
    """按报告名读取并渲染为 HTML。本地文件优先,否则读 store 容器视图(远端)。"""
    text = _fs_report_text(name)
    if text is None:
        text = (_store_reports().get(name) or {}).get("md")
    if not text:
        return None
    import markdown as _md
    html = _md.markdown(text, extensions=["tables", "fenced_code", "toc", "sane_lists"])
    return {"name": name, "title": _report_title(text, name),
            "date": _report_date(name), "html": html}


def sepa_page(date: str = "latest") -> dict:
    """SEPA+VCP 监控页:只读合格池/观察池/雷达 view。缺视图 → 空表不崩。"""
    def _v(name):
        try:
            return store.get_view(name, date=date)
        except FileNotFoundError:
            return {}
    pool = _v("SEPA合格池")
    watch = _v("SEPA观察池")
    radar = _v("SEPA雷达")
    return {
        "as_of": pool.get("as_of") or as_of(date),
        "session": pool.get("session") or watch.get("session") or "",
        "合格": pool.get("rows") or [],
        "观察": watch.get("rows") or [],
        "雷达": radar.get("文本") or watch.get("雷达") or "",
        "合格数": pool.get("合格数") or len(pool.get("rows") or []),
        "观察数": watch.get("观察数") or len(watch.get("rows") or []),
        "规则": pool.get("规则") or "",
    }


def sepa_detail(code: str, date: str = "latest") -> dict | None:
    """单票收缩结构参考图 + 观察池行(若有)。无图则 None。"""
    try:
        chart = store.get_code_view("sepa_vcp_chart", code, date=date)
    except FileNotFoundError:
        return None
    row = None
    try:
        watch = store.get_view("SEPA观察池", date=date)
        for r in watch.get("rows") or []:
            if r.get("code") == code:
                row = r
                break
    except FileNotFoundError:
        pass
    if row is None:
        try:
            pool = store.get_view("SEPA合格池", date=date)
            for r in pool.get("rows") or []:
                if r.get("code") == code:
                    row = r
                    break
        except FileNotFoundError:
            pass
    name = (row or {}).get("name") or _code_name_map().get(code) or code
    return {"code": code, "name": name, "row": row or {}, "chart": chart,
            "as_of": as_of(date)}

