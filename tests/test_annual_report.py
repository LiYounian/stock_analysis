"""年报 PDF 采集层单测(M2)。不触网:喂合成年报全文测切段/URL/正报筛选。

锁语义(约法6):
  - 详情页URL → 静态PDF URL 规律。
  - 正报年报筛选(排除英文版/摘要)。
  - 章节切分:MD&A 取正文节(非目录一行)、审计段含事务所名+意见、风险段命中。
"""
from tools.collectors import annual_report as ar


def test_pdf_url_from_detail():
    detail = ("http://www.cninfo.com.cn/new/disclosure/detail?stockCode=600519"
              "&announcementId=1225114741&orgId=gssh0600519&announcementTime=2026-04-17")
    assert ar._pdf_url(detail) == "http://static.cninfo.com.cn/finalpage/2026-04-17/1225114741.PDF"
    assert ar._pdf_url("http://x/no-id") is None


def test_is_main_annual_filters_variants():
    assert ar._is_main_annual("贵州茅台2025年年度报告")
    assert not ar._is_main_annual("贵州茅台2025年年度报告（英文版）")
    assert not ar._is_main_annual("贵州茅台2025年年度报告摘要")
    assert not ar._is_main_annual("关于2024年年度报告的更正公告")
    assert not ar._is_main_annual("2025年第一季度报告")


_SYNTH = """封面 目录
第一节 释义
管理层讨论与分析 …… 审计报告 …… 财务报告   （目录里的一行行标题,不该被当正文）
第三节 管理层讨论与分析
一、报告期内公司从事的业务情况 公司主要业务是白酒生产与销售，报告期营收增长。
二、核心竞争力分析 品牌与渠道。 三、公司未来发展的展望 (四)可能面对的风险 市场需求波动风险、原材料价格风险、政策风险。
第十节 财务报告
审计报告 天健会计师事务所（特殊普通合伙）接受委托，审计了本公司财务报表。
审计意见 我们认为，上述财务报表在所有重大方面按照企业会计准则的规定编制，公允反映——标准无保留意见。
"""


def test_extract_sections_picks_body_not_toc():
    secs = ar.extract_sections(_SYNTH)
    # MD&A 取正文节(第三节),含真实业务描述,不是目录那一行
    assert secs["MD&A"] and "报告期内公司从事的业务情况" in secs["MD&A"]
    # 审计段含事务所名 + 审计意见
    assert secs["审计报告"] and "会计师事务所" in secs["审计报告"] and "审计意见" in secs["审计报告"]
    # 风险段命中风险表述
    assert secs["风险"] and "风险" in secs["风险"]


def test_extract_sections_degrades_on_empty():
    secs = ar.extract_sections("无任何章节的空文本")
    assert secs == {"审计报告": None, "MD&A": None, "风险": None}
