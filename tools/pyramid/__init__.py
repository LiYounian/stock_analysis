"""金字塔选股 · 工具注册表（D1/P1）。

所有塔层工具的统一契约层。Agent（DeepSeek/千问/Claude Code）与 D2 流程
通过 `registry.get(name).run(as_of, code)` 或 CLI 取用工具，拿到"浓缩块"文字
（每数值带口径/档位/一句解释），**禁止裸 json 进 prompt**。

契约由统筹冻结（ToolResult / PyramidTool），各工具窗口只实现 run() + 注册 +
yaml 条目 + 档位语义锁测试，不改契约。
"""
from tools.pyramid.registry import (
    ToolResult,
    PyramidTool,
    register,
    get,
    list_tools,
    all_names,
)

__all__ = [
    "ToolResult",
    "PyramidTool",
    "register",
    "get",
    "list_tools",
    "all_names",
]
