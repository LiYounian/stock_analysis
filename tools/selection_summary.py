"""从每日选股 / 复盘 markdown 抽"精简摘要",供 /selection-analysis 页展示。

设计目标(经验守则 #5 先定输入输出):
  输入 = `docs/每日分析/选股/<date>.md`(盘后选股)、`docs/每日分析/复盘/<date>.md`(复盘)
  输出 = 一个池级视图字典(键 `selection_analysis`),结构:
    {date, 盘后:{选股, 复盘}, 盘中:None, 盘尾:None}
  其中:
    选股 = {env_note(市场环境一句话), ranking:[{rank,code,name,stance,note}], title, src_date}
    复盘 = {direction(方向命中一句话), scorecard:[{code,name,alpha}], experiences:[标题…], title, src_date}

**只抽"排序表 + 关键表态 + 记分表 + 经验标题",绝不搬全文**——逐票深度分析(§3 逐票深度、
逐票资金面盯点等)一律不进摘要。各日 md 版式不统一(表头 代码/名称 vs 票、表态 vs 明确建议 等),
故用启发式定位表/列,匹配不到就降级为空,不抛错(展示层空态兜底)。

纯函数,不触网、不读 store,可独立单测。上传管线经 import_to_db.collect_date 注入该视图。
"""
from __future__ import annotations

import re

from tools.config import settings

SELECTION_DIR = settings.PROJECT_ROOT / "docs" / "每日分析" / "选股"
REVIEW_DIR = settings.PROJECT_ROOT / "docs" / "每日分析" / "复盘"

_CODE6 = re.compile(r"(?<!\d)(\d{6})(?!\d)")   # 6 位 A 股代码(前后非数字)


# ————————————————————————————————————————————————
# 通用小工具
# ————————————————————————————————————————————————
def _clean(s: str) -> str:
    """去 markdown 强调/代码符,收尾空白。"""
    return s.replace("**", "").replace("`", "").strip()


def _short(s: str, n: int = 40) -> str:
    """标题级截断:超 n 字加省略号。"""
    s = s.strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def _first_sentence(s: str, max_len: int = 160) -> str:
    """取第一句(到第一个句号);句号太靠前(<12 字)则并到下一句;整体超长再截断。"""
    s = s.strip()
    idx = s.find("。")
    if 0 < idx < max_len:
        if idx < 12:
            nxt = s.find("。", idx + 1)
            if 0 < nxt < max_len:
                idx = nxt
        return s[: idx + 1]
    return s if len(s) <= max_len else s[:max_len].rstrip() + "…"


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_sep(line: str) -> bool:
    s = line.strip()
    return "-" in s and re.match(r"^\|?[\s:|-]+\|?$", s) is not None


def _parse_tables(text: str) -> list[dict]:
    """把所有 markdown 表格解析成 {heading(所属最近标题), header:[列], rows:[[单元]]}。"""
    lines = text.splitlines()
    tables: list[dict] = []
    heading = ""
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("#"):
            heading = s.lstrip("#").strip()
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and _is_sep(lines[i + 1]):
            header = _split_row(s)
            rows = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                if _is_sep(lines[j]):
                    j += 1
                    continue
                rows.append(_split_row(lines[j]))
                j += 1
            tables.append({"heading": heading, "header": header, "rows": rows})
            i = j
            continue
        i += 1
    return tables


def _col(header: list[str], *keywords: str) -> int:
    """返回表头里首个含任一关键词的列下标;无则 -1。"""
    for idx, h in enumerate(header):
        for kw in keywords:
            if kw in h:
                return idx
    return -1


def _sections(text: str) -> list[tuple[str, str]]:
    """按 markdown 标题切段,返回 [(heading, body)…](保留出现顺序)。"""
    blocks: list[tuple[str, str]] = []
    cur_h, cur = "", []
    for line in text.splitlines():
        if line.strip().startswith("#"):
            if cur_h or cur:
                blocks.append((cur_h, "\n".join(cur)))
            cur_h, cur = line.strip().lstrip("#").strip(), []
        else:
            cur.append(line)
    if cur_h or cur:
        blocks.append((cur_h, "\n".join(cur)))
    return blocks


