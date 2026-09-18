# -*- coding: utf-8 -*-
"""防复发锁:自动提交「内容回退护栏」——锁死「新版不得丢上一版已有条目 / 检测到删除即拦截还原」。

复现真实事故现场(v2026-09-14 #31/#10 已验证结论被自动提交 082357d 逐字节退回富化前):
经验/复盘结论在**别的 feature worktree**入库到 origin/main → 主仓工作树磁盘留的是入库前
**旧副本** → commit_analysis_docs.sh 第3步 cp -f 用旧副本盖掉刚提交的新内容 → 若不拦截,
自动提交会把这份回退 commit 上去 = 已验证结论静默丢失。

护栏契约(逐条断言,别写松):
  ① 经验沉淀:incoming 丢掉 committed 已有的 §4 编号条目 → 判回退、还原、退码 10、写 marker;
  ② 经验沉淀:incoming 丢掉 committed 已有的 changelog 版本头 → 判回退;
  ③ 通用:incoming 是 committed 的纯删除子集(旧副本覆盖) → 判回退、还原;
  ④ 合法前进编辑(保留全部旧条目、只新增) → **放行**,绝不误伤;
  ⑤ 新增文件(HEAD 无基线) → 放行;
  ⑥ 暂存删除已提交白名单文档 → 判回退、还原;
  ⑦ 端到端:护栏 run 后暂存区里回退文件已回到 origin/main 好版本、好文件仍在。
⚠️ 研究模拟,非投资建议。
"""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tools.ops import docs_commit_guard as g  # noqa: E402

EXP_DIR = "docs/每日分析/经验沉淀"
SEL_DIR = "docs/每日分析/选股"
PATHS = [EXP_DIR, "docs/每日分析/复盘", SEL_DIR, "docs/每日分析/策略建议"]

# 富化版(origin/main 已提交的「好版本」):含 #10、#31 两条编号条目 + 两个 changelog 版本头。
GOOD_EXPERIENCE = """# 经验沉淀 v2026-09-14

## 1. 版本变更记录（changelog）
- **v2026-09-14**：底部超跌反抽回测 CONFIRM，#31/#10 落 SOP。
- **v2026-09-11**：连续普跌重挫日，纪律批次全对。

## 4. 编号条目
**#10 · 选股必做市场环境/β判断（2026-09-02）。**
- 状态：已验证（⑤ capitulation β 例外档，H1-β 回测 CONFIRM 24/24）。

**#31 · 超跌反抽触发闸门定稿（2026-09-14）。**
- 状态：已验证（放量收阳 H2 24/24，比 MA5 更早更准）。
"""

# 旧副本(入库前的磁盘副本):#31 整条缺失、#10 掉了「已验证」状态行、v2026-09-14 changelog 也没有。
STALE_EXPERIENCE = """# 经验沉淀 v2026-09-14

## 1. 版本变更记录（changelog）
- **v2026-09-11**：连续普跌重挫日，纪律批次全对。

## 4. 编号条目
**#10 · 选股必做市场环境/β判断（2026-09-02）。**
- 怎么用：拆 α/β，普跌日买入表态整体降级。
"""

# 合法前进编辑:保留全部旧条目 + changelog，只新增一条 #32 与新版本头。
FORWARD_EXPERIENCE = GOOD_EXPERIENCE + """
**#32 · 新增条目（2026-09-15）。**
- 状态：待验证。
"""


