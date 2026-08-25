"""SEPA+VCP 监控编排:全 A 均线入池 → 合格池上切波段 → 两表+雷达+收缩结构图。

两阶段:
  ① 全 A 只碰日 K 做 SEPA 三条硬条件(便宜)
  ② 只对合格池打星标、切 VCP、写观察池与主图(贵活,通常 200~400 只)

午间/收盘两趟:产物带 session。不自动下单、不给买点。⚠️ 非投资建议。

入口:
  python -m tools.pipeline.screen_sepa_vcp [--universe N|--codes ...] [--date D]
                                           [--session 午间|收盘] [--no-fetch] [--no-chart]
  python -m tools.run sepa [--universe N] [--session 收盘] [--no-fetch]
"""
from __future__ import annotations

import json
import logging

import pandas as pd

from tools.analysis.sepa_vcp import sepa as sepa_mod
from tools.analysis.sepa_vcp import stars as stars_mod
from tools.analysis.sepa_vcp import vcp as vcp_mod
from tools.collectors import market
from tools.config import settings
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store

logger = logging.getLogger("pipeline.screen_sepa_vcp")

_CFG = THRESHOLDS["SEPA_VCP"]
_NAMES_PATH = settings.PROJECT_ROOT / "config" / "code_name.json"
_NAMES: dict[str, str] | None = None


def _names() -> dict[str, str]:
    global _NAMES
    if _NAMES is None:
        try:
            _NAMES = json.loads(_NAMES_PATH.read_text(encoding="utf-8"))
        except Exception:
            _NAMES = {}
    return _NAMES


def _name_of(code: str) -> str:
    return _names().get(code) or code


def _skip_code(code: str) -> bool:
    heads = _CFG.get("排除代码头") or []
    return bool(code) and code[0] in heads


def _offline_universe_codes(limit: int | None = None) -> list[str]:
    codes = set(store.list_master_codes())
    raw_root = settings.DATA_RAW
    if raw_root.exists():
        for p in raw_root.glob("**/kline/*.parquet"):
            stem = p.stem
            if len(stem) == 6 and stem.isdigit():
                codes.add(stem)
    out = [c for c in sorted(codes) if not _skip_code(c)]
    if limit:
        out = out[:limit]
    return out


def _load_or_fetch_kline(code: str, fetch: bool):
    try:
        return market.load_kline_recent(code)
    except FileNotFoundError:
        if not fetch:
            return None
        try:
            return market.fetch_kline([code]).get(code)
        except Exception:  # noqa: BLE001
            return None


def _prev_pool_index(as_of: str) -> dict[str, dict]:
    """上一交易日合格池 {code: row},用于入池天数。缺则空。"""
    try:
        dates = store.list_dates("analysis")
    except Exception:
        return {}
    prev = [d for d in dates if d < as_of]
    if not prev:
        return {}
    try:
        v = store.get_view("SEPA合格池", date=prev[-1])
    except FileNotFoundError:
        return {}
    return {r["code"]: r for r in (v.get("rows") or []) if r.get("code")}


# 展示标签字符串:措辞明确为"收盘态快照",避免"进行中"被误读成实时监控。
# 注意:与内部布尔字段键 vcp["VCP进行中"] 区分——后者是判定位、不改;这里只是给用户看的标签文案。
_TAG_VCP = "VCP收缩中(收盘)"


def _tags(vcp: dict, first_day: bool, n_stars: int) -> list[str]:
    tags = []
    if first_day and n_stars >= 1:
        tags.append("新候选")
    if vcp.get("VCP进行中"):
        tags.append(_TAG_VCP)
    if vcp.get("接近枢纽"):
        tags.append("接近枢纽")
    if vcp.get("结构破坏"):
        tags.append("结构破坏")
    return tags


def _chain_short(chain, n: int = 3) -> str:
    """雷达/摘要只念末 n 轮,避免 40 轮链念不完。"""
    xs = [str(x) for x in (chain or [])]
    if not xs:
        return ""
    if len(xs) <= n:
        return "→".join(f"{x}%" for x in xs)
    return "…" + "→".join(f"{x}%" for x in xs[-n:])