def _title(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip().lstrip("⚠️ ").strip()
    return ""


def _date_in(text: str) -> str:
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", text)
    return m.group(0) if m else ""


# ————————————————————————————————————————————————
# 选股摘要
# ————————————————————————————————————————————————
def _env_note(text: str) -> str:
    """市场环境一句话:优先 **定性/市场环境：… 行,其次"定性结论"标题下首句,再兜底含"定性"的行。"""
    for raw in text.splitlines():
        s = raw.strip().lstrip(">").strip()
        if re.match(r"^\*\*(定性|市场环境)[：:]", s):
            return _first_sentence(_clean(s))
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        if raw.strip().startswith("#") and ("定性结论" in raw or "定性替代" in raw):
            for j in range(i + 1, min(i + 10, len(lines))):
                c = lines[j].strip().lstrip(">").strip()
                if c and not c.startswith(("#", "|")):
                    return _first_sentence(_clean(c))
    for raw in text.splitlines():
        s = raw.strip().lstrip(">").strip()
        if ("定性" in s or "市场环境" in s) and ("：" in s or ":" in s) \
                and not s.startswith(("#", "|", "-", ">")):
            return _first_sentence(_clean(s))
    return ""


def _ranking(text: str) -> list[dict]:
    """买入建议排序表:代码/名称/表态/一句话。表头含(表态|建议)且含(一句话|理由);
    "买入建议排序"标题下的表优先。代码可单列,亦可嵌在 票/名称 单元(如"瑞芯微 603893")。"""
    best = None
    for t in _parse_tables(text):
        h = t["header"]
        if _col(h, "表态", "建议") >= 0 and _col(h, "一句话", "理由") >= 0:
            score = 2 if "买入建议排序" in t["heading"] else 1
            if best is None or score > best[0]:
                best = (score, t)
    if best is None:
        return []
    t = best[1]
    h = t["header"]
    ci_code = _col(h, "代码")
    ci_name = _col(h, "名称", "票")
    ci_stance = _col(h, "表态", "建议")
    ci_note = _col(h, "一句话", "理由")
    out: list[dict] = []
    for row in t["rows"]:
        if len(row) < len(h):
            row = row + [""] * (len(h) - len(row))
        code = _clean(row[ci_code]) if ci_code >= 0 else ""
        name_cell = _clean(row[ci_name]) if ci_name >= 0 else ""
        m = _CODE6.search(name_cell)
        if not code and m:
            code = m.group(1)
        name = (name_cell[: m.start()] + name_cell[m.end():]).strip() if m else name_cell
        if code:
            name = name.replace(code, "").strip()
        stance = _clean(row[ci_stance]) if ci_stance >= 0 else ""
        note = _clean(row[ci_note]) if ci_note >= 0 else ""
        if not (code or name):
            continue
        out.append({"rank": len(out) + 1, "code": code, "name": name,
                    "stance": stance, "note": note})
    return out


def extract_selection_summary(md_text: str) -> dict:
    """从选股 md 抽精简摘要:{env_note, ranking, title, src_date}。"""
    return {"env_note": _env_note(md_text), "ranking": _ranking(md_text),
            "title": _title(md_text), "src_date": _date_in(md_text)}


# ————————————————————————————————————————————————
# 复盘摘要
# ————————————————————————————————————————————————
def _direction(text: str) -> str:
    """方向命中一句话:优先"方向命中/记分：N"(收盘定稿),否则任一含"方向命中/记分"的行。"""
    cand = []
    for raw in text.splitlines():
        s = _clean(raw.strip().lstrip(">").strip())
        if ("方向命中" in s or "方向记分" in s) and re.search(r"\d", s):
            cand.append(s)
    final = None
    for c in cand:
        if re.search(r"方向(命中|记分).{0,12}[：:]\s*\d", c):
            final = c              # 保留最后一个(盘尾定稿在后)
    if final is None and cand:
        final = cand[-1]
    return _first_sentence(final or "", max_len=120)


def _alpha_col(header: list[str]) -> int:
    """记分表 α 列:优先"α vs …/α…等权"(对等权基准),避开"子链α"。"""
    for idx, h in enumerate(header):
        if "α" in h and ("vs" in h or "等权" in h) and "子链" not in h:
            return idx
    for idx, h in enumerate(header):
        if "α" in h and "子链" not in h:
            return idx
    return -1


def _scorecard(text: str) -> list[dict]:
    """逐票收盘记分表:{code,name,alpha,stance}。表头首列为 票/代码/名称 且含 α 列;
    "逐票收盘记分"优先,退而求"逐票…"(盘中核实)。
    stance = 复盘里记录的该票原始表态列(如"09-04 表态"),缺则空;供排序表「复盘结果」列取用。"""
    best = None
    for t in _parse_tables(text):
        h = t["header"]
        if not h:
            continue
        if _alpha_col(h) >= 0 and any(k in h[0] for k in ("票", "代码", "名称")):
            score = 2 if "逐票收盘记分" in t["heading"] else (1 if "逐票" in t["heading"] else 0)
            if best is None or score > best[0]:
                best = (score, t)
    if best is None:
        return []
    t = best[1]
    ci_alpha = _alpha_col(t["header"])
    ci_stance = _col(t["header"], "表态", "建议", "判定", "结论")
    out: list[dict] = []
    for row in t["rows"]:
        cell0 = _clean(row[0])
        m = _CODE6.search(cell0)
        code = m.group(1) if m else ""
        name = (cell0[: m.start()] + cell0[m.end():]).strip() if m else cell0
        alpha = _clean(row[ci_alpha]) if 0 <= ci_alpha < len(row) else ""
        stance = _clean(row[ci_stance]) if 0 <= ci_stance < len(row) else ""
        if not (code or name):
            continue
        out.append({"code": code, "name": name, "alpha": alpha, "stance": stance})
    return out


def _stance_short(s: str) -> str:
    """把复盘表态压成主表态词:取第一个分隔符（括号/斜杠/顿点/空格）前的部分。
    如"买入(唯一持仓,条件式)/ 偏多·中" → "买入";"规避" → "规避"。"""
    s = _clean(s)
    for sep in ("（", "(", "/", "／", "·", " ", "，", ","):
        i = s.find(sep)
        if i > 0:
            s = s[:i]
            break
    return s.strip()


def _review_index(rev_summary: dict | None) -> dict:
    """按 code 建"复盘结果精简串"索引:主表态词 + α(等权)。供排序表「复盘结果」列取用。
    如 {"002234": "买入 · α+1.23pp"}。无表态/无α则只留有的一项;都无则空串。"""
    idx: dict[str, str] = {}
    if not rev_summary:
        return idx
    for row in rev_summary.get("scorecard") or []:
        code = row.get("code")
        if not code:
            continue
        st = _stance_short(row.get("stance") or "")
        al = (row.get("alpha") or "").strip()
        parts = [p for p in (st, ("α" + al) if al else "") if p]
        idx[code] = " · ".join(parts)
    return idx


def _next_day_review_index(date: str, review_dir) -> tuple[dict, bool]:
    """取"选股日 D 的次一交易日 D+1 复盘"的复盘结果索引(跨日反查)。

    返回 (idx, 已复盘):
      · 已复盘=False —— `复盘/<D+1>.md` 尚不存在(今天选的票还没到次日复盘)→ 排序表列显示"待复盘";
      · 已复盘=True  —— D+1 复盘已存在;idx 按 code 给"主表态词 · α"(等权),未命中该票的 code 取空串(渲染「—」)。
    交易日推进走项目交易日历(collectors.calendar.next_trading_day,不触网,缺日历回退跳周末近似)。
    """
    from tools.collectors import calendar as _cal
    d1 = _cal.next_trading_day(date, allow_fetch=False)
    rev_p = review_dir / f"{d1}.md"
    if not rev_p.is_file():
        return {}, False                                    # D+1 复盘未产出 → 待复盘
    rev = extract_review_summary(rev_p.read_text(encoding="utf-8"))
    return _review_index(rev), True


def enrich_view_cross_refs(view: dict | None, *, review_dir=None) -> dict | None:
    """给三区各时段选股 ranking 行补两列精简信息(幂等):
      - midday(午盘分析):午盘选股产出;当前无该数据源 → 占位空串(展示层渲染「—」);
      - review(复盘结果):**跨日反查**——选股日 D 的票,取其在 D+1(次一交易日)复盘里的回看结果
        (主表态词 · α,按 code 匹配 scorecard),三态:D+1 复盘未产出→"待复盘";已产出但票不在→空串(渲染「—」);
        命中→表态+α。

    语义变更(A-1→跨日):此前 review 取"同一 date"的复盘(评的是前一交易日选的票,与当日选出的票几乎不重叠→
    几乎恒为「—」,无用);现改为取"次一交易日"复盘(正是回看当日 D 选出的票的表现),按 code 对齐。
    注:右侧独立「复盘结果」区块(seg.复盘=当日复盘,评前一日票)语义不变,不受此函数影响。

    review_dir 缺省用模块级 REVIEW_DIR(真实 docs);单测可注入内存目录。"""
    if not view:
        return view
    rev_dir = review_dir or REVIEW_DIR
    date = view.get("date")
    rev_idx, reviewed = ({}, False)
    if date:
        rev_idx, reviewed = _next_day_review_index(date, rev_dir)
    for region in ("盘后", "盘中", "盘尾"):
        seg = view.get(region)
        if not seg:
            continue
        sel = seg.get("选股")
        if not sel or not sel.get("ranking"):
            continue
        for r in sel["ranking"]:
            code = r.get("code") or ""
            r.setdefault("midday", "")            # 午盘数据源尚未落地,占位空
            # 跨日反查覆写(非 setdefault):旧视图里可能残留同日口径的 review 值,须以次日口径为准。
            r["review"] = rev_idx.get(code, "") if reviewed else "待复盘"
    return view


def _experiences(text: str) -> list[str]:
    """新增/确认的经验标题列表。定位"经验…(新增|确认|沉淀)"段,抽其中 #NN 条目标题;
    找不到该段则全文兜底扫"（新增）"条目。返回如 "#21 板块β隔周衰减"、"#23（新增） 逆板块α…"。"""
    blocks = _sections(text)
    exp = [b for b in blocks
           if "经验" in b[0] and ("新增" in b[0] or "确认" in b[0] or "沉淀" in b[0])]
    if not exp:                       # 无结构化经验段 → 宁缺毋滥(不全文扫,避免误抓正文标题/行内引用)
        return []
    out, seen = [], set()
    for raw in exp[-1][1].splitlines():
        s = raw.strip()
        # 仅认"加粗领起的经验条目":**#NN… / **经验 #NN… / **深化经验#NN… / **新增经验#NN…
        # 用加粗包裹排除正文标题(#### 2.)与行内引用(正如经验 #2 …)
        m = re.search(r"\*\*([^*]{0,6}?)(?:经验\s*)?#\s*(\d+)", s)
        if not m:
            continue
        prefix, num = m.group(1), m.group(2)
        if num in seen:
            continue
        rest = s[m.end():]
        is_new = "新增" in prefix
        pm = re.match(r"\s*[（(](.+?)[）)]", rest)
        title = ""
        if pm:
            title = pm.group(1).strip()
            if "新增" in title:
                is_new = True
        if not title or "新增" in title:
            dm = re.search(r"[·:：]\s*(.+)", rest)
            title = _clean(dm.group(1)) if dm else ("" if "新增" in title else title)
        title = _short(_clean(title).rstrip("：: 。"), 40)
        if not title:
            continue
        seen.add(num)
        out.append(f"#{num}" + ("（新增）" if is_new else "") + f" {title}")
    return out


def extract_review_summary(md_text: str) -> dict:
    """从复盘 md 抽精简摘要:{direction, scorecard, experiences, title, src_date}。"""
    return {"direction": _direction(md_text), "scorecard": _scorecard(md_text),
            "experiences": _experiences(md_text), "title": _title(md_text),
            "src_date": _date_in(md_text)}


# ————————————————————————————————————————————————
# 视图构建(供上传管线注入 + web 本地兜底共用)
# ————————————————————————————————————————————————
def build_selection_view(date: str, selection_dir=None, review_dir=None) -> dict | None:
    """构建某日 `selection_analysis` 视图。三区:盘后(选股+复盘精简)、盘中(暂无)、盘尾(暂无)。
    选股/复盘 md 均缺 → 返回 None(不产空视图)。盘中/盘尾数据源尚未落地,固定占位 None。

    A-1 同日配对(右侧独立「复盘结果」区块):选股取 `选股/<date>.md`、复盘取 `复盘/<date>.md`——
    **同一个 date**。语义:某日复盘是复盘"前一交易日选出的票今天表现",故它归属于"当日",应与当日
    选股同页并列。此区块语义不变。
    排序表内「复盘结果」列则为**跨日反查**:选股日 D 的票取其 D+1(次一交易日)复盘的回看结果,
    由 enrich_view_cross_refs 补齐(D+1 复盘未产出→"待复盘";已产出但票不在→「—」;命中→表态+α)。"""
    sel_dir = selection_dir or SELECTION_DIR
    rev_dir = review_dir or REVIEW_DIR
    sel_p = sel_dir / f"{date}.md"
    rev_p = rev_dir / f"{date}.md"
    sel = extract_selection_summary(sel_p.read_text(encoding="utf-8")) if sel_p.is_file() else None
    rev = extract_review_summary(rev_p.read_text(encoding="utf-8")) if rev_p.is_file() else None
    if sel is None and rev is None:
        return None
    view = {"date": date, "盘后": {"选股": sel, "复盘": rev}, "盘中": None, "盘尾": None}
    # 排序表「复盘结果」列跨日反查取 D+1 复盘,故把同一 rev_dir 透传给 enrich(单测注入的内存目录亦生效)。
    return enrich_view_cross_refs(view, review_dir=rev_dir)


# ————————————————————————————————————————————————
# 盘后两表视图:今日选股(D) + 对昨日(D-1)选股的复盘,α 由代码算
#
# 诉求(2026-09-09,/selection-analysis 页):把"每天的选股 ↔ 被选股的复盘"对应上。
#   旧实现的两个缺陷:
#     · 复盘侧直接抽 `复盘/<D>.md` 里那张表 —— 那是"自选盯盘池/盘中核实"表,**不是**对
#       (D-1)实际选出票的复盘;某些日子(D-1 选股跳过)复盘对象干脆成了自选池,与选股完全对不上。
#     · 名称易丢、无"D 选股 ↔ D-1 选股复盘"的日期对应。
#   新口径(本函数):
#     · 今日选股(D):读 `选股/D.md` 买入排序(带名称);每票「复盘结果」= 该票次日 D+1 的 α
#       (从 D+1 数据回填);D+1 未到/未复盘 → "待复盘";D 当天选股跳过/未出 → 整表"结果未出"。
#     · 对昨日(D-1)选股的复盘:取 `选股/D-1.md` **实际选出票**(parse_pick_codes,非自选盯盘池),
#       逐票 名称 + α;α 由代码算:该票 D 日涨跌% − D 日全A等权 mean_pct(口径同项目 α=等权基准),
#       **不 parse 复盘 md 里那张错表**;D-1 选股跳过/未出 → 显式"昨日选股结果未出"。
#
# 本函数是**纯装配**:取数(record.snapshot.pct_chg、breadth mean_pct、名称回退链、交易日历、
# 选股码解析)全部经 provider 注入 —— web 展示层(有 store/breadth)接真实源,单测注入内存 fake,
# 故本模块仍不触网、不读 store、可独立单测。⚠️ 测试环境研究模拟,非投资建议。
# ————————————————————————————————————————————————
_MEAN_PCT_RE = re.compile(r"([+\-−]?\d+(?:\.\d+)?)\s*%")


def parse_equal_weight_mean_pct(md_text: str) -> float | None:
    """从选股/复盘 md 市场表抽「全A等权 mean_pct」(%),作 breadth json 缺失时 α 基准的兜底源。
    定位同时含"全A等权"与"mean_pct"的行,取其中带 % 的首个数值(兼容 ASCII '-' 与 Unicode '−')。
    找不到 → None(上层据此把 α 记 null,不假造)。"""
    for raw in md_text.splitlines():
        s = raw.replace("*", "")
        if "全A等权" in s and "mean_pct" in s:
            m = _MEAN_PCT_RE.search(s)
            if m:
                try:
                    return float(m.group(1).replace("−", "-"))
                except ValueError:
                    return None
    return None


def _fmt_alpha(alpha: float | None) -> str:
    """α 显示串:+X.XXpp / −X.XXpp(用 Unicode 负号,与项目复盘口径一致);None → 空串(模板渲染「—」)。"""
    if not isinstance(alpha, (int, float)) or isinstance(alpha, bool):
        return ""
    return f"{alpha:+.2f}pp".replace("-", "−")


def alpha_scorecard(picks, *, quote_of, benchmark, name_of):
    """逐票 α 记分行(等权基准口径,由代码算,不 parse 复盘 md 记分表):
      picks     选股日**实际选出票**代码(顺序保留;来自 parse_pick_codes)
      quote_of  (code) -> 该票"衡量日"涨跌%(record.snapshot.pct_chg),缺 → None
      benchmark 衡量日全A等权 mean_pct(%);None → 无法算 α
      name_of   (code) -> 名称(回退链,保证不空)
    返回 [{code, name, pct, alpha_val, alpha}]:alpha_val = round(pct − benchmark, 2),
    pct 或 benchmark 任一缺 → alpha_val=None、alpha=''(模板渲染「—」)。"""
    rows = []
    for code in picks:
        pct = quote_of(code)
        alpha_val = None
        if (isinstance(pct, (int, float)) and not isinstance(pct, bool)
                and isinstance(benchmark, (int, float)) and not isinstance(benchmark, bool)):
            alpha_val = round(pct - benchmark, 2)
        rows.append({"code": code, "name": name_of(code) or code, "pct": pct,
                     "alpha_val": alpha_val, "alpha": _fmt_alpha(alpha_val)})
    return rows


def build_postmarket_view(date, *, selection_dir=None, parse_picks=None,
                          prev_trading_day=None, next_trading_day=None,
                          quote_at=None, benchmark_at=None, name_of=None):
    """构建 /selection-analysis「每日盘后选股」区两表(今日选股 D + 对昨日 D-1 选股复盘)。

    provider(全部可注入,web 层接真实源、单测注入 fake):
      parse_picks(md_path) -> [code]     选股 md → 当日实际选出票(权威解析,跳过留痕文件返回空)
      prev_trading_day(D)  -> D-1        上一交易日(被复盘的选股日)
      next_trading_day(D)  -> D+1        下一交易日(今日票的复盘衡量日)
      quote_at(code, md)   -> pct|None   某票某交易日的涨跌%(record.snapshot.pct_chg)
      benchmark_at(md)     -> pct|None   某交易日全A等权 mean_pct(%)(α 基准)
      name_of(code)        -> name       名称回退链(不空)

    返回 {date, sel_date:D, prior_sel_date:D-1, measure_date:D, 选股:{...}, 复盘:{...}}。
    今日选股跳过 → 选股.skipped=True(整表"结果未出");D-1 选股跳过 → 复盘.skipped=True("昨日选股结果未出")。
    """
    sel_dir = selection_dir or SELECTION_DIR
    name_of = name_of or (lambda c: c)
    D = date

    # —— 今日选股(D):读排序 + 名称 + 每票 D+1 α 回填 ——
    sel_p = sel_dir / f"{D}.md"
    sel_text = sel_p.read_text(encoding="utf-8") if sel_p.is_file() else ""
    sel_summary = (extract_selection_summary(sel_text) if sel_text else
                   {"env_note": "", "ranking": [], "title": "", "src_date": ""})
    today_picks = parse_picks(sel_p) if (parse_picks and sel_p.is_file()) else []
    today_skipped = not today_picks                 # md 缺 / 跳过留痕 / 无选出 → 结果未出
    ranking = list(sel_summary.get("ranking") or [])
    if not ranking and today_picks:                 # 排序表没解析到但有 picks → 用 picks 兜底出行
        ranking = [{"rank": i + 1, "code": c, "name": "", "stance": "", "note": ""}
                   for i, c in enumerate(today_picks)]
    for r in ranking:                               # 名称列不能空:走回退链补齐
        if not r.get("name"):
            r["name"] = name_of(r.get("code") or "")
    d_next = next_trading_day(D) if next_trading_day else None
    bench_next = benchmark_at(d_next) if (benchmark_at and d_next) else None
    next_reviewed = isinstance(bench_next, (int, float)) and not isinstance(bench_next, bool)
    for r in ranking:
        r.setdefault("midday", "")                  # 午盘数据源尚未落地,占位空
        if today_skipped:
            r["review"] = ""
        elif not next_reviewed:                     # D+1 广度未产出(次日未到/未复盘)→ 待复盘
            r["review"] = "待复盘"
        else:
            pct = quote_at(r.get("code") or "", d_next) if quote_at else None
            av = (round(pct - bench_next, 2)
                  if isinstance(pct, (int, float)) and not isinstance(pct, bool) else None)
            r["review"] = _fmt_alpha(av)            # 命中→+X.XXpp;该票缺→'' 渲染「—」
    today = {"env_note": sel_summary.get("env_note", ""), "ranking": ranking,
             "title": sel_summary.get("title", ""), "src_date": sel_summary.get("src_date", ""),
             "skipped": today_skipped, "next_date": d_next, "next_reviewed": next_reviewed}

    # —— 对昨日(D-1)选股的复盘:D-1 实际选出票 + 代码算 α(D 日衡量)——
    d_prev = prev_trading_day(D) if prev_trading_day else None
    prev_p = (sel_dir / f"{d_prev}.md") if d_prev else None
    prev_picks = (parse_picks(prev_p)
                  if (parse_picks and prev_p is not None and prev_p.is_file()) else [])
    prev_skipped = not prev_picks
    bench_D = benchmark_at(D) if benchmark_at else None
    scorecard = (alpha_scorecard(prev_picks,
                                 quote_of=lambda c: (quote_at(c, D) if quote_at else None),
                                 benchmark=bench_D, name_of=name_of)
                 if not prev_skipped else [])
    review = {"prior_sel_date": d_prev, "measure_date": D, "benchmark": bench_D,
              "scorecard": scorecard, "skipped": prev_skipped,
              "benchmark_missing": not (isinstance(bench_D, (int, float))
                                        and not isinstance(bench_D, bool))}

    return {"date": D, "sel_date": D, "prior_sel_date": d_prev, "measure_date": D,
            "选股": today, "复盘": review}
