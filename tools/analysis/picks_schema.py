"""每日选股结构化产物（`每日选股` view）的 schema 常量 + 校验骨架 —— **方案 P0 附带，不接生产**。

配套设计：docs/计划/2026-09-09_选股产物结构化与程序化_方案.md

⚠️ 本模块**未接入任何生产流程、未被任何模块 import**，仅供审阅 schema 形态与校验逻辑。
真正的落盘/回填/投送（build_picks_json / write_picks / render_picks_md_tables）留待 P1
实现窗按方案 §4.2 落地——那里才会 join record 层（serialize.build_record / repo.get_record）
与策略视图（import_to_db.collect_date），并原子写 data/analysis/<date>/每日选股.json。

此处只固化两样确定性、可单测的东西：
  1) 受控枚举常量（远端表格整齐 / 可排序 / 可校验的根据）；
  2) validate_picks(doc) 纯校验骨架（不读文件、不联网），返回错误列表（空=通过）。
"""
from __future__ import annotations

import re

SCHEMA_VERSION = "1.0"

# —— 受控枚举（方案 §2）——
STATUS = {"ok", "skipped", "partial"}
TYPE = {"买入候选", "检验样本", "规避"}
STANCE = {"买入", "可参与", "观望", "规避"}
DIRECTION = {"偏多", "偏空", "中性", "低波待动", "不给方向"}
CONF = {"高", "中高", "中", "中低", "低", "-"}
SENTIMENT_QUALITY = {"ok", "partial", "unknown", "missing"}

# 情绪盲区：这两态下不得给"买入/首选"最强表态（机制 §4 规则7 的代码化闸门）
SENTIMENT_BLIND = {"unknown", "missing"}

_CODE6 = re.compile(r"^\d{6}$")
_REQUIRED_PICK_FILLED = ("name", "close", "pct_chg")   # 回填后仍缺即报错，防"远端只有代码"复现