def _radar(session: str, watch: list[dict]) -> str:
    focus = [r for r in watch if "结构破坏" not in (r.get("标签") or [])]
    broken = [r for r in watch if "结构破坏" in (r.get("标签") or [])]
    vcp_on = [r for r in watch if _TAG_VCP in (r.get("标签") or [])]
    fresh = [r for r in watch if "新候选" in (r.get("标签") or [])]
    lines = [f"【{session}雷达】", f"重点：{len(focus)}只"]
    if vcp_on:
        bits = []
        for r in vcp_on[:8]:
            chain = _chain_short(r.get("回撤链"))
            n = r.get("轮数") or 0
            bits.append(f"{r['code']}（{chain}，第{n}轮）" if chain else r["code"])
        lines.append(_TAG_VCP + "：" + "、".join(bits))
    else:
        lines.append(_TAG_VCP + "：无")
    if fresh:
        bits = []
        for r in fresh[:8]:
            star = "板块⭐" if r.get("板块星") else ("基本面⭐" if r.get("基本面星") else "⭐")
            bits.append(f"{r['code']}（{star}）")
        lines.append("新入池+星：" + "、".join(bits))
    else:
        lines.append("新入池+星：无")
    lines.append("失效移出：" + ("、".join(r["code"] for r in broken[:8]) if broken else "无"))
    return "\n".join(lines)


def run_sepa_vcp(codes: list[str], as_of: str | None = None,
                 session: str = "收盘", fetch: bool = False,
                 write_charts: bool = True) -> dict:
    """扫描 codes,落 SEPA合格池 / SEPA观察池 / SEPA雷达。返回 summary。"""
    if as_of:
        store.set_active_date(as_of)
    as_of = as_of or pd.Timestamp.today().strftime("%Y-%m-%d")
    session = session if session in ("午间", "收盘") else "收盘"
    prev = _prev_pool_index(as_of)
    need = int(_CFG["最少历史根数"])

    qualified: list[dict] = []
    scanned = skipped = st_skip = 0
    for code in codes:
        if _skip_code(code):
            skipped += 1
            continue
        name = _name_of(code)
        if stars_mod._is_st(name):
            st_skip += 1
            continue
        kdf = _load_or_fetch_kline(code, fetch)
        if kdf is None or len(kdf) < need:
            skipped += 1
            continue
        scanned += 1
        r = sepa_mod.screen_latest(kdf)
        if not r.get("入池"):
            continue
        old = prev.get(code)
        first = old is None
        入池日 = (old or {}).get("入池日") or as_of
        入池天数 = (int(old["入池天数"]) + 1) if old and old.get("入池天数") else 1
        industry = stars_mod.industry_of(code)
        qualified.append({
            "code": code, "name": name, "industry": industry,
            "入池日": 入池日, "入池天数": 入池天数, "今日首入": first,
            "明细": r.get("明细") or {},
            "_kdf": kdf,
        })

    fund_flags = {row["code"]: stars_mod.fundamental_star(row["code"]) for row in qualified}
    sector_set = stars_mod.sector_star_codes(
        [{"code": r["code"], "industry": r["industry"]} for r in qualified])

    pool_rows = []
    watch_rows = []
    for row in qualified:
        kdf = row.pop("_kdf")
        code = row["code"]
        fstar = bool(fund_flags.get(code))
        sstar = code in sector_set
        n_stars = int(fstar) + int(sstar)
        vcp = vcp_mod.analyze_vcp(kdf)
        tags = _tags(vcp, row["今日首入"], n_stars)
        # 趋势分:60日涨幅(Minervini 动量语义),供展示层按强度取 Top10 排序。
        # SEPA 已保证 ≥220 根历史,close[-61] 恒安全;不足则退化用最早一根。
        _close = kdf["close"]
        趋势分 = round(float(_close.iloc[-1] / _close.iloc[-61] - 1.0), 4) \
            if len(_close) >= 61 else round(float(_close.iloc[-1] / _close.iloc[0] - 1.0), 4)
        rec = {
            "code": code, "name": row["name"], "industry": row["industry"],
            "入池日": row["入池日"], "入池天数": row["入池天数"],
            "今日首入": row["今日首入"],
            "基本面星": fstar, "板块星": sstar, "星标数": n_stars,
            "趋势分": 趋势分,
            "轮数": vcp.get("轮数") or 0,
            "回撤链": vcp.get("回撤链") or [],
            "VCP进行中": vcp.get("VCP进行中"),
            "结构更健康": vcp.get("结构更健康"),
            "接近枢纽": vcp.get("接近枢纽"),
            "结构破坏": vcp.get("结构破坏"),
            "洗盘刺破": vcp.get("洗盘刺破"),
            "标签": tags,
        }
        pool_rows.append(rec)
        if tags:
            watch_rows.append(rec)
        if write_charts and tags:
            store.put_code_view("sepa_vcp_chart", code,
                                vcp_mod.build_chart_payload(kdf, vcp))

    # 表1 按入池天数降序;表2 破坏置底,其余按轮数+星标
    pool_rows.sort(key=lambda x: (-int(x["入池天数"]), x["code"]))
    watch_rows.sort(key=lambda x: (
        1 if "结构破坏" in x["标签"] else 0,
        -int(x["轮数"]), -int(x["星标数"]), x["code"]))

    radar = _radar(session, watch_rows)
    pool_view = {
        "as_of": as_of, "session": session, "策略": "SEPA+VCP 监控",
        "扫描数": len(codes), "有效样本": scanned,
        "跳过数": skipped, "ST跳过": st_skip,
        "合格数": len(pool_rows),
        "rows": pool_rows,
        "规则": "股价>MA50>MA150>MA200 且 MA200向上(当前>20日前) 且 股价在MA50与MA200上方",
        "免责": "非投资建议,不自动下单,不给精确买点",
    }
    watch_view = {
        "as_of": as_of, "session": session,
        "合格数": len(pool_rows), "观察数": len(watch_rows),
        "rows": watch_rows,
        "雷达": radar,
        "免责": "非投资建议。表内分类满足任一即入;星标不参与过滤。形态只作收缩结构参考,不自动标成完成。",
    }
    radar_view = {"as_of": as_of, "session": session, "文本": radar,
                  "重点数": sum(1 for r in watch_rows if "结构破坏" not in r["标签"]),
                  "观察数": len(watch_rows)}
    store.put_view("SEPA合格池", pool_view)
    store.put_view("SEPA观察池", watch_view)
    store.put_view("SEPA雷达", radar_view)
    logger.info("SEPA+VCP %s:扫描 %d / 有效 %d / 合格 %d / 观察 %d",
                session, len(codes), scanned, len(pool_rows), len(watch_rows))
    return {"合格池": pool_view, "观察池": watch_view, "雷达": radar_view}


