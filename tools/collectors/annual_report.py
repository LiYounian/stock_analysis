"""年报 PDF 采集(M2 采集层):cninfo 取最新年报 PDF → pymupdf 抽全文 → 切目标章节 → 落 raw。

分层定位:**采集层**——只负责"拿到并切出年报的审计报告/MD&A/风险三段文本",不做任何判断。
下游:闸门1(analysis.financial.audit_gate 从"审计报告"段抽事务所名)、LLM 文本层
(analysis.financial.llm_text 喂 MD&A/风险段)。数值层(financial.py)不依赖本模块。

资源纪律:只对给定 code 列表、只最新一份"正报"年报、内容哈希缓存(同一年报不重下)、
串行 + 单份大小上限、失败降级不中断(约法2)。不全A、不并发。pymupdf 缺 → 整体降级。
无未来函数:年报以**披露日**锚定(payload 带 disclosure_date),下游按 as_of 过滤。
"""
from __future__ import annotations

import hashlib
import logging
import re
import socket
import time
import urllib.request

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.annual_report")

_FETCH_TIMEOUT = 30                      # 单次网络请求超时(秒)
_PDF_MAX_BYTES = 30 * 1024 * 1024        # 单份年报 PDF 上限 30MB,超限跳过(防异常大文件吃内存)
_MDA_CAP = 25000                         # MD&A 段字符上限(喂 LLM 只取两章、不灌整本)
_RISK_CAP = 8000
_AUDIT_CAP = 8000

_SEC_RE = re.compile(r"第[一二三四五六七八九十]{1,3}节\s*([^\n]{1,40})")
_AID_RE = re.compile(r"announcementId=(\d+)")
_ATIME_RE = re.compile(r"announcementTime=([\d-]+)")


def _is_main_annual(title: str) -> bool:
    """正报年报:排除 英文版/摘要/更正/已取消 等非正文。"""
    t = title or ""
    return ("年度报告" in t) and not any(x in t for x in ("英文", "摘要", "更正", "取消", "已取消"))


def _pdf_url(detail_url: str) -> str | None:
    """cninfo 详情页 URL → 静态 PDF URL(finalpage/{时间}/{id}.PDF)。"""
    aid = _AID_RE.search(detail_url or "")
    atime = _ATIME_RE.search(detail_url or "")
    if not (aid and atime):
        return None
    return f"http://static.cninfo.com.cn/finalpage/{atime.group(1)}/{aid.group(1)}.PDF"


def _section_blocks(full: str) -> list[tuple[str, str]]:
    """按"第N节"切块 → [(节标题, 该节全文)];无节标题 → 空列表(走兜底窗口)。"""
    ms = list(_SEC_RE.finditer(full))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(full)
        out.append((m.group(1).strip(), full[m.start():end]))
    return out


def _longest_block(blocks: list[tuple[str, str]], *keywords: str) -> str | None:
    """标题含任一关键词的节里取最长的(避开目录里的一行标题,拿真正文)。"""
    cands = [body for title, body in blocks if any(k in title for k in keywords)]
    return max(cands, key=len) if cands else None


def _window_around(full: str, pat: str, before: int = 200, after: int = 6000) -> str | None:
    """在正文里定位关键词(取**最后一次**出现,避开目录),返回其周边窗口。"""
    idx = full.rfind(pat)
    if idx < 0:
        return None
    return full[max(0, idx - before): idx + after]


def extract_sections(full: str) -> dict[str, str | None]:
    """从年报全文切出 审计报告 / MD&A / 风险 三段(各有上限;缺则 None)。"""
    blocks = _section_blocks(full)

    # MD&A:节标题含"管理层讨论与分析/经营情况讨论与分析"的最长节
    mda = _longest_block(blocks, "管理层讨论与分析", "经营情况讨论与分析")
    if not mda:
        mda = _window_around(full, "管理层讨论与分析", before=0, after=_MDA_CAP) \
            or _window_around(full, "经营情况讨论与分析", before=0, after=_MDA_CAP)
    mda = mda[:_MDA_CAP] if mda else None

    # 审计报告:财务报告节头部含"审计报告→会计师事务所→审计意见";取财务报告最长节头部,
    # 或兜底用事务所签名窗口(P2.2 正则从此段抽名)。
    fin_block = _longest_block(blocks, "财务报告", "审计报告")
    if fin_block:
        audit = fin_block[:_AUDIT_CAP]
    else:
        audit = _window_around(full, "会计师事务所", before=1500, after=1500) \
            or _window_around(full, "审计报告", before=0, after=_AUDIT_CAP)
    audit = audit[:_AUDIT_CAP] if audit else None

    # 风险:MD&A 内的"风险"子节(展望/可能面对的风险常在 MD&A 末段);兜底全文窗口。
    risk = None
    if mda:
        ridx = mda.rfind("风险")
        if ridx >= 0:
            risk = mda[max(0, ridx - 200): ridx + _RISK_CAP]
    if not risk:
        risk = _window_around(full, "可能面对的风险", before=200, after=_RISK_CAP) \
            or _window_around(full, "公司面临的风险", before=200, after=_RISK_CAP)
    risk = risk[:_RISK_CAP] if risk else None

    return {"审计报告": audit, "MD&A": mda, "风险": risk}


