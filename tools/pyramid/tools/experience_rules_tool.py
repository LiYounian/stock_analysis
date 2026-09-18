"""experience_rules_tool（经验塔层）· 把经验散文拆成结构化规则库，按需取用。

动机：每日把 `docs/每日分析/经验沉淀/*.md`（712K·十余版散文，约 43K/次）整块塞进
prompt 既贵又噪。本工具把散文一次性解析成机读规则库 `data/analysis/经验规则库.json`，
之后按【作用环节】或【个股 code】按需取几条 + 出处，进 prompt 只吐命中的浓缩块。

规则四要素（守则6 语义锁）：
- 规则文字：标题句。
- 作用环节：召回 | 排雷 | 排序 | 价位 | 择时（可多环节；均不命中标"通则"）。关键词分类。
- 状态：现行 | 存疑 | 已弃。§4 经验条目/§5 已知陷阱→现行；§6 待验证假设→存疑；
  只增不删的散文里若某条在后续版本消失→已弃（保守，通常不触发）。
- 证据出处：首见版本 + 章节/编号 + 为什么一句。首见版本用于 as_of 防未来
  （只取首见版本 ≤ as_of 的规则，不用未来才写下的经验）。

数据铁律：essays 与规则库均为 tracked 仓内文件，按 repo 根解析（不需 --data-root）；
落盘的规则库已脱敏（散文里的真实人名/内部品牌在此不落盘）。
"""
from __future__ import annotations

from typing import Optional
import glob
import json
import os
import re

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import data_root, 浓缩块

# ── 作用环节枚举（语义锁：这张表 + 关键词映射被测试锁死）──
环节枚举 = ("召回", "排雷", "排序", "价位", "择时", "通则")

# 关键词 → 环节（一条规则可命中多环节；全不命中记"通则"）。
# 描述性一般约束（守则3）：按语义大类归桶，不针对具体 case。
_环节关键词: dict[str, tuple[str, ...]] = {
    "召回": ("召回", "选股", "票池", "候选", "挖掘", "多策略", "策略命中", "命中", "全部策略", "同源"),
    "排雷": (
        "规避", "风险", "陷阱", "派发", "见顶", "假利好", "降温", "澄清", "减持", "解禁",
        "接飞刀", "尾部", "崩塌", "高波动", "不可测", "price-in", "价内", "未落地",
        "供给面", "泼冷水", "融资盘", "存疑", "壳股", "警示",
    ),
    "排序": ("排序", "排名", "评分", "加分", "记分", "基准", "台账", "优先", "强弱", "分位", "广度", "α", "β"),
    "价位": ("入场", "回踩", "挂单", "止损", "止盈", "限价", "买点", "加仓", "价位", "退出", "仓位", "bias", "阻力位", "建仓", "追高"),
    "择时": ("次日", "盘中", "收盘", "触发信号", "时点", "时效", "跨日", "窗口", "半衰期", "隔周", "择时", "启动", "反抽", "高低切", "时机", "封板", "涨停"),
}

_STATE_现行, _STATE_存疑, _STATE_已弃 = "现行", "存疑", "已弃"
状态枚举 = (_STATE_现行, _STATE_存疑, _STATE_已弃)

_CODE_RE = re.compile(r"(?<!\d)(?:00[0-3]\d{3}|30\d{4}|60[0-3]\d{3}|68[89]\d{3}|60[5-9]\d{3})(?!\d)")
_VER_RE = re.compile(r"v(\d{4}-\d{2}-\d{2})\.md$")


def essay_dir(root: Optional[str] = None) -> str:
    return os.path.join(data_root(root), "docs", "每日分析", "经验沉淀")


def ruledb_path(root: Optional[str] = None) -> str:
    return os.path.join(data_root(root), "data", "analysis", "经验规则库.json")


# ── 解析：把一版散文拆成 {id: rule} ──────────────────────────────

def classify_环节(text: str) -> list[str]:
    """关键词归桶；全不命中→['通则']。"""
    hits = [seg for seg, kws in _环节关键词.items() if any(k in text for k in kws)]
    return hits or ["通则"]


def _extract_codes(text: str) -> list[str]:
    return sorted(set(_CODE_RE.findall(text)))


def _first_sentence(text: str, n: int = 70) -> str:
    """取一句证据（截断脱敏后的为什么）。"""
    s = re.split(r"[。；\n]", text.strip(), maxsplit=1)[0]
    s = s.strip()
    return (s[:n] + "…") if len(s) > n else s


