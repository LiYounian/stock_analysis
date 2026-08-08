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