def _run(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


def _commit_all(repo, msg):
    _run(repo, "add", "-A")
    _run(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", msg)


@pytest.fixture()
def repo(tmp_path):
    """真 git 仓:HEAD 提交富化版好文件,模拟 worktree 已 reset 到 origin/main。"""
    r = tmp_path / "wt"
    (r / EXP_DIR).mkdir(parents=True)
    (r / SEL_DIR).mkdir(parents=True)
    _run(r, "init", "-q")
    _run(r, "config", "core.quotepath", "false")
    (r / EXP_DIR / "v2026-09-14.md").write_text(GOOD_EXPERIENCE, encoding="utf-8")
    (r / SEL_DIR / "2026-09-14.md").write_text("买入候选:601061\n", encoding="utf-8")
    _commit_all(r, "good baseline (= origin/main)")
    return r


def _write_and_stage(repo, relpath, content):
    (repo / relpath).write_text(content, encoding="utf-8")
    _run(repo, "add", "--", relpath)


def _staged_content(repo, relpath):
    """暂存区(index)里某文件的内容——护栏 run 后应等于 HEAD 好版本。"""
    p = subprocess.run(
        ["git", "-C", str(repo), "show", f":{relpath}"], capture_output=True, text=True
    )
    return p.stdout


# ——— 纯函数:检测判据 ———

def test_detect_missing_item_header():
    """① 丢 §4 编号条目 → 回退。"""
    reasons = g.detect_regression(f"{EXP_DIR}/v.md", GOOD_EXPERIENCE, STALE_EXPERIENCE)
    assert reasons, "旧副本丢了 #31 应判回退"
    assert any("#31" in x for x in reasons)


def test_detect_missing_changelog_version():
    """② 丢 changelog 版本头 → 回退(即便条目都在也算)。"""
    committed = GOOD_EXPERIENCE
    # incoming 保留两条编号条目,只把 v2026-09-14 changelog 删掉,并加一行别的避免纯删除判据。
    line = "- **v2026-09-14**：底部超跌反抽回测 CONFIRM，#31/#10 落 SOP。\n"
    assert line in committed  # 守住 fixture 字面,改动后立即暴露
    incoming = committed.replace(line, "") + "\n- 附注:新增一行确保非纯删除。\n"
    reasons = g.detect_regression(f"{EXP_DIR}/v.md", committed, incoming)
    assert any("changelog" in x and "2026-09-14" in x for x in reasons)


def test_detect_pure_deletion_generic():
    """③ 通用纯删除子集(非经验沉淀文件也拦) → 回退。"""
    committed = "line-a\nline-b\nline-c\n"
    incoming = "line-a\nline-c\n"  # 删了 line-b,无新增
    reasons = g.detect_regression(f"{SEL_DIR}/x.md", committed, incoming)
    assert reasons and "纯删除" in reasons[0]


def test_forward_edit_not_flagged():
    """④ 合法前进编辑(保留全部旧条目、只新增) → 放行。"""
    reasons = g.detect_regression(f"{EXP_DIR}/v.md", GOOD_EXPERIENCE, FORWARD_EXPERIENCE)
    assert reasons == [], f"合法新增被误伤: {reasons}"


def test_inline_reference_not_counted_as_item():
    """§4 条目提取只认条目头 `**#N ·`,内联引用「强化 #26」不算(避免误判)。"""
    assert g.item_headers("**#31 · 定稿**\n强化 #26/#18") == {"31"}


# ——— 端到端:guard() 还原 + 退码 + marker ———

def test_guard_restores_regressed_experience(repo, tmp_path):
    """①⑦ 旧副本覆盖富化版经验文件 → guard 还原成 HEAD 好版本、好文件不受影响。"""
    _write_and_stage(repo, f"{EXP_DIR}/v2026-09-14.md", STALE_EXPERIENCE)
    # 同批一个合法新增文件,必须不受牵连、仍留在暂存区
    _write_and_stage(repo, f"{SEL_DIR}/2026-09-15.md", "新一日选股:002913\n")

    report = g.guard(str(repo), PATHS, restore=True)

    assert report.has_regression
    assert f"{EXP_DIR}/v2026-09-14.md" in report.restored
    # 暂存区里经验文件已回到富化好版本(#31 回来了)
    staged = _staged_content(repo, f"{EXP_DIR}/v2026-09-14.md")
    assert "**#31 ·" in staged and "已验证（放量收阳" in staged
    # 合法新增文件仍在暂存区
    names = _run(repo, "diff", "--cached", "--name-only").stdout
    assert "2026-09-15.md" in names


def test_guard_allows_forward_edit(repo):
    """④ 合法前进编辑整批放行,暂存区保留新增内容,退码 0。"""
    _write_and_stage(repo, f"{EXP_DIR}/v2026-09-14.md", FORWARD_EXPERIENCE)
    report = g.guard(str(repo), PATHS, restore=True)
    assert not report.has_regression
    staged = _staged_content(repo, f"{EXP_DIR}/v2026-09-14.md")
    assert "**#32 ·" in staged  # 新增条目保住


def test_guard_new_file_allowed(repo):
    """⑤ 新增文件(HEAD 无基线)放行。"""
    _write_and_stage(repo, f"{SEL_DIR}/2026-09-20.md", "全新一日\n")
    report = g.guard(str(repo), PATHS, restore=True)
    assert not report.has_regression


def test_guard_staged_deletion_flagged(repo):
    """⑥ 暂存删除已提交白名单文档 → 判回退、还原。"""
    os.remove(repo / f"{EXP_DIR}/v2026-09-14.md")
    _run(repo, "add", "-A")
    report = g.guard(str(repo), PATHS, restore=True)
    assert report.has_regression
    assert f"{EXP_DIR}/v2026-09-14.md" in report.restored
    assert (repo / f"{EXP_DIR}/v2026-09-14.md").exists()


def test_cli_exit_code_and_marker(repo, tmp_path):
    """CLI 命中回退 → 退码 10 + 写 marker(供监控扫描)。"""
    _write_and_stage(repo, f"{EXP_DIR}/v2026-09-14.md", STALE_EXPERIENCE)
    marker = tmp_path / "alarm.json"
    rc = g.main(["--repo", str(repo), "--paths", *PATHS, "--marker", str(marker)])
    assert rc == 10
    assert marker.exists()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["alarm"] == "docs_commit_regression"
    assert any("v2026-09-14.md" in f["path"] for f in payload["findings"])


def test_cli_clean_exit_zero(repo):
    """CLI 无回退 → 退码 0。"""
    _write_and_stage(repo, f"{SEL_DIR}/2026-09-20.md", "全新一日\n")
    rc = g.main(["--repo", str(repo), "--paths", *PATHS])
    assert rc == 0