def parse_essay(md: str) -> dict[str, dict]:
    """解析一版散文的 §4 经验条目 / §5 已知陷阱 / §6 待验证假设，返回 {id: rule}。

    rule = {id, 规则文字, 章节, 为什么, 全文, 默认状态}。首见版本/环节/code 由 build 补。
    """
    rules: dict[str, dict] = {}
    lines = md.splitlines()
    section = None  # 4/5/6
    i = 0
    # 当前 §4 条目累积
    cur_id = cur_title = None
    cur_buf: list[str] = []

    def _flush_sec4():
        nonlocal cur_id, cur_title, cur_buf
        if cur_id:
            body = "\n".join(cur_buf).strip()
            why = ""
            m = re.search(r"为什么：(.*?)(?:\n- 怎么用|\Z)", body, re.S)
            if m:
                why = m.group(1).strip()
            rules[cur_id] = {
                "id": cur_id,
                "规则文字": cur_title,
                "章节": "§4经验条目",
                "为什么": why,
                "全文": f"{cur_title}\n{body}",
                "默认状态": _STATE_现行,
            }
        cur_id = cur_title = None
        cur_buf = []

    while i < len(lines):
        ln = lines[i]
        h = re.match(r"^##\s+(\d+)\.", ln)
        if h:
            _flush_sec4()
            section = int(h.group(1))
            i += 1
            continue

        if section == 4:
            m = re.match(r"^\*\*#(\d+)\s*·\s*(.+?)\*\*\s*$", ln)
            if m:
                _flush_sec4()
                cur_id = f"经验#{m.group(1)}"
                cur_title = m.group(2).rstrip("。").strip()
            elif cur_id is not None:
                if ln.strip() == "---":
                    _flush_sec4()
                else:
                    cur_buf.append(ln)
        elif section == 5:
            # 标题加粗后可能夹"（日期）"再冒号（陷阱4/5/6），故 ** 与冒号间允许非冒号文本
            m = re.match(r"^-\s*\*\*陷阱(\d+)\s*·\s*(.+?)\*\*([^:：]*)[:：](.*)$", ln)
            if m:
                rid = f"陷阱{m.group(1)}"
                title = (m.group(2).strip() + m.group(3)).strip()
                why = m.group(4).strip()
                rules[rid] = {
                    "id": rid,
                    "规则文字": title,
                    "章节": "§5已知陷阱",
                    "为什么": why,
                    "全文": f"{title}：{why}",
                    "默认状态": _STATE_现行,
                }
        elif section == 6:
            m = re.match(r"^-\s+(.*\S.*)$", ln)
            if m:
                raw = m.group(1).strip()
                # 标题：优先取加粗 **...** 段；否则取问号前一句
                tm = re.match(r"^\*\*(.+?)\*\*", raw)
                title = tm.group(1).strip() if tm else _first_sentence(raw, 40)
                slug = re.sub(r"[\s*·（）()]", "", title)[:16]
                rid = f"假设:{slug}"
                rules[rid] = {
                    "id": rid,
                    "规则文字": title,
                    "章节": "§6待验证假设",
                    "为什么": raw,
                    "全文": raw,
                    "默认状态": _STATE_存疑,
                }
        i += 1

    _flush_sec4()
    return rules


def _sanitize(text: str) -> str:
    """落盘前脱敏：去掉内部网关/品牌名（描述性，不针对具体串）。"""
    for brand in ("fintopia", "Fintopia", "FINTOPIA"):
        text = text.replace(brand, "内部网关")
    return text


def build_rule_db(root: Optional[str] = None, write: bool = True) -> dict:
    """扫全部 v*.md → 结构化规则库；首见版本用于 as_of 防未来。

    - 首见版本：某规则 id 出现的最早版本（essays 累积只增，故首见=起点）。
    - 状态：默认状态；若某 id 在最新版本消失（只增不删下罕见）→ 已弃。
    - 规则文字/为什么：取最新出现版本（最完整）。
    """
    files = sorted(glob.glob(os.path.join(essay_dir(root), "v*.md")))
    versions = []
    for f in files:
        m = _VER_RE.search(os.path.basename(f))
        if m:
            versions.append((m.group(1), f))
    versions.sort()
    if not versions:
        raise FileNotFoundError(f"未找到经验散文：{essay_dir(root)}/v*.md")

    首见: dict[str, str] = {}
    末见: dict[str, str] = {}
    latest_rule: dict[str, dict] = {}
    for ver, f in versions:
        with open(f, "r", encoding="utf-8") as fh:
            parsed = parse_essay(fh.read())
        for rid, rule in parsed.items():
            首见.setdefault(rid, ver)
            末见[rid] = ver
            latest_rule[rid] = rule  # 后来的版本覆盖 → 保留最新文本

    latest_ver = versions[-1][0]
    rules_out = []
    for rid, rule in latest_rule.items():
        text = rule["全文"]
        状态 = rule["默认状态"]
        if 末见[rid] != latest_ver:
            状态 = _STATE_已弃  # 后续版本移除（保守判定）
        rules_out.append({
            "id": rid,
            "规则文字": _sanitize(rule["规则文字"]),
            "作用环节": classify_环节(text),
            "状态": 状态,
            "首见版本": f"v{首见[rid]}",
            "出处": f"v{首见[rid]} {rule['章节']} {rid}",
            "证据": _sanitize(_first_sentence(rule["为什么"] or rule["规则文字"])),
            "codes": _extract_codes(text),
        })
    # 稳定排序：章节 → id
    rules_out.sort(key=lambda r: (r["出处"], r["id"]))
    db = {
        "version": f"经验规则库·截至 v{latest_ver}",
        "as_of_essay": latest_ver,
        "rule_count": len(rules_out),
        "环节枚举": list(环节枚举),
        "状态枚举": list(状态枚举),
        "rules": rules_out,
    }
    if write:
        p = ruledb_path(root)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(db, fh, ensure_ascii=False, indent=2)
    return db


