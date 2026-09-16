"""S2 · 角色关系表维护(周度·"先定表")——程序化流水线第二阶段。

设计契约:docs/计划/2026-09-16_消息板块选股_程序化流水线_设计.md §1 S2。
在 roster.py(每日重算·10板块)之上升级为:**读 S1 全板块清单 → 逐板块产
龙头/中军/补涨先锋/弹性 + 主力(大资金) → 周度维护关系表 + 变更留痕**。

输入:S1 sector_universe.json 板块清单 + roles.py 可计算打分 + 主力资金流(LHB龙虎榜proxy)。
处理:每板块五角色名单;与上周表 diff(谁进谁出/角色变动),变更留痕。
输出:data/sector_roster_table/<板块>.json(角色名单+特征+主力覆盖度+变更历史)
      + data/sector_roster_table/changes/roster_changes_<ISO周>.md(周度变更过程文件)。
周期:每周一次。**保留每日 roles 版(data/sector_roster/)作对照,本表不动它。**

主力口径(§3 数据墙诚实处理):东财个股主力净流入接口被墙、北向停披露 → 用
**龙虎榜净买(lhb_asof·PIT·T+1可用)累计为正**作"大资金主导"proxy,**诚实标覆盖度**、
不假装全量主力流。缺 LHB 的板块主力名单为空并标注,不编造。

**防未来函数**:角色特征只用 ≤date 的 master kline(roles 保证);主力用 lhb_asof
(上榜日 < date,盘后披露 T+1 才可用),严格无未来。⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sector_forecast.sector_roster_table")

TABLE_VERSION = "v1-2026-09-16"

# —— 主力(LHB proxy)参数:预注册写死,不对个例调 ——
MAINFORCE_LOOKBACK_DAYS = 30     # 累计龙虎榜净买的回看窗口(日历日)
MAINFORCE_TOP_K = 5              # 每板块主力名单上限
ROLE_KEYS = ("龙头", "中军", "补涨先锋", "弹性股", "主力")


def _boards_from_universe(date: str) -> Optional[list[str]]:
    """读 S1 sector_universe.json 的板块清单(全申万一级)。取不到 → None(调用方回退)。"""
    from tools.config import settings
    from tools.backtest.iet_probe.data import _MAIN
    for base in (settings.PROJECT_ROOT, _MAIN):
        p = Path(base) / "data" / "analysis" / "sector_universe.json"
        if p.exists():
            try:
                uni = json.loads(p.read_text(encoding="utf-8"))
                return [x["板块"] for x in uni.get("板块清单", [])]
            except Exception:
                pass
    return None


def _mainforce(sw: str, members: list[str], date: str,
               feat_names: Optional[dict] = None) -> tuple[list[dict], dict]:
    """主力(大资金)proxy = 龙虎榜净买累计为正的领先个股。返回 (名单, 覆盖度)。

    覆盖度诚实标注:板块成分中有多少票有 LHB 快照 / 有上榜事件 / 净买为正 / 入选主力。
    """
    from datetime import datetime, timedelta
    from tools.collectors import lhb
    cutoff = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=MAINFORCE_LOOKBACK_DAYS)) \
        .strftime("%Y-%m-%d")

    have_snap = have_event = 0
    scored = []
    for c in members:
        try:
            evs = lhb.lhb_asof(c, date)              # 上榜日 < date(PIT·T+1可用)
        except FileNotFoundError:
            continue                                  # 无快照 = 未覆盖(不惩罚,不编造)
        except Exception:
            continue
        have_snap += 1
        win = [e for e in evs if str(e.get("list_date", ""))[:10] >= cutoff]
        if not win:
            continue
        have_event += 1
        net = sum((e.get("net_buy") or 0.0) for e in win)
        if net <= 0:                                  # 主力=净流入,净卖出/中性不入
            continue
        last = max((str(e.get("list_date", ""))[:10] for e in win), default="")
        scored.append({"code": c, "累计净买亿": round(net / 1e8, 3),
                       "上榜次数": len(win), "最近上榜": last})
    scored.sort(key=lambda x: -x["累计净买亿"])
    top = scored[:MAINFORCE_TOP_K]
    for i, it in enumerate(top):
        it["选级"] = "主选" if i < 2 else "备选"
        if feat_names:
            nm = feat_names.get(it["code"])
            if nm:
                it["name"] = nm
        it["理由"] = (f"近{MAINFORCE_LOOKBACK_DAYS}日龙虎榜净买+{it['累计净买亿']}亿、"
                     f"上榜{it['上榜次数']}次(最近{it['最近上榜']}),大资金净流入proxy")
    coverage = {
        "口径": f"LHB龙虎榜净买近{MAINFORCE_LOOKBACK_DAYS}日累计>0(替代被墙的主力净流入·PIT·T+1)",
        "板块成分数": len(members), "有LHB快照数": have_snap,
        "窗口内上榜数": have_event, "净买为正数": len(scored), "主力入选数": len(top),
        "覆盖率": round(have_snap / len(members), 3) if members else None,
        "诚实说明": "仅覆盖有龙虎榜快照的票;无快照票未纳入(非无大资金,是无此数据源)",
    }
    return top, coverage


def build_roster_table(sw: str, date: str, members: list[str]) -> dict:
    """单板块五角色关系表(龙头/中军/补涨先锋/弹性 + 主力)。"""
    from tools.analysis.sector_forecast import roles as R
    from tools.analysis.sector_forecast import roster as RO
    feat = R.member_features(sw, members, date)
    roles = R.identify_roles(sw, feat) if not feat.empty else {
        r: [] for r in ("龙头", "中军", "补涨先锋", "弹性股")}
    # 补股票名(复用 roster 的 code→name)
    name_of = {}
    for lst in roles.values():
        for it in lst:
            nm = RO._stock_name(it["code"])
            if nm:
                it["name"] = nm
            name_of[it["code"]] = nm
    # 主力(LHB proxy)
    主力, 主力覆盖 = _mainforce(sw, members, date, feat_names={c: RO._stock_name(c) for c in members})
    roles["主力"] = 主力
    return {
        "板块": sw, "口径": "申万一级", "细分说明": R.SEED_NOTE.get(sw, ""),
        "更新日": date, "table_version": TABLE_VERSION, "roles_version": R.ROLES_VERSION,
        "成分数": int(len(feat)),
        "roles": roles,
        "主力覆盖度": 主力覆盖,
        "诚实边界": ["概念级细分成分缺(仅申万一级)",
                    "主力=龙虎榜净买proxy(东财主力净流入被墙/北向停披露)、覆盖度见主力覆盖度",
                    "基本面quality/北向未纳入角色打分(缺失不惩罚)"],
        "免责": "测试环境研究模拟,非投资建议。",
    }


def _role_codes(table: dict) -> dict[str, list[str]]:
    """{角色: [code,...]}(供 diff)。"""
    out = {}
    for r in ROLE_KEYS:
        out[r] = [it["code"] for it in table.get("roles", {}).get(r, [])]
    return out


def _diff_table(cur: dict, prev: Optional[dict]) -> dict:
    """与上周表 diff:新进(任一角色新出现)/退出(全角色都不在了)/角色变动(所属角色变化)。"""
    if not prev:
        return {"基线周": None, "说明": "无上周表(首次建表)"}
    cur_rc, prev_rc = _role_codes(cur), _role_codes(prev)
    cur_all = {c for lst in cur_rc.values() for c in lst}
    prev_all = {c for lst in prev_rc.values() for c in lst}

    def _roles_of(rc, code):
        return sorted(r for r, lst in rc.items() if code in lst)

    新进 = [{"code": c, "角色": _roles_of(cur_rc, c)} for c in sorted(cur_all - prev_all)]
    退出 = [{"code": c, "原角色": _roles_of(prev_rc, c)} for c in sorted(prev_all - cur_all)]
    角色变动 = []
    for c in sorted(cur_all & prev_all):
        a, b = _roles_of(prev_rc, c), _roles_of(cur_rc, c)
        if a != b:
            角色变动.append({"code": c, "上周": a, "本周": b})
    return {"基线周": prev.get("ISO周") or prev.get("更新日"),
            "新进": 新进, "退出": 退出, "角色变动": 角色变动,
            "变更计数": {"新进": len(新进), "退出": len(退出), "角色变动": len(角色变动)}}


def _iso_week(date: str) -> str:
    from datetime import datetime
    y, w, _ = datetime.strptime(date, "%Y-%m-%d").isocalendar()
    return f"{y}-W{w:02d}"


def write_roster_table(payload: dict, date: str, *, out_root: Optional[str] = None) -> Path:
    """落 data/sector_roster_table/<板块>.json(原子写)+ diff 上一版留痕。"""
    from tools.config import settings
    base = Path(out_root) if out_root else settings.PROJECT_ROOT / "data" / "sector_roster_table"
    base.mkdir(parents=True, exist_ok=True)
    out = base / f"{payload['板块']}.json"
    prev = None
    if out.exists():
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            prev = None
    payload["ISO周"] = _iso_week(date)
    # 仅当上一版属于不同 ISO 周才作"上周"基线(同周重跑不误报变更)
    base_prev = prev if (prev and prev.get("ISO周") != payload["ISO周"]) else None
    payload["变更"] = _diff_table(payload, base_prev)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(out)
    return out


def write_changes_md(date: str, changes: list[dict], *, out_root: Optional[str] = None) -> Path:
    """汇总各板块周度变更 → roster_changes_<ISO周>.md(人看的过程文件)。"""
    from tools.config import settings
    base = Path(out_root) if out_root else settings.PROJECT_ROOT / "data" / "sector_roster_table"
    cdir = base / "changes"
    cdir.mkdir(parents=True, exist_ok=True)
    week = _iso_week(date)
    lines = [f"# 角色关系表 周度变更 · {week}(口径日 {date})", "",
             "> S2 每周维护;主力=龙虎榜净买proxy(诚实标覆盖度)。⚠️ 测试环境研究模拟,非投资建议。", ""]
    any_change = False
    for ch in changes:
        d = ch.get("变更", {})
        cnt = d.get("变更计数") or {}
        if not d.get("基线周"):
            lines.append(f"## {ch['板块']}  (首次建表,无上周基线)")
            lines.append("")
            continue
        if not any(cnt.values()):
            continue
        any_change = True
        lines.append(f"## {ch['板块']}  (基线 {d.get('基线周')})")
        if d.get("新进"):
            lines.append("- **新进**:" + "、".join(
                f"{x['code']}({'/'.join(x['角色'])})" for x in d["新进"]))
        if d.get("退出"):
            lines.append("- **退出**:" + "、".join(
                f"{x['code']}(原{'/'.join(x['原角色'])})" for x in d["退出"]))
        if d.get("角色变动"):
            lines.append("- **角色变动**:" + "、".join(
                f"{x['code']}({'/'.join(x['上周'])}→{'/'.join(x['本周'])})" for x in d["角色变动"]))
        lines.append("")
    if not any_change:
        lines.append("_本周无角色变更(或均为首次建表)。_")
    out = cdir / f"roster_changes_{week}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def build_all_roster_tables(date: str, *, boards: Optional[list[str]] = None,
                            out_root: Optional[str] = None) -> tuple[list[Path], Path]:
    """对 S1 全板块(或指定 boards)批量建关系表 + 汇总变更 md。返回 (表路径list, 变更md路径)。"""
    from tools.analysis.sector_forecast import roster as RO
    mem = RO._members_by_sector()
    targets = boards or _boards_from_universe(date) or sorted(mem.keys())
    paths, changes = [], []
    for sw in targets:
        members = mem.get(sw, [])
        if not members:
            logger.warning("板块 %s 无成分(申万映射空),跳过", sw)
            continue
        payload = build_roster_table(sw, date, members)
        p = write_roster_table(payload, date, out_root=out_root)
        paths.append(p)
        changes.append(payload)
        cov = payload["主力覆盖度"]
        logger.info("关系表 %s → %s(成分%d 龙头%d 主力%d/覆盖%s)", sw, p, payload["成分数"],
                    len(payload["roles"]["龙头"]), len(payload["roles"]["主力"]), cov.get("覆盖率"))
    md = write_changes_md(date, changes, out_root=out_root)
    logger.info("周度变更汇总 → %s(%d 板块)", md, len(changes))
    return paths, md
