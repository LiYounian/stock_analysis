"""事件驱动数据采集(F7):业绩预告 yjyg / 业绩快报 yjkb / 高管增减持 ggcg。

数据源:AKShare(封装东财)——`stock_yjyg_em(date=报告期)` / `stock_yjkb_em(date=报告期)` /
`stock_ggcg_em()`。AKShare 列名随版本/上游改版会变,故**列名按关键字模糊匹配 + 全程 try/except 降级**:
任一步失败 → 记 logger、返回空,绝不抛断整条流水(东财被墙/限流时优雅降级,见问题台账 B2/R5)。

缓存:走 store.put_raw(kind, tag, df);tag 用报告期/日期。消费见 tools/analysis/event_driven/summary.py。
⚠️ 未接入 run.py 定时(run.py 不在本轮改动范围);需要精数值时手动/后续流水调用本模块的 fetch_*。

依赖:pandas + (可选)akshare;不 import web/serialize。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.store import repo as store

logger = logging.getLogger("collectors.event_driven")


def _akshare():
    """惰性导入 akshare;未安装 → None(降级)。"""
    try:
        import akshare as ak
        return ak
    except Exception as e:                      # noqa: BLE001
        logger.warning("akshare 不可用,事件采集降级: %s", e)
        return None


def _find_col(df: pd.DataFrame, *keywords) -> str | None:
    """按关键字在列名里模糊找第一个命中列(容忍 AKShare 列名漂移)。"""
    for kw in keywords:
        for c in df.columns:
            if kw in str(c):
                return c
    return None


def _norm_code(x) -> str | None:
    s = "".join(ch for ch in str(x) if ch.isdigit())
    return s.zfill(6) if s else None


def _norm_date(x) -> str | None:
    """归一化披露/变动日期为 "YYYY-MM-DD";空/无法解析 → None(不编造)。

    两路增减持源(股东汇总 vs 董监高明细)日期列格式可能不一,按 code+日期对齐前先归一,
    保证「协议转让方式」只挂到披露日期真正匹配的减持记录上(防未来函数:不跨日错配)。
    """
    if x is None:
        return None
    s = str(x).strip()
    if s in ("", "nan", "None", "NaT"):
        return None
    try:
        return pd.to_datetime(s).strftime("%Y-%m-%d")
    except Exception:                                # noqa: BLE001
        return s or None


# ———————————————————— 业绩预告 / 快报 ————————————————————
def fetch_earnings_forecast(period: str, kind: str = "yjyg") -> pd.DataFrame:
    """拉某报告期业绩预告(kind=yjyg)或快报(kind=yjkb)并落盘。失败返回空 df。

    Args:
        period: 报告期 "YYYYMMDD"(如 "20240930")。
        kind: "yjyg"(预告)| "yjkb"(快报)。
    Returns:
        规整 df[code, 增速, 类型, 报告期];失败/无数据 → 空 df。
    """
    ak = _akshare()
    if ak is None:
        return pd.DataFrame()
    fn = {"yjyg": "stock_yjyg_em", "yjkb": "stock_yjkb_em"}.get(kind)
    try:
        raw = getattr(ak, fn)(date=period)
    except Exception as e:                       # noqa: BLE001
        logger.warning("%s(%s) 采集失败,降级: %s", fn, period, e)
        return pd.DataFrame()
    if raw is None or len(raw) == 0:
        return pd.DataFrame()

    code_col = _find_col(raw, "代码")
    # 增速优先"同比"/"变动幅度"/"净利润变动"等;快报可能是"净利润同比"
    growth_col = _find_col(raw, "同比", "变动幅度", "增长", "净利润变动")
    rows = []
    for _, r in raw.iterrows():
        code = _norm_code(r.get(code_col)) if code_col else None
        if not code:
            continue
        growth = None
        if growth_col is not None:
            try:
                growth = float(r.get(growth_col))
            except (TypeError, ValueError):
                growth = None
        rows.append({"code": code, "增速": growth, "类型": kind, "报告期": period})
    df = pd.DataFrame(rows)
    if not df.empty:
        try:
            store.put_raw(f"event_{kind}", period, df, meta={"source": "akshare-em"})
        except Exception as e:                   # noqa: BLE001
            logger.warning("事件缓存落盘失败(%s %s): %s", kind, period, e)
    logger.info("事件采集 %s %s:%d 条", kind, period, len(df))
    return df


def load_earnings(period: str, kind: str = "yjyg") -> pd.DataFrame:
    """读某报告期业绩预告/快报缓存。无缓存 → 空 df(不抛)。"""
    try:
        return store.get_raw(f"event_{kind}", period)
    except FileNotFoundError:
        return pd.DataFrame()


# ———————————————————— 高管/股东增减持 ————————————————————
def fetch_insider_trades(tag: str = "latest") -> pd.DataFrame:
    """拉高管/股东增减持(stock_ggcg_em)并落盘。失败返回空 df。

    Returns 规整 df[code, 方向(增持/减持), 变动股数, 方式, 日期];列名模糊匹配 + 降级。
    「方式」= 变动途径(如"协议转让"/"集中竞价"/"大宗交易"),供减持性质区分(协议转让给
    产业方 ≠ 二级市场抛售);源列缺失时为空,不影响其它字段。
    """
    ak = _akshare()
    if ak is None:
        return pd.DataFrame()
    try:
        raw = ak.stock_ggcg_em()
    except Exception as e:                       # noqa: BLE001
        logger.warning("stock_ggcg_em 采集失败,降级: %s", e)
        return pd.DataFrame()
    if raw is None or len(raw) == 0:
        return pd.DataFrame()

    code_col = _find_col(raw, "代码")
    dir_col = _find_col(raw, "变动方向", "增减")
    qty_col = _find_col(raw, "变动数量", "变动股数", "数量")
    method_col = _find_col(raw, "变动方式", "变动途径", "减持方式", "方式")
    date_col = _find_col(raw, "变动日期", "日期", "公告日")
    rows = []
    for _, r in raw.iterrows():
        code = _norm_code(r.get(code_col)) if code_col else None
        if not code:
            continue
        d = str(r.get(dir_col)) if dir_col else ""
        方向 = "增持" if ("增" in d) else ("减持" if ("减" in d) else None)
        qty = None
        if qty_col is not None:
            try:
                qty = float(r.get(qty_col))
            except (TypeError, ValueError):
                qty = None
        method = None
        if method_col is not None:
            mv = r.get(method_col)
            method = str(mv) if mv is not None and str(mv).strip() not in ("", "nan", "None") else None
        rows.append({"code": code, "方向": 方向, "变动股数": qty, "方式": method,
                     "日期": _norm_date(r.get(date_col)) if date_col else None,
                     "来源": "ggcg"})
    df = pd.DataFrame(rows)
    if not df.empty:
        try:
            store.put_raw("event_ggcg", tag, df, meta={"source": "akshare-em"})
        except Exception as e:                   # noqa: BLE001
            logger.warning("增减持缓存落盘失败: %s", e)
    logger.info("增减持采集:%d 条", len(df))
    return df


# 「方式」预期取值域(董监高明细「变动原因」实测枚举 + 股东汇总变动方式);取值合法性校验白名单。
# 实测东财 stock_hold_management_detail_em「变动原因」取值:集中竞价/大宗交易/协议转让/竞价交易/
# 二级市场买卖/盘后定价/询价转让/集合竞价/增持/减持…(可组合如"集中竞价,大宗交易")。
METHOD_VALUES = ("集中竞价", "集合竞价", "竞价交易", "集中交易", "竞价", "大宗交易", "大宗",
                 "协议转让", "股份转让", "股权转让", "询价转让", "盘后定价", "二级市场",
                 "增持", "减持", "承继", "继承", "赠与", "无偿划转", "划转", "要约",
                 "行权", "解除限售", "转融通", "司法", "拍卖", "质押", "解质")


def is_valid_method(m) -> bool:
    """「方式」取值是否落在预期枚举内(含组合值,如"集中竞价,大宗交易");供数据质量校验。"""
    if m is None:
        return False
    s = str(m).strip()
    if s in ("", "nan", "None"):
        return False
    return any(v in s for v in METHOD_VALUES)


def fetch_management_change(tag: str = "latest") -> pd.DataFrame:
    """拉董监高持股变动明细(stock_hold_management_detail_em)并落盘。失败返回空 df。

    该接口**自带「变动原因」列**(取值即"集中竞价/大宗交易/协议转让"等),正是 stock_ggcg_em
    缺的「方式」语义。规整 df[code, 方向, 变动股数, 方式, 日期, 变动人, 来源="mgmt"]。
    **口径局限(须诚实):** 覆盖为董监高(含相关人员),与 stock_ggcg_em 的大股东/机构增减持
    口径不完全重合——大股东协议转让减持不一定进此明细,故只能补一部分方式,非全覆盖。
    方向由「变动股数」符号推断(正=增持/负=减持;此接口无显式增减列)。
    """
    ak = _akshare()
    if ak is None:
        return pd.DataFrame()
    try:
        raw = ak.stock_hold_management_detail_em()
    except Exception as e:                       # noqa: BLE001
        logger.warning("stock_hold_management_detail_em 采集失败,降级: %s", e)
        return pd.DataFrame()
    if raw is None or len(raw) == 0:
        return pd.DataFrame()

    code_col = _find_col(raw, "代码")
    reason_col = _find_col(raw, "变动原因", "变动方式", "变动途径")
    qty_col = _find_col(raw, "变动股数", "变动数量")
    date_col = _find_col(raw, "变动日期", "日期", "公告日")
    person_col = _find_col(raw, "变动人", "董监高人员姓名", "姓名")
    rows = []
    for _, r in raw.iterrows():
        code = _norm_code(r.get(code_col)) if code_col else None
        if not code:
            continue
        qty = None
        if qty_col is not None:
            try:
                qty = float(r.get(qty_col))
            except (TypeError, ValueError):
                qty = None
        方向 = None
        if qty is not None and qty != 0:
            方向 = "增持" if qty > 0 else "减持"
        method = None
        if reason_col is not None:
            mv = r.get(reason_col)
            method = str(mv).strip() if mv is not None and str(mv).strip() not in ("", "nan", "None") else None
        rows.append({"code": code, "方向": 方向,
                     "变动股数": abs(qty) if qty is not None else None, "方式": method,
                     "日期": _norm_date(r.get(date_col)) if date_col else None,
                     "变动人": str(r.get(person_col)) if person_col else None,
                     "来源": "mgmt"})
    df = pd.DataFrame(rows)
    if not df.empty:
        try:
            store.put_raw("event_ggcg_mgmt", tag, df, meta={"source": "akshare-em-mgmt"})
        except Exception as e:                   # noqa: BLE001
            logger.warning("董监高变动缓存落盘失败: %s", e)
    nn = int(df["方式"].notna().sum()) if not df.empty else 0
    logger.info("董监高变动采集:%d 条(方式非空 %d)", len(df), nn)
    return df


def load_management_change(tag: str = "latest") -> pd.DataFrame:
    """读董监高变动明细缓存。无缓存 → 空 df(不抛)。"""
    try:
        return store.get_raw("event_ggcg_mgmt", tag)
    except FileNotFoundError:
        return pd.DataFrame()


def _merge_method_from_mgmt(base: pd.DataFrame, mgmt: pd.DataFrame) -> pd.DataFrame:
    """把董监高明细的「方式」按 code+日期严格对齐,富集进 base(股东增减持)的空「方式」;
    并把 base 里无对应(code+日期+方向)的董监高变动行**追加**进来(补董监高口径覆盖)。

    防未来函数:只按 code + 归一化日期**精确相等**对齐——同一披露日期才认为是同一事件,
    绝不用 mgmt 里日期更晚(未来)的方式去标注 base 的历史减持,也不跨日期错配他人方式。
    追加时按(code,日期,方向)去重,避免同一事件在两表里被重复计数放大信号。
    """
    if base is None:
        base = pd.DataFrame()
    if mgmt is None or mgmt.empty or "方式" not in mgmt.columns:
        return base
    mgmt = mgmt[mgmt["方式"].notna()].copy()
    if mgmt.empty:
        return base
    # code+日期 → 方式(取该 code+日期 下第一个非空方式)
    method_lookup: dict[tuple, str] = {}
    for _, r in mgmt.iterrows():
        key = (r.get("code"), r.get("日期"))
        if key[0] and key[1] and key not in method_lookup:
            method_lookup[key] = r.get("方式")

    if not base.empty:
        base = base.copy()
        if "方式" not in base.columns:
            base["方式"] = None
        # 1) 富集:base 里方式为空的行,按 code+日期查董监高方式回填
        def _fill(row):
            if row.get("方式") not in (None, "", "nan", "None"):
                return row.get("方式")
            return method_lookup.get((row.get("code"), row.get("日期")))
        base["方式"] = base.apply(_fill, axis=1)

    # 2) 追加:base 无对应(code,日期,方向)的董监高变动行(补覆盖,去重防重复计数)
    seen = set()
    if not base.empty:
        for _, r in base.iterrows():
            seen.add((r.get("code"), r.get("日期"), r.get("方向")))
    extra = []
    for _, r in mgmt.iterrows():
        key = (r.get("code"), r.get("日期"), r.get("方向"))
        if key in seen:
            continue
        seen.add(key)
        extra.append({"code": r.get("code"), "方向": r.get("方向"),
                      "变动股数": r.get("变动股数"), "方式": r.get("方式"),
                      "日期": r.get("日期"), "来源": "mgmt"})
    if extra:
        base = pd.concat([base, pd.DataFrame(extra)], ignore_index=True)
    return base


def load_insider_trades(tag: str = "latest", with_method: bool = True) -> pd.DataFrame:
    """读增减持缓存(股东汇总)。无缓存 → 空 df(不抛)。

    with_method=True(默认):若存在董监高变动明细缓存(event_ggcg_mgmt),把其「变动原因」按
    code+日期严格对齐富集进「方式」列、并追加董监高口径的变动行(见 _merge_method_from_mgmt),
    使下游减持性质区分(协议转让给战投 vs 二级抛售)真正拿到方式。缺明细缓存 → 原样返回(优雅降级)。
    """
    try:
        base = store.get_raw("event_ggcg", tag)
    except FileNotFoundError:
        base = pd.DataFrame()
    if not with_method:
        return base
    mgmt = load_management_change(tag)
    if (mgmt is None or mgmt.empty) and (base is None or base.empty):
        return base
    try:
        return _merge_method_from_mgmt(base, mgmt)
    except Exception as e:                       # noqa: BLE001
        logger.warning("增减持方式合并降级(返回原始股东汇总): %s", e)
        return base