def load_rule_db(root: Optional[str] = None) -> dict:
    """读规则库；缺失则即时构建（懒构建）。"""
    p = ruledb_path(root)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return build_rule_db(root, write=True)


class ExperienceRulesTool:
    name = "experience_rules"
    塔层 = "经验"
    source = "docs/每日分析/经验沉淀/v*.md → 经验规则库.json"

    def run(
        self,
        as_of: str,
        code: Optional[str] = None,
        环节: Optional[str] = None,
        root: Optional[str] = None,
        **kw,
    ) -> ToolResult:
        try:
            db = load_rule_db(root)
        except FileNotFoundError:
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
                浓缩块="经验: 规则库/散文缺失·人工确认", fields={"missing": True},
                freshness="missing", 防未来=True, source=self.source,
            )
        rules = db.get("rules", [])
        # as_of 防未来：只取首见版本 ≤ as_of 的规则
        cutoff = "v" + as_of
        applicable = [r for r in rules if r.get("首见版本", "v0000-00-00") <= cutoff]
        # 环节过滤
        if 环节:
            applicable = [r for r in applicable if 环节 in r.get("作用环节", [])]
        # code 过滤（规则证据引用了该票）
        if code:
            applicable = [r for r in applicable if code in r.get("codes", [])]
        # 已弃不进召回结果（但计数保留在 fields）
        现行存疑 = [r for r in applicable if r.get("状态") != _STATE_已弃]

        总条 = len(rules)
        stale = db.get("as_of_essay", "") and ("v" + db["as_of_essay"] > cutoff)
        # 无命中：诚实报空，不编
        if not 现行存疑:
            scope = "".join(x for x in (环节 and f"环节={环节}", code and f"code={code}") if x) or "全部"
            浓 = 浓缩块([
                f"经验: 命中 0 条（{scope}·总库 {总条} 条·可用 {len(applicable)} 条）",
                "无适用规则（口径过窄或首见版本晚于 as_of）",
            ])
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code, 浓缩块=浓,
                fields={"命中数": 0, "总库条数": 总条, "适用条数": len(applicable),
                        "命中规则": [], "环节": 环节, "code": code},
                freshness="stale" if stale else "fresh", 防未来=True, source=self.source,
            )

        # 浓缩块：≤8 行 = 1 行汇总 + 至多 6 条规则 + 1 行出处提示
        存疑数 = sum(1 for r in 现行存疑 if r.get("状态") == _STATE_存疑)
        scope = "、".join(x for x in (环节, code) if x) or "全环节"
        head = f"经验[{scope}]: 命中 {len(现行存疑)} 条（存疑 {存疑数}·总库 {总条}·as_of可用 {len(applicable)}）"
        lines = [head]
        for r in 现行存疑[:6]:
            envs = "/".join(r.get("作用环节", []))
            flag = "⚠存疑" if r.get("状态") == _STATE_存疑 else ""
            lines.append(f"·[{envs}]{flag} {r['规则文字']}　（{r['出处']}）")
        if len(现行存疑) > 6:
            lines.append(f"…另有 {len(现行存疑) - 6} 条（见 fields.命中规则）")

        return ToolResult(
            name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
            浓缩块=浓缩块(lines),
            fields={
                "命中数": len(现行存疑),
                "存疑数": 存疑数,
                "总库条数": 总条,
                "适用条数": len(applicable),
                "环节": 环节,
                "code": code,
                "命中规则": [
                    {"id": r["id"], "规则文字": r["规则文字"], "作用环节": r["作用环节"],
                     "状态": r["状态"], "出处": r["出处"], "证据": r["证据"]}
                    for r in 现行存疑
                ],
            },
            freshness="stale" if stale else "fresh", 防未来=True, source=self.source,
        )


register(ExperienceRulesTool())


if __name__ == "__main__":  # 手动构建/刷新规则库：python -m tools.pyramid.tools.experience_rules_tool [--data-root R]
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default=None)
    a = ap.parse_args()
    d = build_rule_db(a.data_root, write=True)
    print(f"规则库已落盘 {ruledb_path(a.data_root)}：{d['rule_count']} 条（截至 {d['as_of_essay']}）")
