"""塔层工具实现包。import 各模块即触发 register()。

P1a 仅落 entry_price（样板）；窗1/2/3 落其余 6 个后在此登记 import。
"""
from tools.pyramid.tools import entry_price_tool  # noqa: F401
from tools.pyramid.tools import experience_rules_tool  # noqa: F401  窗3
from tools.pyramid.tools import fake_good_news_tool  # noqa: F401  窗3

__all__ = ["entry_price_tool", "experience_rules_tool", "fake_good_news_tool"]
