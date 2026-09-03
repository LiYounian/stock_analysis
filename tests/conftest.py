"""tests 共享 fixtures。

hermetic_experts:让**依赖磁盘数据**的专家在单测中确定性弃权,使 council/experts 的
红线测试对本地 `data/analysis/<date>/` 缓存免疫(hermetic)。

背景:板块轮动(RRG)/多因子/事件驱动三位专家的数据不在单票 record 字段里,而是从
store 读盘(RRG 读行业/基准 K 线、多因子读横截面 code_view、事件驱动按 code+as_of 汇总)。
本地有缓存时它们**不弃权** → 参与合议、占据分母 → 把"只有技术趋势有数据、其余应弃权"
这类记录的综合分往 0 稀释,使"弃权不稀释 / 分母一致 / 全空看空"等红线断言**时灵时不灵**
(干净环境通过、有缓存环境挂)。这是测试对环境数据的隐式依赖,**不是 council 业务逻辑问题**。

做法:只切断这三位专家的**数据加载入口**(置空/置缺),强制它们走各自真实的
"缺数据 → 弃权(中性+强度0+置信度0+数据充分度=缺失)"分支。**不改任何业务逻辑**,
断言仍真正锁"弃权不稀释"语义;只是让"谁弃权"不再取决于磁盘上恰好有没有缓存。

analysis_tmpdir:把 store 的 analysis 落盘根切到 pytest tmp_path,供"会走真实落盘入口"的
编排类测试用(见文件末尾护栏说明——data/analysis 是 git 跟踪目录,测试产物不能进去)。

no_writes_into_tracked_analysis(autouse):上述污染的兜底护栏,见文件末尾。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def hermetic_experts(monkeypatch):
    """强制 板块轮动/多因子/事件驱动 在无显式 record 数据时确定性弃权(去磁盘依赖)。"""
    from tools.analysis import rrg
    from tools.analysis.event_driven import summary as event_summary
    from tools.store import repo as store

    rrg.clear_cache()  # 清掉可能已从磁盘算出的基准/行业缓存

    # 板块轮动:RRG 行业查询恒空 → expert_板块轮动 走"无 RRG 数据"弃权分支
    monkeypatch.setattr(rrg, "industry_row", lambda name: None)

    # 多因子:横截面 code_view 恒缺 → expert_多因子 走 FileNotFoundError 弃权分支
    _orig_get_code_view = store.get_code_view

    def _no_factor_view(name, code, date="latest"):
        if name == "factor":
            raise FileNotFoundError("hermetic 测试:factor code_view 不可用 → 多因子弃权")
        return _orig_get_code_view(name, code, date)

    monkeypatch.setattr(store, "get_code_view", _no_factor_view)

    # 事件驱动:事件汇总恒空 → expert_事件驱动 走"近期无相关事件"弃权分支
    monkeypatch.setattr(event_summary, "summarize", lambda *a, **k: None)

    yield
    rrg.clear_cache()


@pytest.fixture
def analysis_tmpdir(monkeypatch, tmp_path):
    """把 store 的 analysis 落盘根切到 tmp_path,并返回该目录。

    为什么这么隔离:data/analysis 是**被 git 跟踪**的目录(见文件末尾护栏说明),编排类测试
    (cmd_all / run_two_stage / run_screen_all)哪怕只想锁"步骤顺序/接线",也会顺带跑到真实
    落盘的收尾步。这里只切**唯一 IO 入口**(store._ANALYSIS_DIR),被测编排逻辑一行不改——
    落盘照落、可回读,只是落到临时目录,跑完即弃。
    """
    d = tmp_path / "analysis"
    d.mkdir()
    from tools.store import repo as store
    monkeypatch.setattr(store, "_ANALYSIS_DIR", d)
    return d


# ————————————————————————————————————————————————————————————————
# 全局护栏:测试产物绝不落进 git 跟踪的 data/analysis/
# ————————————————————————————————————————————————————————————————
# 为什么要这条护栏:data/analysis 是**被 git 跟踪**的(合作者靠 git 拉数据看网页,见 .gitignore
# 里的说明),测试一旦走真实落盘入口写进去,既脏工作区(git status 冒出 M/??)、又可能被误提交
# 进版本库——而这种污染只有人事后手动 git status 才会发现,属于"静默欠账"。
#
# 做法:只在**写原语**上拦(不改任何业务逻辑):
#   · store._write_json —— 所有 record / view / code_view 的原子写出口(analysis 侧唯一写口);
#   · DataFrame.to_csv  —— 记分卡类 CSV 产物出口(路径是模块常量、不经 _ANALYSIS_DIR)。
# 命中真实 data/analysis 就**当场失败并指名道姓**,把"测试不 hermetic"变成一条会红的断言。
#
# 测试侧的正确隔离姿势(二选一,均只切 IO 入口、不动被测逻辑):
#   · monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path)  → 落盘改到 pytest 临时目录;
#   · 把"真会落盘的那一步"(闭环收尾步 / screener)桩成计数桩。
class TrackedAnalysisWrite(BaseException):
    """护栏命中信号。

    故意继承 BaseException 而非 Exception:被测的闭环编排到处是 `_safe(...)`/`except Exception`
    的降级兜底(生产上正确——单源失败不该中止闭环),Exception 会被它们**静默吞掉**,
    护栏就退化成"悄悄不写"而不是"报出这个测试不 hermetic"。BaseException 能穿过这些兜底,
    pytest 照样按失败报告,污染才会被当场看见。
    """


@pytest.fixture(autouse=True)
def no_writes_into_tracked_analysis(monkeypatch):
    """任何测试往真实 data/analysis/ 写产物 → 立即失败(而非静默污染 git 工作区)。"""
    import os
    from pathlib import Path

    import pandas as pd

    from tools.config import settings
    from tools.store import repo as store

    tracked = (settings.PROJECT_ROOT / "data" / "analysis").resolve()

    def _under_tracked(p) -> bool:
        try:
            rp = Path(p)
            rp = Path(os.path.normpath(str(rp if rp.is_absolute() else Path.cwd() / rp)))
        except (TypeError, ValueError):        # 非路径(缓冲区等)→ 与磁盘无关,放行
            return False
        return rp == tracked or tracked in rp.parents

    def _boom(p):
        raise TrackedAnalysisWrite(
            f"测试试图写 git 跟踪的 data/analysis:{p}\n"
            "→ 请让该测试 hermetic:monkeypatch store._ANALYSIS_DIR 到 tmp_path,"
            "或把真会落盘的那一步桩掉(见 tests/conftest.py 顶部说明)。")

    _orig_write_json = store._write_json
    _orig_to_csv = pd.DataFrame.to_csv

    def _guarded_write_json(p, obj):
        if _under_tracked(p):
            _boom(p)
        return _orig_write_json(p, obj)

    def _guarded_to_csv(self, path_or_buf=None, *a, **k):
        if isinstance(path_or_buf, (str, os.PathLike)) and _under_tracked(path_or_buf):
            _boom(path_or_buf)
        return _orig_to_csv(self, path_or_buf, *a, **k)

    monkeypatch.setattr(store, "_write_json", _guarded_write_json)
    monkeypatch.setattr(pd.DataFrame, "to_csv", _guarded_to_csv)
