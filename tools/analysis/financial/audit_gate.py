"""闸门1:审计机构备案核查(M2 分析层)。

方案 §0.5.A:从年报"审计报告"段抽**会计师事务所名** → 比对证券服务业务**备案名录**
(config/audit_firms.json,现行备案制 105 家)。不在录 → 财报不可信(高危红旗 → 降"风险")。
投资侧"合理怀疑即避"、非审计定罪:抽不到名/名录未知 → 不判(不误杀),只在"抽到名且不在录"时升红旗。

分层:分析层只读采集层落的 annual_report_text.审计报告 段 + config 名录,不触网、不改采集。
"""
from __future__ import annotations

import functools
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("analysis.financial.audit_gate")

_FIRMS_PATH = Path(__file__).resolve().parents[2] / "config" / "audit_firms.json"

# 事务所签名:抓"XX会计师事务所"(不含其后的"(特殊普通合伙)");限中文/英文字符,防跨行乱抓。
_FIRM_RE = re.compile(r"([一-龥A-Za-z]{2,20}会计师事务所)")


def _normalize(name: str) -> str:
    """归一:去'(特殊普通合伙)'等后缀、空白、全角括号;保留核心所名。"""
    if not name:
        return ""
    n = re.sub(r"[（(].*", "", name)          # 砍掉第一个括号及其后(特殊普通合伙/有限公司等)
    n = re.sub(r"\s+", "", n)
    return n.strip()


@functools.lru_cache(maxsize=1)
def _registry() -> dict:
    """加载名录 → {归一名/别名 → firm dict};缺文件/坏 JSON → 空(闸门降级为'未知',不误杀)。"""
    try:
        data = json.loads(_FIRMS_PATH.read_text(encoding="utf-8"))
    except Exception as e:                                   # noqa: BLE001
        logger.warning("审计名录加载失败(%s),闸门1 降级为未知", e)
        return {"index": {}, "meta": {}}
    index: dict[str, dict] = {}
    for f in data.get("firms", []):
        keys = {_normalize(f.get("名称", ""))} | {_normalize(a) for a in (f.get("别名") or [])}
        for k in keys:
            if k:
                index.setdefault(k, f)
    return {"index": index, "meta": {"更新日期": data.get("更新日期"),
                                     "覆盖家数": data.get("覆盖家数")}}


def extract_firm_names(audit_text: str) -> list[str]:
    """从审计报告段抽所有"XX会计师事务所"候选(去重保序)。"""
    if not audit_text:
        return []
    return list(dict.fromkeys(_FIRM_RE.findall(audit_text)))


def check_auditor(firm_name: str) -> dict:
    """单个事务所名 → 备案核查。返回 {事务所, 在录, 档位, 百强名次, 备注}。"""
    idx = _registry()["index"]
    norm = _normalize(firm_name)
    hit = idx.get(norm)
    if not hit:                                             # 归一名未命中 → 试别名包含(短简称)
        for key, f in idx.items():
            if key and (key in norm or norm in key) and len(key) >= 2:
                hit = f
                break
    if hit:
        return {"事务所": firm_name, "在录": True, "档位": hit.get("档位"),
                "百强名次": hit.get("百强名次_2024"), "备注": hit.get("备注")}
    return {"事务所": firm_name, "在录": False, "档位": None, "百强名次": None, "备注": None}


def audit_gate(audit_text: str) -> dict:
    """闸门1:从审计报告段判审计机构是否备案。

    返回 {审计机构, 闸门1, 在录, 档位, 备注, 依据}:
      - 抽到名且**任一候选在录** → 闸门1='通过'(取在录那家);
      - 抽到名且**全部不在录** → 闸门1='不通过'(报第一个候选,高危);
      - 名录空 或 抽不到名 → 闸门1='未知'(不判、不误杀)。
    """
    reg = _registry()
    names = extract_firm_names(audit_text)
    if not reg["index"]:
        return {"审计机构": None, "闸门1": "未知", "在录": None, "档位": None,
                "备注": None, "依据": "名录不可用"}
    if not names:
        return {"审计机构": None, "闸门1": "未知", "在录": None, "档位": None,
                "备注": None, "依据": "审计报告段未抽到事务所名"}
    checks = [check_auditor(n) for n in names]
    on = next((c for c in checks if c["在录"]), None)
    if on:
        return {"审计机构": on["事务所"], "闸门1": "通过", "在录": True,
                "档位": on["档位"], "备注": on["备注"],
                "依据": f"在备案名录(档{on['档位']})"}
    first = checks[0]
    return {"审计机构": first["事务所"], "闸门1": "不通过", "在录": False,
            "档位": None, "备注": None,
            "依据": f"审计机构「{first['事务所']}」不在证券服务业务备案名录"}
