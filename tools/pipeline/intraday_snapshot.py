"""盘中定时快照(分析师流水线的**确定性节点**:在正确时点把实时行情落盘)。

## 为什么有这个脚本(2026-09-03)

三个 Claude 定时任务(盘中核实/盘尾复盘/收盘选股)由桌面 App 触发,实测**App 非活动时
会话的工具调用会被挂起数小时**:09-03 盘中任务 10:34 发出第一条命令,直到 13:50 才真正执行,
于是"10:30 早盘偏离核实"取到的其实是 13:50 的数据——**早盘那一维根本没被检验**。

治本:把"在正确时点取实时数据"这件**确定性**的事从 LLM 会话里下沉到 OS 级定时 + 项目代码
(见 docs/计划/分析师流水线设计.md §2.5「确定性节点(代码/工具)」)。launchd 在真实 10:30
跑本脚本抓快照落盘;Claude 盘中任务**无论几点醒**,都读这份 10:30 的真快照做早盘偏离判断,
时效性由代码保证、不再依赖会话被唤醒的时刻。本脚本是该类节点的第一个落地件。

## 契约

输入:
  · 标的 = 上一交易日 `docs/每日分析/选股/<上一交易日>.md` 解析出的代码 ∪ 自选池(A股部分)
    ∪ `--codes` 显式指定;指数 = 上证/深成/创业板/沪深300/中证500。
  · 行情源 = 腾讯 gtimg,复用 `tools.collectors.gtimg_quote`(与 web 盯盘同一份解析)。
输出:`data/intraday/<YYYY-MM-DD>/T<slot>.json`,见 `build_payload()` 的结构说明。

纪律:
  · **防未来函数**:快照只含抓取时点及之前的信息;`captured_at` 写**真实抓取时刻**,
    并给出与该 slot 名义时刻的 `drift_seconds`(下游据此判断这份快照够不够"准点")。
  · **幂等**:同 slot 文件已存在 → 不覆盖、退出 0(除 `--force`);多 slot(1030/1400/…)共存。
  · **容错**:单票失败只记 `errors`,不让整体失败;**全部失败**才非 0 退出且**不落文件**
    (保持"文件存在 ⇒ 内容可用"这一不变式,下游只需判文件在不在)。
  · **非交易日**:launchd 只认周一~周五,节假日照样触发 → 本脚本自己判交易日,非交易日跳过退 0。

用法:
    python -m tools.pipeline.intraday_snapshot --slot 1030
    python -m tools.pipeline.intraday_snapshot --slot 1400 --codes 002811,603270 --force
    python -m tools.pipeline.intraday_snapshot --slot testrun          # 联调:名义时刻未知→drift=None

⚠️ 测试环境研究用,非投资建议;只读行情、不下单。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from tools.collectors import calendar as cal
from tools.collectors import gtimg_quote
from tools.collectors.index import index_prefix
from tools.config import settings, stock_pool

logger = logging.getLogger("pipeline.intraday_snapshot")

SCRIPT_VERSION = "1.0.0"
SOURCE = "qt.gtimg.cn"

# 快照跟踪的指数(6 位代码 → 名称);gtimg 前缀走 collectors.index.index_prefix
INDEX_CODES: dict[str, str] = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000300": "沪深300",
    "000905": "中证500",
}

OUT_ROOT = settings.PROJECT_ROOT / "data" / "intraday"
PICK_DIR = settings.PROJECT_ROOT / "docs" / "每日分析" / "选股"
LOG_PATH = settings.PROJECT_ROOT / "logs" / "intraday_snapshot.log"
_CODE_NAME_PATH = settings.PROJECT_ROOT / "config" / "code_name.json"

_SLOT_RE = re.compile(r"^([01]\d|2[0-3])([0-5]\d)$")     # HHMM(其余 slot 名视为"名义时刻未知")
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# 机读锚点:选股 md 顶部可放一行 `<!-- PICKS: 601061,600356,... -->`(HTML 注释,给人不可见、给机器权威)。
# 选 HTML 注释而非 YAML frontmatter:注释对 md 渲染完全透明(不显示、不占版面),各类阅读器/GitHub 预览
# 都不当正文;frontmatter 需文件严格以 `---` 起头,对存量自由文本 md 侵入更大。
_ANCHOR_RE = re.compile(r"<!--\s*PICKS?\s*:\s*(.*?)\s*-->", re.IGNORECASE | re.DOTALL)
# 锚点里表示"本日无选股"的取值(命中即返回空、绝不回退全文扫码)。
_ANCHOR_NONE = {"none", "skip", "empty", "n/a", "na", "-", "无", ""}

# 无锚点时的回退**首选锚区**:"买入建议排序"这一节。
#   为什么单挑这一节:它是每份真实收盘选股 md 都有的**最终排序表**(`## 4. 买入建议排序` /
#   `## 四、买入建议排序`),一张干净的表格恰好列出且仅列出当日全部入选票(买入候选/观望/检验/规避都在),
#   且**排在**策略建议(§6)/记分预登记(§5)/遇到的问题这些含"说明性散码"的节**之前**。
# `买入(建议)?排序` 同时覆盖收盘选股 md 的「买入建议排序」和 intraday_screen 全A午盘选股 md 的「买入排序」榜。
_PICK_SECTION_RE = re.compile(r"买入(?:建议)?排序")
# 次选锚区:没有"买入建议排序"节的老/异版 md(如 day-0 人工文)用其它"选股清单式"节名兜底。
#   反例(刻意排除):§3「逐票深度分析」正文、§6「策略建议」等含消息面误判讨论里的散码
#   (如"同名深圳能源000027 张冠李戴"),不在选股清单节内,故不会被扫到。
_PICKLIST_SECTION_RE = re.compile(
    r"买入(?:建议)?排序|选股清单|最终选出|最终\s*\d+\s*只|\d+\s*只票|入选.{0,6}清单|最终.{0,4}只")
# 识别"跳过留痕/本日无选股"文件——只看**标题行**:真正的跳过文件会在标题里自证跳过;
# 正文里"…当晚如实跳过…"这类叙述(补做文件常见)不算,否则会把有真实选股的补做文件误判成空。
_SKIP_MARK_RE = re.compile(r"跳过留痕|本日无选股|今日无选股|无真实选股|选股跳过|未选股|本日跳过|跳过.{0,6}选股")


# ────────────────────────────── 标的解析 ──────────────────────────────

def _known_codes() -> set[str]:
    """离线全A代码集合(config/code_name.json);缺文件 → 空集(调用方回退前缀规则)。"""
    try:
        with open(_CODE_NAME_PATH, encoding="utf-8") as f:
            return set(json.load(f).keys())
    except Exception as e:                       # 文件缺失/损坏 → 不阻断,回退前缀规则
        logger.warning("code_name.json 不可用(%s),标的校验回退前缀规则", e)
        return set()


def _looks_like_a_code(code: str) -> bool:
    """6 位串是否长得像 A 股代码(0/2/3/6/8/4 开头);排除指数代码。

    ⚠️ **已知缺口(2026-09-03,有意未修)**:漏 9 开头 → 920 段(北交所现行)与 900 段
    (沪B)会被判成非 A 股。实际影响有限:本函数只是 `_known_codes()` 拿不到
    `config/code_name.json` 时的兜底路径。改了会让 920 票开始进早盘快照(口径变更),
    故本轮不动,另开任务拍板。届时应改成委托 `tools.config.exchange.is_a_code` 再剔指数。
    """
    return len(code) == 6 and code[:1] in ("0", "2", "3", "6", "8", "4") and code not in INDEX_CODES


def _codes_from(text: str, *, trust: bool = False) -> list[str]:
    """从一段文本里扫独立 6 位代码(顺序去重、剔指数)。

    trust=False(默认,回退扫码):用**离线全A代码表**过滤散码(拿不到代码表时回退前缀规则),
      只留像真票的;trust=True(锚点):锚点是权威来源,只做去指数/去重的最小清洗,不要求在代码表里
      (允许代码表尚未收录的新票/920 段等,以锚点为准)。
    """
    known = _known_codes() if not trust else set()
    out: list[str] = []
    for code in _CODE_RE.findall(text):
        if code in INDEX_CODES or code in out:
            continue
        if trust:
            out.append(code)
        elif known:
            if code in known:
                out.append(code)
        elif _looks_like_a_code(code):
            out.append(code)
    return out


def _split_sections(text: str) -> list[tuple[str, str]]:
    """按 level-2(`## `)标题把 md 切成 [(标题行, 正文), ...];首个 `## ` 之前的前言标题为空串。"""
    sections: list[tuple[str, str]] = []
    head, body = "", []
    for line in text.splitlines():
        if re.match(r"^##\s", line):        # 只在 level-2 处切;### 子标题留在本节正文里
            sections.append((head, "\n".join(body)))
            head, body = line, []
        else:
            body.append(line)
    sections.append((head, "\n".join(body)))
    return sections


def _is_skip_file(text: str) -> bool:
    """跳过留痕/本日无选股文件?**只看标题行**(首个 `# `)是否自证跳过。

    刻意不扫正文:补做文件常在正文叙述"原文件为跳过留痕/当晚如实跳过",但它本身有真实选股,
    扫正文会把这类文件误判成空。真正的跳过留痕文件会在标题里写明。
    """
    title = next((ln for ln in text.splitlines() if ln.startswith("# ")), "")
    return bool(_SKIP_MARK_RE.search(title))


def parse_pick_codes(md_path: str | Path) -> list[str]:
    """从选股 md 里解析出**当日选中**的股票代码(顺序去重、剔指数)。

    解析优先级(从最权威到最兜底):
      1. **机读锚点** `<!-- PICKS: 601061,600356,... -->`:存在即**只认锚点**里的码
         (`PICKS: none` / 无选股标记 → 返回空)。这是生成方应主动写出的确定性契约。
      2. **跳过留痕/本日无选股**文件(**标题行**自证跳过)→ 返回空,**不回退全文扫码**
         (根因:这类文件正文常含说明性 6 位数字,历史上被误当"昨日选股"抓走)。
      3. **无锚点时的回退**:只在**「买入建议排序」这一节**里扫码——它是每份真实收盘选股 md 都有的
         最终排序表,干净地列出且仅列出当日全部入选票,且排在策略建议/记分/遇到的问题这些含正文散码的节之前。
      4. **没有该节**的老/异版 md(如 day-0 人工文):退而扫其它"选股清单式"节(选股清单/最终选出/N只票…)。
      5. **连选股清单节都没有** → 返回空。跳过留痕文件正是这种(只有根因/说明文字、无选股清单表),
         从而**不会**再把根因段落里的散码误当"昨日选股"抓走(本次要修的缺陷)。

    宁缺勿滥优先于宁多不漏:多抓一只散码会让下游快照/研判跟踪一只根本没选的票(已实证的缺陷),
    比漏抓危害更大;真正想被跟踪的票,生成方写一行锚点即可确定性锁定。
    """
    p = Path(md_path)
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8", errors="ignore")

    # 1) 机读锚点最优先:存在即只认锚点
    m = _ANCHOR_RE.search(text)
    if m:
        raw = m.group(1).strip()
        if raw.lower() in _ANCHOR_NONE or _SKIP_MARK_RE.search(raw):
            return []
        return _codes_from(raw, trust=True)

    # 2) 标题自证跳过 → 空(不回退扫码)
    if _is_skip_file(text):
        return []

    sections = _split_sections(text)
    # 3) 首选:「买入建议排序」节
    region = "\n".join(body for head, body in sections if _PICK_SECTION_RE.search(head))
    if region:
        return _codes_from(region)
    # 4) 次选:其它选股清单式节(老/异版 md)
    region = "\n".join(body for head, body in sections if _PICKLIST_SECTION_RE.search(head))
    if region:
        return _codes_from(region)
    # 5) 无任何选股清单节(含跳过留痕文件)→ 空
    return []


def prev_trading_day(date: str) -> str | None:
    """date 之前最近的一个交易日(YYYY-MM-DD);日历不可用 → 回退"上一个工作日"。"""
    d0 = datetime.strptime(date, "%Y-%m-%d")
    try:
        dates = cal.trading_dates()
    except Exception as e:                        # 日历异常不阻断快照
        logger.warning("交易日历异常(%s),上一交易日回退工作日近似", e)
        dates = set()
    if dates:
        earlier = [d for d in dates if d < date]
        if earlier:
            return max(earlier)
        return None
    d = d0 - timedelta(days=1)
    while d.weekday() >= 5:                       # 周六/周日往前退
        d -= timedelta(days=1)
    logger.warning("交易日历不可用,上一交易日按工作日近似 = %s", d.strftime("%Y-%m-%d"))
    return d.strftime("%Y-%m-%d")


def _rel(p: Path) -> str:
    """路径尽量写成相对项目根(可读、可移交);不在项目内(如测试 tmp)→ 原样绝对路径。"""
    try:
        return str(p.relative_to(settings.PROJECT_ROOT))
    except ValueError:
        return str(p)


def resolve_targets(date: str, extra_codes: list[str] | None = None) -> tuple[list[str], dict]:
    """标的 = 上一交易日选股 md ∪ 自选池(A股) ∪ --codes。返回 (codes, 来源明细)。

    来源明细进 `meta.targets`,让下游/复盘能看清"这份快照为什么盯这些票"。
    选股 md 缺失(如上一交易日没出选股)→ 只用自选池,记 `pick_md=None`,不报错。
    """
    pick_date = prev_trading_day(date)
    pick_md = PICK_DIR / f"{pick_date}.md" if pick_date else None
    pick_codes = parse_pick_codes(pick_md) if pick_md else []
    if pick_md is not None and not pick_md.exists():
        logger.warning("上一交易日选股 md 不存在:%s(只用自选池)", pick_md)
    pool_codes = stock_pool.get_codes_by_market("A")
    extra = [c.strip() for c in (extra_codes or []) if c.strip()]

    codes = list(dict.fromkeys([*pick_codes, *pool_codes, *extra]))
    sources = {
        "pick_date": pick_date,
        "pick_md": _rel(pick_md) if (pick_md is not None and pick_md.exists()) else None,
        "pick_codes": pick_codes,
        "pool_codes_n": len(pool_codes),
        "explicit_codes": extra,
        "total_n": len(codes),
    }
    return codes, sources


# ────────────────────────────── 时间与路径 ──────────────────────────────

def slot_nominal_dt(date: str, slot: str) -> datetime | None:
    """slot 名义时刻:"1030" → 该日 10:30:00 本地时间;非 HHMM 的 slot(如 testrun)→ None。"""
    m = _SLOT_RE.match(slot)
    if not m:
        return None
    return datetime.strptime(f"{date} {m.group(1)}:{m.group(2)}", "%Y-%m-%d %H:%M").astimezone()


def snapshot_path(date: str, slot: str) -> Path:
    """产出路径 data/intraday/<date>/T<slot>.json。"""
    return OUT_ROOT / date / f"T{slot}.json"


# ────────────────────────────── 抓取 ──────────────────────────────

def fetch_codes(codes: list[str]) -> tuple[dict[str, dict], list[dict]]:
    """抓个股 → ({code: 字段}, errors)。整批网络失败 → 全部记 errors(不抛,由上层判"全挂")。"""
    if not codes:
        return {}, []
    errors: list[dict] = []
    try:
        got = gtimg_quote.fetch_quotes(codes)
    except Exception as e:
        logger.error("个股报价抓取失败(整批):%s: %s", type(e).__name__, e)
        return {}, [{"scope": "code", "code": c, "reason": f"{type(e).__name__}: {e}"}
                    for c in codes]
    out: dict[str, dict] = {}
    for c in codes:
        q = got.get(c)
        if not q or q.get("price") is None:
            errors.append({"scope": "code", "code": c,
                           "reason": "源方无数据(停牌/退市/代码异常)" if not q else "现价缺失"})
            continue
        out[c] = {"code": c, **q}
    return out, errors


def fetch_indices(index_codes: dict[str, str] | None = None) -> tuple[dict[str, dict], list[dict]]:
    """抓指数 → ({code: 字段}, errors)。指数前缀走 collectors.index.index_prefix(沪/深/北)。"""
    idx = index_codes or INDEX_CODES
    if not idx:
        return {}, []
    symbols = [index_prefix(c) for c in idx]
    try:
        got = gtimg_quote.fetch_symbols(symbols)
    except Exception as e:
        logger.error("指数报价抓取失败(整批):%s: %s", type(e).__name__, e)
        return {}, [{"scope": "index", "code": c, "reason": f"{type(e).__name__}: {e}"}
                    for c in idx]
    out: dict[str, dict] = {}
    errors: list[dict] = []
    for c, alias in idx.items():
        q = got.get(c)
        if not q or q.get("price") is None:
            errors.append({"scope": "index", "code": c, "reason": "源方无数据"})
            continue
        out[c] = {"code": c, "alias": alias, **q}
    return out, errors


# ────────────────────────────── 组装与落盘 ──────────────────────────────

def build_payload(date: str, slot: str, captured_at: datetime,
                  codes_data: dict[str, dict], indices_data: dict[str, dict],
                  errors: list[dict], sources: dict) -> dict:
    """快照 JSON。

    结构:
      date/slot                 —— 快照日与时点槽位
      captured_at               —— **真实抓取时刻**(ISO,带时区),防未来函数的锚
      nominal_at/drift_seconds  —— slot 名义时刻与偏差秒(正=晚到);slot 非 HHMM → 均为 None
      codes/indices             —— {6位代码: 字段}(字段口径见 collectors.gtimg_quote)
      errors                    —— 失败明细 [{scope, code, reason}]
      meta                      —— source/script_version/标的来源/覆盖率
    """
    nominal = slot_nominal_dt(date, slot)
    drift = int(round((captured_at - nominal).total_seconds())) if nominal else None
    return {
        "date": date,
        "slot": slot,
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "nominal_at": nominal.isoformat(timespec="seconds") if nominal else None,
        "drift_seconds": drift,
        "codes": codes_data,
        "indices": indices_data,
        "errors": errors,
        "meta": {
            "source": SOURCE,
            "script_version": SCRIPT_VERSION,
            "script": "tools.pipeline.intraday_snapshot",
            "targets": sources,
            "index_codes": dict(INDEX_CODES),
            "codes_ok_n": len(codes_data),
            "codes_err_n": len([e for e in errors if e.get("scope") == "code"]),
            "indices_ok_n": len(indices_data),
            "note": "盘中准实时快照;非投资建议",
        },
    }


def _write_atomic(path: Path, payload: dict) -> None:
    """先写 .tmp 再 rename——避免下游读到写一半的 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def run(slot: str, *, date: str | None = None, codes: list[str] | None = None,
        force: bool = False) -> int:
    """跑一次快照。返回进程退出码(0=成功/跳过,1=全部失败)。"""
    date = date or datetime.now().strftime("%Y-%m-%d")

    if not cal.is_trading_day(date):
        logger.info("跳过:%s 非 A 股交易日(slot=%s)", date, slot)
        return 0

    out = snapshot_path(date, slot)
    if out.exists() and not force:
        logger.info("跳过:快照已存在,不覆盖 %s(要重抓加 --force)", out)
        return 0

    targets, sources = resolve_targets(date, codes)
    logger.info("开始抓取 slot=%s 标的=%d 只(选股md %d ∪ 自选 %d ∪ 显式 %d)+ 指数 %d",
                slot, len(targets), len(sources["pick_codes"]), sources["pool_codes_n"],
                len(sources["explicit_codes"]), len(INDEX_CODES))

    captured_at = datetime.now().astimezone()          # 真实抓取时刻(在抓取前取,不事后编)
    codes_data, code_errors = fetch_codes(targets)
    indices_data, index_errors = fetch_indices()
    errors = [*code_errors, *index_errors]

    if not codes_data and not indices_data:
        logger.error("全部抓取失败(%d 条错误),不落文件、非 0 退出", len(errors))
        return 1

    payload = build_payload(date, slot, captured_at, codes_data, indices_data, errors, sources)
    _write_atomic(out, payload)
    logger.info("落盘 %s:个股 %d 成功/%d 失败,指数 %d,captured_at=%s,drift=%ss",
                out, len(codes_data), len(code_errors), len(indices_data),
                payload["captured_at"], payload["drift_seconds"])
    return 0


# ────────────────────────────── CLI ──────────────────────────────

def _setup_logging() -> None:
    """logs/intraday_snapshot.log + stderr 双写(命名空间 pipeline.intraday_snapshot)。"""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="盘中定时快照(确定性节点):在真实时点抓实时行情落盘")
    ap.add_argument("--slot", default="1030", help="时点槽位,HHMM(默认 1030);非 HHMM 名义时刻为 None")
    ap.add_argument("--codes", default="", help="额外标的,逗号分隔(在 选股md∪自选池 之外追加)")
    ap.add_argument("--date", default=None, help="快照日 YYYY-MM-DD(默认今天;手动补跑用)")
    ap.add_argument("--force", action="store_true", help="覆盖同 slot 已有快照")
    args = ap.parse_args(argv)
    _setup_logging()
    codes = [c for c in args.codes.split(",") if c.strip()]
    try:
        return run(args.slot, date=args.date, codes=codes, force=args.force)
    except Exception as e:                              # 兜底:异常也进日志,退出码非 0
        logger.exception("快照任务异常退出:%s: %s", type(e).__name__, e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
