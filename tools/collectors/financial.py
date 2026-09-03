"""财报采集(P0 数值层):三大表(利润表/资产负债表/现金流量表)多报告期结构化抽取。

数据源(akshare 封装东财,per-stock、自带披露日,避开需扫全市场的 by-date 接口):
  - `stock_profit_sheet_by_report_em(symbol)`     利润表
  - `stock_balance_sheet_by_report_em(symbol)`    资产负债表
  - `stock_cash_flow_sheet_by_report_em(symbol)`  现金流量表
每张表按**报告期**给全科目,并带 `REPORT_DATE`(报告期)+ `NOTICE_DATE`(**披露日**,
无未来函数锚点)+ `REPORT_TYPE`(一季报/中报/三季报/年报)+ `OPINION_TYPE`(审计意见,
仅年报有值 → 供后续审计闸门,本轮不判)。

**健壮降级(约法2/collectors 惯例)**:任一张表 / 任一票失败 → 记 logger、跳过,绝不炸整批。
三张表按报告期对齐合并;单张表缺失 → 该表科目留空,其余表照常产出。

落盘:`store.put_raw("financial_report", code, payload)`,payload 内层按 period 索引
(一个 code 一份多报告期字典)。衍生/红旗/评分在 analysis.financial.analyzer 消费,不在此算。

⚠️ 非投资建议。审计闸门 / LLM 文本定性 / 三大表深度勾稽本轮不实现(见 analyzer TODO)。
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import time

import pandas as pd

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.financial")

_SOURCE = "akshare-em(三大表 by_report)"

# —— 单张表拉取超时(秒)——akshare/requests 底层不暴露 timeout,单只票连接挂起会
#   无限阻塞、拖垮整条盘后闭环(实测:某票财报采集挂死 → 后续 serialize 永不执行 → 记录=0)。
#   用工作线程包一层硬超时:超时即当作该表失败降级(与"任一表失败跳过、不炸整批"一致)。
_FETCH_TIMEOUT_SEC = int(os.environ.get("FIN_FETCH_TIMEOUT_SEC", "45"))

# —— 报告期回溯期数(近 12 期≈3 年,够算增速趋势与周转;方案 Q2 默认)——
DEFAULT_PERIODS = 12

# —— EM 英文科目 → 中文字段(仅取分析必需的关键科目,原始表 200+ 列不全落)——
# 利润表
_PROFIT_MAP = {
    "营业总收入": "TOTAL_OPERATE_INCOME", "营业收入": "OPERATE_INCOME",
    "营业总成本": "TOTAL_OPERATE_COST", "营业成本": "OPERATE_COST",
    "销售费用": "SALE_EXPENSE", "管理费用": "MANAGE_EXPENSE",
    "研发费用": "RESEARCH_EXPENSE", "财务费用": "FINANCE_EXPENSE",
    "利息费用": "FE_INTEREST_EXPENSE", "利息收入": "FE_INTEREST_INCOME",
    # 减值(通用/非金融口径):新利润表用 *_INCOME(收入符号制,负数=损失);旧字段 *_LOSS 对
    #   非金融票恒 NaN(实测格力/仲景 _LOSS 全空、_INCOME 有值)——此前误采 _LOSS 致减值全 null。
    "资产减值损失": "ASSET_IMPAIRMENT_INCOME", "信用减值损失": "CREDIT_IMPAIRMENT_INCOME",
    "公允价值变动收益": "FAIRVALUE_CHANGE_INCOME", "投资收益": "INVEST_INCOME",
    "营业利润": "OPERATE_PROFIT", "利润总额": "TOTAL_PROFIT", "所得税": "INCOME_TAX",
    "净利润": "NETPROFIT", "归母净利润": "PARENT_NETPROFIT",
    "扣非归母净利润": "DEDUCT_PARENT_NETPROFIT", "基本每股收益": "BASIC_EPS",
    # —— 银行/金融专属科目(非金融票为空;金融票营收=OPERATE_INCOME,营业总收入 TOTAL_* 常空)——
    #   银行减值用 *_LOSS(正数=计提额,与通用相反),故独立字段、不与通用减值混符号制。
    "利息净收入": "INTEREST_NI", "利息收入_金融": "INTEREST_INCOME",
    "利息支出_金融": "INTEREST_EXPENSE",
    "手续费及佣金净收入": "FEE_COMMISSION_NI",
    "业务及管理费": "BUSINESS_MANAGE_EXPENSE",
    "信用减值损失_金融": "CREDIT_IMPAIRMENT_LOSS", "资产减值损失_金融": "ASSET_IMPAIRMENT_LOSS",
}
# 资产负债表
_BALANCE_MAP = {
    "货币资金": "MONETARYFUNDS", "应收账款": "ACCOUNTS_RECE", "应收票据及应收账款": "NOTE_ACCOUNTS_RECE",
    "存货": "INVENTORY", "商誉": "GOODWILL", "合同负债": "CONTRACT_LIAB",
    # 银行/金融专属:发放贷款及垫款 / 吸收存款(存贷比、规模口径;非金融票为空)
    "发放贷款及垫款": "LOAN_ADVANCE", "吸收存款": "ACCEPT_DEPOSIT",
    "预收账款": "ADVANCE_RECEIVABLES", "应付账款": "ACCOUNTS_PAYABLE",
    "其他应收款合计": "TOTAL_OTHER_RECE", "在建工程": "CIP", "固定资产": "FIXED_ASSET",
    "无形资产": "INTANGIBLE_ASSET", "开发支出": "DEVELOP_EXPENSE",
    "短期借款": "SHORT_LOAN", "长期借款": "LONG_LOAN", "应付债券": "BOND_PAYABLE",
    "一年内到期非流动负债": "NONCURRENT_LIAB_1YEAR",
    "流动资产合计": "TOTAL_CURRENT_ASSETS", "流动负债合计": "TOTAL_CURRENT_LIAB",
    "资产总计": "TOTAL_ASSETS", "负债合计": "TOTAL_LIABILITIES",
    "股东权益合计": "TOTAL_EQUITY", "归母股东权益": "TOTAL_PARENT_EQUITY",
}
# 现金流量表
_CASHFLOW_MAP = {
    "销售商品提供劳务收到的现金": "SALES_SERVICES",
    "经营活动现金流入小计": "TOTAL_OPERATE_INFLOW",
    "经营活动现金流出小计": "TOTAL_OPERATE_OUTFLOW",
    "经营活动现金流量净额": "NETCASH_OPERATE",
    "投资活动现金流量净额": "NETCASH_INVEST",
    "筹资活动现金流量净额": "NETCASH_FINANCE",
    "购建固定资产无形资产等支付现金": "CONSTRUCT_LONG_ASSET",
    "固定资产折旧": "FA_IR_DEPR", "无形资产摊销": "IA_AMORTIZE",
    "分配股利利润偿付利息支付现金": "ASSIGN_DIVIDEND_PORFIT",
    "期末现金及现金等价物余额": "END_CCE",
}

_TABLES = (
    ("利润表", "stock_profit_sheet_by_report_em", _PROFIT_MAP),
    ("资产负债表", "stock_balance_sheet_by_report_em", _BALANCE_MAP),
    ("现金流量表", "stock_cash_flow_sheet_by_report_em", _CASHFLOW_MAP),
)


def _akshare():
    """惰性导入 akshare;未安装 → None(降级)。"""
    try:
        import akshare as ak
        return ak
    except Exception as e:                       # noqa: BLE001
        logger.warning("akshare 不可用,财报采集降级: %s", e)
        return None


def _to_float(v):
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _em_symbol(code: str) -> str:
    """6 位代码 → 东财 by_report 接口所需带市场前缀符号(SH/SZ/BJ)。"""
    c = str(code).zfill(6)
    if c[0] == "6":
        return "SH" + c
    if c.startswith("920") or c[0] in ("4", "8"):  # 北交所(920 段为现行代码段,须先于 0/3 兜底)
        return "BJ" + c
    return "SZ" + c                               # 0/3 开头(深主板/创业板)


def _norm_date(v) -> str | None:
    """'2025-09-30 00:00:00' / Timestamp → 'YYYY-MM-DD';不可解析 → None。"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s[:10] if len(s) >= 10 else None


