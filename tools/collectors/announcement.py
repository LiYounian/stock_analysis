"""公告采集:公司内部行为(情绪三层之「公司」层的结构化基座)。

数据源:巨潮 `stock_zh_a_disclosure_report_cninfo`(本机实测可用,权威、非东财)。
只拿到标题/时间/链接(无正文),按标题关键词**规则打标**分类 + 粗判影响方向。
正文级定性(利好/利空强度)留给 P2-C 的 LLM(event.py)。
落盘:走 store 层(kind="announcement",json),旁记 meta.source="cninfo"。
契约见 docs/计划/P2_结构化情绪与基本面.md。
"""
from __future__ import annotations

import logging
import time
import warnings

import pandas as pd

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.announcement")

_SOURCE = "cninfo"  # 巨潮

# 标题关键词 → 类型(顺序即优先级,先命中先归类)
_TYPE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("回购",), "回购"),
    (("增持",), "增持"),
    (("减持",), "减持"),
    (("业绩预告", "预增", "预减", "预亏", "扭亏", "首亏"), "业绩预告"),
    (("业绩快报",), "业绩快报"),
    (("中标", "中标通知", "重大合同", "签订", "框架协议", "订单", "采购"), "合同订单"),
    (("诉讼", "仲裁", "起诉"), "诉讼仲裁"),
    (("股权激励", "员工持股", "激励计划", "股票期权", "限制性股票"), "股权激励"),
    (("向特定对象发行", "非公开发行", "定增", "可转债", "配股", "募集资金"), "再融资"),
    (("权益变动", "一致行动人"), "权益变动"),
    (("解除限售", "解禁", "限售股上市"), "解禁"),
    (("质押", "冻结"), "股权质押"),
    (("分红", "利润分配", "派息", "现金分红"), "分红"),
    (("投资者关系", "调研", "问询函", "关注函", "监管"), "监管/调研"),
    (("异常波动", "交易异常"), "交易异动"),
    (("定期报告", "年度报告", "季度报告", "半年度报告", "第一季度", "第三季度"), "定期报告"),
]
# 影响方向粗判(仅标题关键词,细判交 P2-C LLM)
_BULLISH = ("增持", "回购", "预增", "扭亏", "中标", "重大合同", "订单", "股权激励")
_BEARISH = ("减持", "预减", "预亏", "首亏", "诉讼", "仲裁", "被起诉", "质押", "冻结")


def classify_title(title: str) -> str:
    """按标题关键词归类型。无命中 → 其他。"""
    for kws, typ in _TYPE_RULES:
        if any(k in title for k in kws):
            return typ
    return "其他"


def impact_hint(title: str) -> str:
    """粗判影响方向:利好/利空/待判(仅标题启发,非定论)。"""
    bull = any(k in title for k in _BULLISH)
    bear = any(k in title for k in _BEARISH)
    if bull and not bear:
        return "利好"
    if bear and not bull:
        return "利空"
    return "待判"


def _fetch_cninfo(code: str, start: str, end: str) -> pd.DataFrame:
    import akshare as ak
    with warnings.catch_warnings():          # 屏蔽 akshare 内部 SettingWithCopyWarning
        warnings.simplefilter("ignore")
        return ak.stock_zh_a_disclosure_report_cninfo(
            symbol=code, market="沪深京", start_date=start, end_date=end)


def fetch_announcements(codes: list[str], days: int = None) -> dict[str, list[dict]]:
    """拉取近 days 天公告并规则打标落盘。

    输出:{code: [{date, title, type, impact, url}, ...]}(按时间倒序)。
    单票失败记 logger 并跳过,不中断整批。
    """
    settings.ensure_dirs()
    days = days or settings.NEWS_LOOKBACK_DAYS
    start = (pd.Timestamp.today() - pd.Timedelta(days=days)).strftime("%Y%m%d")
    end = pd.Timestamp.today().strftime("%Y%m%d")

    from tools.config import stock_pool

    out: dict[str, list[dict]] = {}
    failed: list[str] = []
    n = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] 公告 %s 采集...", i, n, code)
        try:
            if stock_pool.is_hk(code):
                # 巨潮不支持港股,降级为空(港股重大事项由新闻源覆盖)
                items = []
                store.put_raw("announcement", code, items, meta={"source": "none(hk)"})
            else:
                df = _fetch_cninfo(code, start, end)
                items = []
                if df is not None and len(df):
                    for _, r in df.iterrows():
                        title = str(r.get("公告标题", ""))
                        items.append({
                            "date": str(r.get("公告时间", ""))[:10],
                            "title": title,
                            "type": classify_title(title),
                            "impact": impact_hint(title),
                            "url": r.get("公告链接", ""),
                        })
                    items.sort(key=lambda x: x["date"], reverse=True)
                store.put_raw("announcement", code, items, meta={"source": _SOURCE})
            out[code] = items
            logger.info("公告 %s:%d 条", code, len(items))
        except Exception as e:
            failed.append(code)
            logger.error("公告 %s 失败: %s", code, e)
        time.sleep(settings.FETCH_SLEEP_SEC)
    if failed:
        logger.warning("公告拉取失败(%d): %s", len(failed), failed)
    return out


def load_announcements(code: str) -> list[dict]:
    """从本地缓存读单票公告。缓存缺失抛 FileNotFoundError。"""
    return store.get_raw("announcement", code)