def validate_picks(doc: dict) -> list[str]:
    """纯校验，返回错误列表（空=通过）。见方案 §4.3。骨架：只校验结构/枚举/一致性，不读外部数据。"""
    errors: list[str] = []

    if not isinstance(doc, dict):
        return ["doc 不是 dict"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version 应为 {SCHEMA_VERSION}，实为 {doc.get('schema_version')!r}")

    meta = doc.get("meta") or {}
    status = meta.get("status")
    if status not in STATUS:
        errors.append(f"meta.status 非法：{status!r}（应 ∈ {sorted(STATUS)}）")
    for k in ("pick_date", "predict_for", "as_of"):
        if not meta.get(k):
            errors.append(f"meta.{k} 缺失")
    # 防未来：as_of 不得晚于 pick_date
    if meta.get("as_of") and meta.get("pick_date") and meta["as_of"] > meta["pick_date"]:
        errors.append(f"防未来违规：meta.as_of({meta['as_of']}) 晚于 pick_date({meta['pick_date']})")

    picks = doc.get("picks")
    if not isinstance(picks, list):
        return errors + ["picks 不是 list"]

    # 跳过态：picks 必空 + skip_reason 必填
    if status == "skipped":
        if picks:
            errors.append("status=skipped 但 picks 非空")
        if not meta.get("skip_reason"):
            errors.append("status=skipped 但 skip_reason 缺失")
    elif status == "ok":
        if not picks:
            errors.append("status=ok 但 picks 为空")
        if meta.get("picks_count") != len(picks):
            errors.append(f"picks_count({meta.get('picks_count')}) != len(picks)({len(picks)})")

    seen_codes: set[str] = set()
    ranks: list[int] = []
    for i, p in enumerate(picks):
        tag = f"picks[{i}]"
        code = p.get("code")
        if not (isinstance(code, str) and _CODE6.match(code)):
            errors.append(f"{tag}.code 非 6 位：{code!r}")
        elif code in seen_codes:
            errors.append(f"{tag}.code 重复：{code}")
        else:
            seen_codes.add(code)

        for field in _REQUIRED_PICK_FILLED:
            if p.get(field) in (None, ""):
                errors.append(f"{tag}.{field} 回填后仍缺（防'远端只有代码'）")

        _enum(errors, tag, "type", p.get("type"), TYPE)
        _enum(errors, tag, "stance", p.get("stance"), STANCE)
        _enum(errors, tag, "dir_1d", p.get("dir_1d"), DIRECTION)
        _enum(errors, tag, "dir_5d", p.get("dir_5d"), DIRECTION)
        _enum(errors, tag, "dir_1d_conf", p.get("dir_1d_conf"), CONF)
        _enum(errors, tag, "dir_5d_conf", p.get("dir_5d_conf"), CONF)
        sq = p.get("sentiment_quality")
        _enum(errors, tag, "sentiment_quality", sq, SENTIMENT_QUALITY)

        # 情绪质量闸门：盲区不得给买入/首选
        if sq in SENTIMENT_BLIND:
            if p.get("stance") == "买入":
                errors.append(f"{tag} 情绪质量={sq}（盲区）不得给 stance=买入")
            if "首选" in (p.get("stance_qualifier") or ""):
                errors.append(f"{tag} 情绪质量={sq}（盲区）不得含'首选'")

        # 不给方向时置信必须 "-"
        for d, c in (("dir_1d", "dir_1d_conf"), ("dir_5d", "dir_5d_conf")):
            if p.get(d) == "不给方向" and p.get(c) != "-":
                errors.append(f"{tag}.{d}=不给方向 但 {c}={p.get(c)!r}（应为 '-'）")

        if isinstance(p.get("buy_rank"), int):
            ranks.append(p["buy_rank"])

    # buy_rank 连续无重复
    if ranks and sorted(ranks) != list(range(1, len(ranks) + 1)):
        errors.append(f"buy_rank 应为 1..N 连续无重复，实为 {sorted(ranks)}")

    return errors


def _enum(errors: list[str], tag: str, field: str, val, allowed: set[str]) -> None:
    if val not in allowed:
        errors.append(f"{tag}.{field} 非法：{val!r}（应 ∈ {sorted(allowed)}）")


# —— 样例 doc（供审阅 schema 形态）——
EXAMPLE_OK = {
    "schema_version": SCHEMA_VERSION,
    "meta": {
        "pick_date": "2026-09-09", "predict_for": "2026-09-10", "as_of": "2026-09-09",
        "generated_at": "2026-09-09T20:15:03+08:00", "generator": "daily-stock-selection",
        "status": "ok", "skip_reason": None,
        "market_regime": "权重护盘伪涨市/二八分化/个股普跌",
        "market_forecast_ref": None, "picks_count": 1,
        "source_md": "docs/每日分析/选股/2026-09-09.md",
        "drift_note": "cron 18:36 / 实际 18:31:44 / DRIFT ok",
    },
    "picks": [{
        "code": "601061", "name": "中信金属", "industry": "有色/金属贸易",
        "close": 12.18, "pct_chg": 4.91,
        "strategies": [{"name": "策略0合议", "rank": 1, "score": 0.7107}],
        "type": "买入候选", "buy_rank": 1, "stance": "买入", "stance_qualifier": "首选",
        "dir_1d": "偏多", "dir_1d_conf": "中", "dir_5d": "偏多", "dir_5d_conf": "中高",
        "sentiment_quality": "ok",
        "key_reason": "净利+84.55%真利好+PE14.5低估+主力连续6日净流入+放量反包",
        "key_risk": "铜价回调；已涨2日部分price-in",
        "alpha_beta": "α=铜价景气+主力吸筹相对强度；β=有色板块次要驱动",
        "watch_points": ["明日主力是否维持净流入？", "收盘是否站稳MA5≈11.63？"],
        "detail_anchor": "选股/2026-09-09.md#1-601061-中信金属",
    }],
    "review": None,
}

EXAMPLE_SKIPPED = {
    "schema_version": SCHEMA_VERSION,
    "meta": {
        "pick_date": "2026-09-09", "predict_for": "2026-09-10", "as_of": "2026-09-09",
        "generated_at": "2026-09-09T21:00:00+08:00", "generator": "daily-stock-selection",
        "status": "skipped", "skip_reason": "闭环未在门控90分钟窗口内完成",
        "picks_count": 0,
    },
    "picks": [],
    "review": None,
}


if __name__ == "__main__":   # 自证：样例应通过校验（快速手验骨架逻辑）
    assert validate_picks(EXAMPLE_OK) == [], validate_picks(EXAMPLE_OK)
    assert validate_picks(EXAMPLE_SKIPPED) == [], validate_picks(EXAMPLE_SKIPPED)
    print("picks_schema 骨架自证通过（EXAMPLE_OK / EXAMPLE_SKIPPED 均校验通过）")
