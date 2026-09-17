"""读选股产物 → 统一 Pick 列表（大复盘输入层）。

权威结构化源 = `<data_root>/analysis/<date>/今日选股_<date>.json`（双路并集主产物，`板块[].个股[]`
逐票带 来源/角色/council/形态；board 级 `板块消息面` 挂催化）。统筹精选 v2/v3 是**手工对照版**（md），
只解析出 code 清单，其结构化依据**按 code join 今日选股**继承（同日同依据）——join 不到则 Pick 依据留空。

防未来：只读信号日 D 已落盘的产物（≤D），不取任何 >D 信息。缺文件/坏结构 → 有声降级（跳过 + warn），
绝不臆造。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from tools.review.types import Pick

logger = logging.getLogger("review.loaders")

_CODE_RE = re.compile(r"^#{2,4}\s*\d+[\.、]\s*(.+?)\s*[（(]?\s*(\d{6})\b")  # "### 1. 复旦微电 688385（..."

CURATED_FILES = {
    "统筹精选v2": "统筹精选_v2催化优先_{date}.md",
    "统筹精选v3": "统筹精选_v3信息先行_{date}.md",
}


def _resolve_root(data_root: str | None) -> Path:
    """返回 data/ 目录。缺省走项目 dataroot 解析（worktree 指主仓 data/）。"""
    if data_root:
        return Path(data_root)
    try:
        from tools.analysis.market_forecast.dataroot import resolve_data_root
        return resolve_data_root(None)
    except Exception:  # noqa: BLE001
        from tools.config import settings
        return settings.PROJECT_ROOT / "data"


def analysis_dir(date: str, data_root: str | None = None) -> Path:
    return _resolve_root(data_root) / "analysis" / date


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as e:
        logger.warning("读 %s 失败（有声降级、跳过）：%s", path, e)
        return None


def _pick_from_record(date: str, board: str | None, 板块消息面: dict,
                      rec: dict, version_tag: str) -> Pick | None:
    code = rec.get("code")
    if not code:
        return None
    return Pick(
        date=date,
        code=str(code),
        name=rec.get("name", ""),
        version_tag=version_tag,
        来源=rec.get("来源"),
        角色=rec.get("角色"),
        档=rec.get("档"),
        建议分=rec.get("建议分"),
        策略命中=list(rec.get("策略命中") or []),
        board=rec.get("board") or board,
        council=dict(rec.get("council") or {}),
        形态=dict(rec.get("形态") or {}),
        板块消息面=dict(板块消息面 or {}),
        入场_text=rec.get("入场"),
        止损_text=rec.get("止损"),
        硬纪律命中=rec.get("硬纪律命中"),
    )


def load_board_regime(date: str, data_root: str | None = None) -> dict[str, dict]:
    """sector_regime.json → {board: {冷热标签, 拥挤档, 动量_截面档}}（按 board join 用）。缺→{}。"""
    doc = _load_json(analysis_dir(date, data_root) / "sector_regime.json")
    out: dict[str, dict] = {}
    if not doc:
        return out
    for b in doc.get("板块", []) or []:
        name = b.get("板块")
        if name:
            out[name] = {"冷热标签": b.get("冷热标签"), "拥挤档": b.get("拥挤档"),
                         "动量_截面档": b.get("动量_截面档")}
    return out


def load_market_context(date: str, data_root: str | None = None) -> dict:
    """大盘 regime 上下文（判踏错大盘基调）：优先 今日选股.regime，回退 market_forecast.json。缺→{}。"""
    doc = _load_json(analysis_dir(date, data_root) / f"今日选股_{date}.json")
    if doc and doc.get("regime"):
        r = doc["regime"]
        return {"宏观净方向": r.get("宏观净方向"), "风险偏好": r.get("风险偏好"),
                "hs300方向": r.get("hs300方向")}
    mf = _load_json(analysis_dir(date, data_root) / "market_forecast.json")
    if mf:
        return {"宏观净方向": mf.get("宏观净方向") or mf.get("净方向"),
                "风险偏好": mf.get("风险偏好")}
    return {}


def load_today_picks(date: str, data_root: str | None = None) -> list[Pick]:
    """今日选股_<date>.json → list[Pick]（version_tag=今日选股）。缺文件→[]（有声）。"""
    path = analysis_dir(date, data_root) / f"今日选股_{date}.json"
    doc = _load_json(path)
    if not doc:
        logger.warning("缺 %s（有声降级，返回空 picks）", path)
        return []
    regimes = load_board_regime(date, data_root)
    picks: list[Pick] = []
    for bl in doc.get("板块", []) or []:
        board = bl.get("board")
        msg = bl.get("板块消息面") or {}
        for rec in bl.get("个股", []) or []:
            p = _pick_from_record(date, board, msg, rec, "今日选股")
            if p:
                p.board_regime = dict(regimes.get(p.board or board, {}))
                picks.append(p)
    return picks


def parse_curated_codes(md_text: str) -> list[tuple[str, str]]:
    """从统筹精选 md 提 (name, code)：逐票在 "### N. 名称 CODE（..." 标题行。锚点解析，鲁棒容噪。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in md_text.splitlines():
        m = _CODE_RE.match(line.strip())
        if m:
            name, code = m.group(1).strip(), m.group(2)
            if code not in seen:
                seen.add(code)
                out.append((name, code))
    return out


