"""财报 LLM 文本层(M2 分析层):年报 MD&A/风险段 → 定性(schema_A) + 综合归纳(schema_B)。

分工硬边界(方案 §2.1):数值全走代码(financial.metrics),**LLM 只碰文本**。
资源纪律:**只对给定子集跑**(screenall 传 自选∪每策略前5),内容哈希缓存免重烧;
LLM 未配置 / 无年报文本 → 降级 None(与 M1 一致,不阻断)。
分层:`build_financial_block` 只读本层预算的 code_view `financial_text`,**不触发 LLM**——
避免对每票 serialize 都烧 token。
无未来函数:只用 disclosure_date <= as_of 的年报(由 run_financial_text 过滤)。
"""
from __future__ import annotations

import hashlib
import json
import logging

from tools.config import settings
from tools.llm import client as lc
from tools.llm import prompts
from tools.store import repo as store

logger = logging.getLogger("analysis.financial.llm_text")

_MAX_TEXT = 16000        # 送 LLM 的正文上限(MD&A+风险 拼接后截断,控 token)


def _cached_extract(client, text: str, instruction: str, schema: dict) -> dict:
    """按 (指令+文本) hash 缓存 LLM 抽取(与 event._cached_extract 同缓存目录/口径,跨天免重烧)。"""
    settings.LLM_CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5((instruction + "||" + text).encode("utf-8")).hexdigest()
    p = settings.LLM_CACHE / f"{key}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    r = client.extract(text, schema, instruction=instruction)
    p.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    return r


def _numeric_digest(block: dict | None) -> str:
    """把 M1 数值块压成一段喂给 verdict 的数值上下文(不含大数组)。"""
    if not block:
        return "(无数值块)"
    prof = block.get("利润表摘要") or {}
    dims = block.get("five_dims") or {}
    return (f"数值评级={block.get('评级')} quality={block.get('quality_score')}; "
            f"利润表:营收增速={prof.get('营收增速')} 扣非增速={prof.get('扣非增速')} "
            f"毛利率={prof.get('毛利率')} 净利率={prof.get('净利率')}; "
            f"五维={dims}; 红旗={block.get('flags')}; "
            f"审计意见闸门={block.get('审计意见闸门')} 审计机构闸门={block.get('审计机构闸门')}")


def analyze_text(code: str, name: str, sections: dict, block: dict | None = None,
                 client=None) -> dict:
    """对单票年报文本做 schema_A(定性)+ schema_B(综合归纳)。

    sections: annual_report_text['段落'](MD&A/风险)。block: M1 数值块(供 verdict 上下文)。
    LLM 未配置 → {qualitative: None, verdict: None}。无 MD&A/风险文本 → qualitative=None。
    """
    if not lc.is_configured():
        return {"qualitative": None, "verdict": None}
    mda = (sections or {}).get("MD&A") or ""
    risk = (sections or {}).get("风险") or ""
    text = (mda + "\n【风险】\n" + risk).strip()[:_MAX_TEXT]
    if not text.replace("【风险】", "").strip():             # 无实际正文 → 不建 client、不烧 token
        return {"qualitative": None, "verdict": None}
    client = client or lc.get_client()

    qualitative = None
    try:
        qualitative = _cached_extract(
            client, text, prompts.financial_qualitative_instruction(name, code),
            prompts.FINANCIAL_QUALITATIVE_SCHEMA)
    except Exception as e:                                   # noqa: BLE001
        logger.warning("%s 财报定性抽取失败:%s", code, e)

    verdict = None
    try:
        vtext = f"【数值】{_numeric_digest(block)}\n【文本定性】{json.dumps(qualitative, ensure_ascii=False)}"
        verdict = _cached_extract(
            client, vtext, prompts.financial_verdict_instruction(name, code),
            prompts.FINANCIAL_VERDICT_SCHEMA)
    except Exception as e:                                   # noqa: BLE001
        logger.warning("%s 财报综合归纳失败:%s", code, e)

    return {"qualitative": qualitative, "verdict": verdict}


def run_financial_text(codes: list[str], as_of: str | None = None) -> int:
    """对子集逐票跑文本层 → 落 code_view `financial_text`(供 build_financial_block 读取)。

    只处理有年报文本(disclosure<=as_of)的票;串行、缓存;LLM 未配置 → 直接返回 0。
    返回成功产出条数。
    """
    if not lc.is_configured():
        logger.info("LLM 未配置,财报文本层跳过(qualitative/verdict 保持 null)")
        return 0
    from tools.analysis.financial import analyzer as fr_analyzer
    n = 0
    for code in codes:
        code = str(code).zfill(6)
        try:
            ar_raw = store.get_raw("annual_report_text", code)
        except FileNotFoundError:
            continue
        if as_of is not None and (ar_raw.get("disclosure_date") or "") > as_of:
            continue                                        # 无未来函数:未披露不可见
        block = fr_analyzer.build_financial_block(code, as_of=as_of)   # 数值块(不含 LLM)
        res = analyze_text(code, ar_raw.get("name", code), ar_raw.get("段落") or {}, block)
        if res["qualitative"] is None and res["verdict"] is None:
            continue
        store.put_code_view("financial_text", code, {
            "code": code, "as_of": as_of, "年度": ar_raw.get("年度"),
            "qualitative": res["qualitative"], "verdict": res["verdict"]})
        n += 1
        logger.info("[%d] %s 财报文本层完成(定性%s/归纳%s)", n, code,
                    "有" if res["qualitative"] else "无", "有" if res["verdict"] else "无")
    return n