def _download_pdf(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as r:
        data = r.read(_PDF_MAX_BYTES + 1)
    if len(data) > _PDF_MAX_BYTES:
        logger.warning("年报 PDF 超限(>%dMB)跳过:%s", _PDF_MAX_BYTES // 1024 // 1024, url)
        return None
    return data


def fetch_annual_report(codes: list[str], years_back: int = 2) -> dict[str, dict]:
    """对 codes 采最新"正报"年报 PDF → 切段 → 落 raw kind `annual_report_text`(键=code)。

    只取最新一份;内容哈希缓存(pdf 内容未变则不重抽);任一票失败降级跳过、不中断整批。
    返回 {code: payload}(payload 含 disclosure_date/pdf_url/年度/段落{审计报告,MD&A,风险})。
    """
    try:
        import akshare as ak
        import pymupdf
    except ImportError as e:
        logger.warning("缺依赖(%s),年报采集整体降级(下游 LLM/闸门1 保持 null)", e)
        return {}

    from datetime import datetime
    end = datetime.now().strftime("%Y%m%d")
    start = f"{datetime.now().year - years_back}0101"
    _old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_FETCH_TIMEOUT)
    out: dict[str, dict] = {}
    try:
        for code in codes:
            code = str(code).zfill(6)
            time.sleep(settings.FETCH_SLEEP_SEC)                 # 串行礼貌间隔(资源纪律)
            try:
                df = ak.stock_zh_a_disclosure_report_cninfo(
                    symbol=code, market="沪深京", category="年报",
                    start_date=start, end_date=end)
            except Exception as e:                              # noqa: BLE001
                logger.warning("%s 年报清单获取失败:%s", code, e)
                continue
            if df is None or len(df) == 0:
                logger.info("%s 无年报清单", code)
                continue
            mains = df[df["公告标题"].map(_is_main_annual)].sort_values("公告时间", ascending=False)
            if len(mains) == 0:
                continue
            row = mains.iloc[0]
            title, disc = row["公告标题"], str(row["公告时间"])[:10]
            ym = re.search(r"(\d{4})\s*年", title)
            year = ym.group(1) if ym else disc[:4]
            pdf_url = _pdf_url(row["公告链接"])
            if not pdf_url:
                logger.warning("%s 年报无法构造 PDF url", code)
                continue
            # 内容缓存:已存同一 pdf_url 且解析过 → 跳过重下
            try:
                prev = store.get_raw("annual_report_text", code)
                if prev and prev.get("pdf_url") == pdf_url and prev.get("段落"):
                    out[code] = prev
                    logger.info("%s 年报缓存命中(%s),跳过重下", code, year)
                    continue
            except FileNotFoundError:
                pass
            try:
                data = _download_pdf(pdf_url)
                if not data:
                    continue
                doc = pymupdf.open(stream=data, filetype="pdf")
                full = "\n".join(doc[i].get_text() for i in range(doc.page_count))
                doc.close()
            except Exception as e:                              # noqa: BLE001
                logger.warning("%s 年报 PDF 下载/解析失败:%s", code, e)
                continue
            secs = extract_sections(full)
            payload = {
                "code": code, "name": row["简称"], "年度": year,
                "disclosure_date": disc, "pdf_url": pdf_url,
                "全文hash": hashlib.sha256(full.encode("utf-8")).hexdigest()[:16],
                "段落": secs,
                "段长": {k: (len(v) if v else 0) for k, v in secs.items()},
            }
            store.put_raw("annual_report_text", code, payload)
            out[code] = payload
            logger.info("%s 年报采集 %s(%d页→审计%d/MD&A%d/风险%d字)", code, year,
                        full.count("\n"), *(payload["段长"][k] for k in ("审计报告", "MD&A", "风险")))
    finally:
        socket.setdefaulttimeout(_old)
    return out


def load_annual_report(code: str) -> dict:
    """读已采年报文本(缺 → FileNotFoundError,与 store 约定一致)。"""
    return store.get_raw("annual_report_text", str(code).zfill(6))