def load_curated_picks(date: str, data_root: str | None,
                       today_index: dict[str, dict]) -> list[Pick]:
    """统筹精选 v2/v3 md → Pick（依据按 code join 今日选股 today_index；join 不到则最小 Pick）。

    today_index：code → 今日选股原始 rec（含 board/板块消息面 挂在 rec['_board']/rec['_板块消息面']）。
    """
    picks: list[Pick] = []
    d = analysis_dir(date, data_root)
    for version_tag, fname_tpl in CURATED_FILES.items():
        path = d / fname_tpl.format(date=date)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.info("无 %s（对照版缺省，跳过）", path)
            continue
        except OSError as e:
            logger.warning("读 %s 失败（跳过）：%s", path, e)
            continue
        for name, code in parse_curated_codes(text):
            rec = today_index.get(code)
            if rec is not None:  # 继承今日选股结构化依据
                p = _pick_from_record(date, rec.get("_board"), rec.get("_板块消息面"),
                                      rec, version_tag)
                if p:
                    p.name = p.name or name
                    picks.append(p)
            else:  # 精选独有票（不在双路并集）：最小 Pick，依据留空（有声缺失）
                picks.append(Pick(date=date, code=code, name=name, version_tag=version_tag))
    return picks


def _build_today_index(date: str, data_root: str | None) -> dict[str, dict]:
    """code → 今日选股原始 rec（附 _board/_板块消息面），供精选继承依据。"""
    path = analysis_dir(date, data_root) / f"今日选股_{date}.json"
    doc = _load_json(path)
    idx: dict[str, dict] = {}
    if not doc:
        return idx
    for bl in doc.get("板块", []) or []:
        board = bl.get("board")
        msg = bl.get("板块消息面") or {}
        for rec in bl.get("个股", []) or []:
            code = rec.get("code")
            if code:
                r = dict(rec)
                r["_board"] = board
                r["_板块消息面"] = msg
                idx[str(code)] = r
    return idx


def load_picks(date: str, data_root: str | None = None,
               include_curated: bool = True) -> list[Pick]:
    """大复盘输入总入口：今日选股（双路并集）+ 统筹精选 v2/v3 对照版 → list[Pick]。"""
    picks = load_today_picks(date, data_root)
    if include_curated:
        idx = _build_today_index(date, data_root)
        picks.extend(load_curated_picks(date, data_root, idx))
    logger.info("date=%s 载入 %d picks（今日选股+精选对照）", date, len(picks))
    return picks