def _report_type(report_date: str | None) -> str | None:
    """由报告期结束月推断报告类型(接口 REPORT_TYPE 兜底)。"""
    if not report_date:
        return None
    mmdd = report_date[5:10]
    return {"03-31": "一季报", "06-30": "半年报", "09-30": "三季报", "12-31": "年报"}.get(mmdd)


def _extract_row(row: pd.Series, colmap: dict) -> dict:
    """按 colmap 从一行抽取中文字段(缺列/缺值 → None,不 KeyError)。"""
    out = {}
    for cn, en in colmap.items():
        out[cn] = _to_float(row[en]) if en in row.index else None
    return out


def _fetch_one_table(ak, fn: str, symbol: str) -> pd.DataFrame | None:
    """拉单张表;失败/空/超时 → None(降级,不抛)。

    akshare 底层不暴露 timeout,单只连接挂起会无限阻塞整条闭环 → 用单工作线程包硬超时
    (`_FETCH_TIMEOUT_SEC`):超时即降级返回 None,挂起的线程随进程退出回收(不 join,
    避免继续阻塞)。这样一只票的网络挂死最多拖 45s,不会再卡死盘后 serialize。
    """
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(getattr(ak, fn), symbol=symbol)
    try:
        df = fut.result(timeout=_FETCH_TIMEOUT_SEC)
    except concurrent.futures.TimeoutError:
        logger.warning("%s(%s) 采集超时(>%ds),该表降级", fn, symbol, _FETCH_TIMEOUT_SEC)
        ex.shutdown(wait=False, cancel_futures=True)
        return None
    except Exception as e:                       # noqa: BLE001
        logger.warning("%s(%s) 采集失败,该表降级: %s", fn, symbol, e)
        ex.shutdown(wait=False)
        return None
    else:
        ex.shutdown(wait=False)
    if df is None or len(df) == 0 or "REPORT_DATE" not in df.columns:
        return None
    return df