def _main(argv: list[str] | None = None) -> int:
    import argparse
    from tools.collectors import universe

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="SEPA+VCP 监控扫描(午间/收盘)")
    ap.add_argument("--universe", type=int, metavar="N", help="全A票池前 N 只(不传=全量)")
    ap.add_argument("--codes", help="逗号分隔的指定代码(优先于 --universe)")
    ap.add_argument("--date", help="运行日期 YYYY-MM-DD(默认今天)")
    ap.add_argument("--session", default="收盘", choices=["午间", "收盘"])
    ap.add_argument("--no-fetch", action="store_true", help="只读本地缓存,不触网")
    ap.add_argument("--no-chart", action="store_true", help="不写收缩结构图")
    a = ap.parse_args(argv)

    as_of = a.date or pd.Timestamp.today().strftime("%Y-%m-%d")
    fetch = not a.no_fetch
    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    elif fetch:
        codes = universe.universe_codes(limit=a.universe)
        codes = [c for c in codes if not _skip_code(c)]
    else:
        codes = _offline_universe_codes(limit=a.universe)
    if fetch:
        try:
            market.update_master_from_spot(codes, date=as_of)
        except Exception as e:  # noqa: BLE001
            logger.warning("当日 spot 增量失败,沿用本地主档: %s", e)
    logger.info("SEPA+VCP 扫描:%d 只(日期 %s session=%s fetch=%s)",
                len(codes), as_of, a.session, fetch)
    v = run_sepa_vcp(codes, as_of=as_of, session=a.session, fetch=fetch,
                     write_charts=not a.no_chart)
    print(v["雷达"]["文本"])
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
