"""经验库检索(headless 逐票研判用)。

设计:docs/计划/2026-09-13_headless逐票研判生成器与双跑框架_P2实现计划.md §1
      + 架构设计 §3.3(A)/§3.7(拍板2:关键词检索起步,embedding 留后)。

经验沉淀库(`docs/每日分析/经验沉淀/v<date>.md`)累积逐日教训,§4 是编号条目
(`**#N · 标题。**` + `- 为什么：` + `- 怎么用：`)。整包(~65KB)不能塞 prompt,故:
  1) 只取 §4「经验条目」段(到 §5「已知陷阱」为止),按 `**#N` 精确切片成条目;
  2) 按该票的 行业 / 命中策略 / 定性形态 关键词召回相关条目;
  3) 叠加一小撮**高频通用纪律**(常驻,不论命中与否都带),防丢既往红线。

**防未来**:只选版本日期 ≤ pick_date 的最新经验文件(选股在收盘做,当日版本可用)。
纯文本处理,不联网、不调 LLM,可单测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 经验库目录(相对项目根);调用方可传自定义 base 覆盖(测试/多环境)。
_DEFAULT_SUBDIR = ("docs", "每日分析", "经验沉淀")

# 版本文件名形如 v2026-09-11.md
_VER_RE = re.compile(r"^v(\d{4}-\d{2}-\d{2})\.md$")
# 条目头:**#12 · 标题。**
_ENTRY_RE = re.compile(r"^\*\*#(\d+)\s*·\s*(.+?)\*\*\s*$", re.M)
# 条目状态字段(可选,第三子弹):`- 状态：已验证（…）` / `- 状态：待验证`。
# 缺失 = None（存量条目 legacy,不回填、按既有红线沿用);新条目由 eod-review 默认写"待验证"。
_STATUS_RE = re.compile(r"^\s*[-*]\s*状态[:：]\s*(已验证|待验证)", re.M)
# §4 段起止锚点
_SEC4_RE = re.compile(r"^##\s*4\.\s*经验条目", re.M)
_SEC5_RE = re.compile(r"^##\s*5\.", re.M)

# 高频通用纪律:不论命中与否都常驻(按条目号,来自经验库 §4 的普适红线)。
# 选号依据:游资连板不给方向(#1)、辨资金来源(#2)、数据×消息面(#3)、加工分存疑(#4)。
_ALWAYS_ON_IDS = (1, 2, 3, 4)

# 定性形态 → 检索关键词(SOP 步骤0 的四类票 + 常见形态词)。
QUALITATIVE_KEYWORDS = {
    "游资情绪连板": ["游资", "连板", "情绪", "涨停"],
    "趋势成长": ["趋势", "成长", "突破", "均线"],
    "高位妖股超买": ["高位", "超买", "巨量", "妖股", "回吐"],
    "超卖反弹": ["超卖", "反弹", "反包", "广度", "破位"],
}


@dataclass(frozen=True)
class ExperienceEntry:
    id: int
    title: str
    body: str          # 含 为什么/怎么用/状态 全文
    status: str | None = None    # "已验证" / "待验证" / None(存量未标注,按既有红线沿用)

    def snippet(self, max_chars: int = 320) -> str:
        """单条紧凑片段(状态 tag + 标题 + 截断正文),供拼进 prompt。

        状态 tag 前置(不占正文预算)让 headless 研判一眼看到条目可信度:
        待验证条目只轻用/试仓、不当 firm 规则(选股 SOP 据此处理)。
        """
        body = self.body.strip().replace("\n", " ")
        if len(body) > max_chars:
            body = body[:max_chars] + "…"
        tag = {"已验证": "[✓已验证] ", "待验证": "[待验证] "}.get(self.status, "")
        return f"{tag}#{self.id} {self.title} —— {body}"


def latest_version_file(pick_date: str, base_dir: Path | None = None) -> Path | None:
    """选版本日期 ≤ pick_date 的**最新**经验文件(防未来)。无则 None。"""
    base = Path(base_dir) if base_dir else _default_base()
    if not base.is_dir():
        return None
    best: tuple[str, Path] | None = None
    for p in base.glob("v*.md"):
        m = _VER_RE.match(p.name)
        if not m:
            continue
        ver = m.group(1)
        if ver <= pick_date and (best is None or ver > best[0]):
            best = (ver, p)
    return best[1] if best else None


def _default_base() -> Path:
    from tools.config import settings
    return settings.PROJECT_ROOT.joinpath(*_DEFAULT_SUBDIR)


def parse_entries(text: str) -> list[ExperienceEntry]:
    """把经验 md 的 §4「经验条目」段切成条目列表(只取 §4,到 §5 为止)。"""
    m4 = _SEC4_RE.search(text)
    section = text[m4.start():] if m4 else text
    m5 = _SEC5_RE.search(section)
    if m5:
        section = section[:m5.start()]

    heads = list(_ENTRY_RE.finditer(section))
    entries: list[ExperienceEntry] = []
    for i, h in enumerate(heads):
        start = h.end()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(section)
        body = section[start:end].strip()
        sm = _STATUS_RE.search(body)            # 可选状态字段;缺失=None(不回填存量)
        entries.append(ExperienceEntry(
            id=int(h.group(1)), title=h.group(2).strip(), body=body,
            status=(sm.group(1) if sm else None)))
    return entries


def load_entries(pick_date: str, base_dir: Path | None = None) -> tuple[list[ExperienceEntry], str | None]:
    """加载 ≤ pick_date 最新经验文件并切条目。返回 (entries, version_file_name)。缺 → ([], None)。"""
    fp = latest_version_file(pick_date, base_dir)
    if fp is None:
        return [], None
    return parse_entries(fp.read_text(encoding="utf-8")), fp.name


def _keywords(*, industry: str | None, strategy_tags, qualitative: str | None,
              extra_keywords) -> list[str]:
    """据票的画像拼检索关键词(行业切词 + 策略名 + 定性形态词 + 额外词)。"""
    kws: list[str] = []
    if industry:
        # 行业可能形如"航运港口/水运"或无分隔的复合词"航运港口":
        #   ① 按分隔符切词;② 再补 2-gram 滑窗,让"航运港口"能命中只写"航运"的经验条目
        #   (中文无法轻量分词,2-gram 是可解释的近似)。
        for t in re.split(r"[/·、,，\s]+", industry):
            if len(t) >= 2:
                kws.append(t)
                kws += [t[i:i + 2] for i in range(len(t) - 1)]
    for s in (strategy_tags or []):
        if s:
            kws.append(str(s))
    if qualitative and qualitative in QUALITATIVE_KEYWORDS:
        kws += QUALITATIVE_KEYWORDS[qualitative]
    for k in (extra_keywords or []):
        if k:
            kws.append(str(k))
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for k in kws:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def recall(
    entries: list[ExperienceEntry],
    *,
    industry: str | None = None,
    strategy_tags=None,
    qualitative: str | None = None,
    extra_keywords=None,
    top_k: int = 8,
    include_always_on: bool = True,
) -> list[ExperienceEntry]:
    """按关键词召回相关条目 + 常驻高频纪律。返回去重、按相关度(命中词数)降序的条目。

    - 相关度 = 条目(标题+正文)命中的关键词个数;命中 0 的条目不召回(除常驻)。
    - 常驻纪律(_ALWAYS_ON_IDS)始终纳入(排在前),防丢既往红线。
    - top_k 限总条数(含常驻),控制 prompt 体量。
    """
    kws = _keywords(industry=industry, strategy_tags=strategy_tags,
                    qualitative=qualitative, extra_keywords=extra_keywords)

    scored: list[tuple[int, ExperienceEntry]] = []
    for e in entries:
        hay = e.title + "\n" + e.body
        hits = sum(1 for k in kws if k in hay)
        if hits > 0:
            scored.append((hits, e))
    scored.sort(key=lambda t: (-t[0], t[1].id))

    out: list[ExperienceEntry] = []
    seen_ids: set[int] = set()

    if include_always_on:
        by_id = {e.id: e for e in entries}
        for eid in _ALWAYS_ON_IDS:
            e = by_id.get(eid)
            if e and e.id not in seen_ids:
                out.append(e)
                seen_ids.add(e.id)

    for _, e in scored:
        if len(out) >= top_k:
            break
        if e.id not in seen_ids:
            out.append(e)
            seen_ids.add(e.id)

    return out[:top_k]


def render_snippets(entries: list[ExperienceEntry], *, max_chars: int = 320) -> str:
    """把召回条目渲染成 prompt 片段块(每条一行)。空 → 空串。"""
    return "\n".join(f"- {e.snippet(max_chars=max_chars)}" for e in entries)


def recall_snippets_for(
    pick_date: str,
    *,
    industry: str | None = None,
    strategy_tags=None,
    qualitative: str | None = None,
    extra_keywords=None,
    top_k: int = 8,
    base_dir: Path | None = None,
    max_chars: int = 320,
) -> tuple[str, str | None]:
    """一步到位:加载 ≤pick_date 最新经验 → 召回 → 渲染片段。返回 (片段文本, 版本文件名)。"""
    entries, ver = load_entries(pick_date, base_dir)
    if not entries:
        return "", ver
    picked = recall(entries, industry=industry, strategy_tags=strategy_tags,
                    qualitative=qualitative, extra_keywords=extra_keywords, top_k=top_k)
    return render_snippets(picked, max_chars=max_chars), ver