def _merge_tables(dfs: dict[str, pd.DataFrame | None], periods: int) -> dict[str, dict]:
    """三张表按报告期对齐,取最近 `periods` 期,组装 {period: 单期记录}。

    单期记录:{report_date, disclosure_date, report_type, is_forecast,
              audit_opinion, 利润表{}, 资产负债表{}, 现金流量表{}}。
    披露日取三表中最早的 NOTICE_DATE(同一期不同表披露日一致,取到即可)。
    """
    by_period: dict[str, dict] = {}
    # 收集所有出现过的报告期(任一表有即算)
    all_periods: set[str] = set()
    for df in dfs.values():
        if df is None:
            continue
        for v in df["REPORT_DATE"]:
            p = _norm_date(v)
            if p:
                all_periods.add(p)
    keep = sorted(all_periods, reverse=True)[:periods]

    for tbl_name, colmap in ((n, m) for n, _, m in _TABLES):
        df = dfs.get(tbl_name)
        if df is None:
            continue
        idx = {_norm_date(v): i for i, v in enumerate(df["REPORT_DATE"])}
        for p in keep:
            if p not in idx:
                continue
            row = df.iloc[idx[p]]
            rec = by_period.setdefault(p, {
                "report_date": p, "disclosure_date": None, "report_type": _report_type(p),
                "is_forecast": False, "audit_opinion": None,
                "利润表": {}, "资产负债表": {}, "现金流量表": {},
            })
            rec[tbl_name] = _extract_row(row, colmap)
            # 披露日:取最早非空(三表应一致)
            nd = _norm_date(row["NOTICE_DATE"]) if "NOTICE_DATE" in row.index else None
            if nd and (rec["disclosure_date"] is None or nd < rec["disclosure_date"]):
                rec["disclosure_date"] = nd
            # 报告类型(接口值优先)/审计意见(年报有值,供后续闸门)
            if "REPORT_TYPE" in row.index and isinstance(row["REPORT_TYPE"], str):
                rt = {"中报": "半年报"}.get(row["REPORT_TYPE"], row["REPORT_TYPE"])
                rec["report_type"] = rt or rec["report_type"]
            op = row["OPINION_TYPE"] if "OPINION_TYPE" in row.index else None
            if isinstance(op, str) and op and op != "未经审计" and rec["audit_opinion"] is None:
                rec["audit_opinion"] = op
    return by_period


def fetch_financial(codes: list[str], periods: int = DEFAULT_PERIODS,
                    as_of: str | None = None) -> dict[str, dict]:
    """批量采集三大表并落盘。返回 {code: payload}。

    Args:
        codes: 6 位代码列表。
        periods: 回溯报告期数(默认 12≈3 年)。
        as_of: 仅作元数据留痕(采集不按 as_of 截断;可见性过滤在分析层按披露日做)。
    单票整体失败记 logger 并跳过,不中断整批。
    """
    settings.ensure_dirs()
    ak = _akshare()
    out: dict[str, dict] = {}
    failed: list[str] = []
    n = len(codes)
    for i, code in enumerate(codes, 1):
        code = str(code).zfill(6)
        logger.info("[%d/%d] 财报 %s 采集...", i, n, code)
        if ak is None:
            failed.append(code)
            continue
        try:
            symbol = _em_symbol(code)
            dfs: dict[str, pd.DataFrame | None] = {}
            name = None
            for tbl_name, fn, _ in _TABLES:
                df = _fetch_one_table(ak, fn, symbol)
                dfs[tbl_name] = df
                if df is not None and name is None and "SECURITY_NAME_ABBR" in df.columns:
                    name = str(df.iloc[0]["SECURITY_NAME_ABBR"])
                time.sleep(settings.FETCH_SLEEP_SEC)
            if all(v is None for v in dfs.values()):
                raise ValueError("三表全空(可能被限流/代码无效)")
            by_period = _merge_tables(dfs, periods)
            if not by_period:
                raise ValueError("无可用报告期")
            payload = {"code": code, "name": name, "periods": by_period,
                       "n_periods": len(by_period)}
            store.put_raw("financial_report", code, payload, meta={"source": _SOURCE})
            out[code] = payload
            logger.info("财报 %s 落盘(%d 期,最新 %s)", code, len(by_period),
                        max(by_period) if by_period else "-")
        except Exception as e:                   # noqa: BLE001
            failed.append(code)
            logger.error("财报 %s 失败: %s", code, e)
    if failed:
        logger.warning("财报采集失败(%d): %s", len(failed), failed)
    return out


def load_financial(code: str) -> dict:
    """读单票财报 raw(多报告期)。缓存缺失抛 FileNotFoundError。"""
    return store.get_raw("financial_report", str(code).zfill(6))
