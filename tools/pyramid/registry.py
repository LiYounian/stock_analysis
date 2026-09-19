"""工具注册表契约（统筹冻结·各窗口不改）。

- `ToolResult`：每工具每票的统一产出。机读字段 `fields` + 人读 `浓缩块`（≤8 行）。
  `to_prompt()` 只吐浓缩块，**禁 json.dumps 进 prompt**（公理 G1/G3）。
- `PyramidTool`：工具协议，实现 `run(as_of, code=None, **kw) -> ToolResult`。
- `register/get/list_tools`：注册与检索；`list_tools` 读 `tool_registry.yaml`（清单 SSOT）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable, Optional, Any
import os

# ── 新鲜度三态（来自 provenance）──
FRESHNESS = ("fresh", "stale", "missing")
# ── 塔层枚举（金字塔三层 + 横切）──
塔层枚举 = ("①塔基", "②消息", "③塔身", "④宏观", "经验", "评估", "基础")
# ── 四面枚举（体检卡组织轴：基本面/技术面/资金面/消息情绪面）+ 卡头/经验 两个非四面槽 ──
# 与 塔层 正交：塔层=金字塔机制分层；面=喂选股 LLM 的个股分析组织面（设计 2026-09-19 §1b/§2c）。
四面枚举 = ("基本面", "技术面", "资金面", "消息情绪面")
面枚举 = 四面枚举 + ("卡头", "经验")

_MAX_浓缩块_行 = 8  # 公理 G3：每工具每票默认 ≤ 8 行文字（统筹冻结）
# G3 例外登记（统筹批准的按工具更高上限）：默认 8 是安全栏（防工具随意膨胀），
# 个别工具经统筹裁定可声明更高上限——ToolResult(max_浓缩块_行=N) 覆盖。
#   · growth_quality=13（2026-09-19·体检卡 v2 财报五维逐维各一句，统筹裁定 A 精细版）。
# 新增例外须统筹批准并在此登记 + 加锁测试（防别的工具偷抬上限、防回退）。


@dataclass
class ToolResult:
    """一个工具对一个标的（或大盘/板块）的产出。

    fields：机读结构字段（数值/枚举）。
    浓缩块：人读文字，≤8 行，每个数值须带【口径】【档位】【一句解释】；进 prompt 用它。
    面：归属四面之一（基本面/技术面/资金面/消息情绪面）或 卡头/经验；None=未标（旧工具兼容）。
        d2_package 按 面 把各工具块重排进四面板（拼装层不写口径，只排版）。
    字段解读：结构化"口径三段" [{名,值,口径,意味}, ...]，Wave2 新工具的规范载体。
        非空且 浓缩块 为空时 __post_init__ 自动派生 浓缩块=render_字段(字段解读)（展示=传输同源）。
        口径须来自工具自己的档位表，拼装层永不现编；四段（名/值/口径/意味）禁止空编。
    freshness：fresh/stale/missing，口径新鲜度。
    防未来：True 表示已断言只用了 as_of 当日及之前的数据。
    max_浓缩块_行：G3 行数上限（默认 8·统筹冻结安全栏）。经统筹批准个别工具可声明更高
        （2026-09-19：growth_quality=13，因体检卡 v2 财报五维逐维各一句，统筹裁定 A 精细版）。
        新增例外须统筹批准 + 在 _MAX_浓缩块_行 处登记 + 加锁测试（见 test_framework G3 锁）。
    """

    name: str
    塔层: str
    as_of: str
    浓缩块: str
    fields: dict[str, Any] = field(default_factory=dict)
    code: Optional[str] = None
    freshness: str = "fresh"
    防未来: bool = True
    source: str = ""
    面: Optional[str] = None
    字段解读: list = field(default_factory=list)
    max_浓缩块_行: int = _MAX_浓缩块_行

    def __post_init__(self) -> None:
        if self.塔层 not in 塔层枚举:
            raise ValueError(f"塔层 非法: {self.塔层!r}，应属 {塔层枚举}")
        if self.freshness not in FRESHNESS:
            raise ValueError(f"freshness 非法: {self.freshness!r}，应属 {FRESHNESS}")
        if self.面 is not None and self.面 not in 面枚举:
            raise ValueError(f"面 非法: {self.面!r}，应属 {面枚举}")
        # 字段解读结构校验（口径三段不空编）——名/口径/意味 非空，值须有键
        for it in self.字段解读:
            if not isinstance(it, dict) or "值" not in it or not all(
                str(it.get(k, "")).strip() for k in ("名", "口径", "意味")
            ):
                raise ValueError(
                    f"工具 {self.name} 字段解读项非法（名/口径/意味 禁空编）: {it!r}"
                )
        # 字段解读非空 且 浓缩块为空 → 自动派生（同源：展示由字段解读渲染·按本工具上限）
        if self.字段解读 and not str(self.浓缩块).strip():
            from tools.pyramid._common import render_字段
            self.浓缩块 = render_字段(self.字段解读, max_lines=self.max_浓缩块_行)
        # 浓缩块行数硬约束（G3）——按工具上限（默认 8·统筹冻结；经批准个别更高）；超限即契约违规，早失败
        n = len([ln for ln in str(self.浓缩块).splitlines() if ln.strip()])
        if n > self.max_浓缩块_行:
            raise ValueError(
                f"工具 {self.name} 浓缩块 {n} 行 > 上限 {self.max_浓缩块_行}（G3·默认{_MAX_浓缩块_行}）"
            )

    def to_prompt(self) -> str:
        """进 Agent prompt 的唯一入口：只吐浓缩块 + 新鲜度旗标，绝不吐裸 json。"""
        flag = "" if self.freshness == "fresh" else f"　[{self.freshness}]"
        head = f"【{self.name}·{self.塔层}】as_of={self.as_of}{flag}"
        return head + "\n" + self.浓缩块.rstrip()


@runtime_checkable
class PyramidTool(Protocol):
    name: str
    塔层: str

    def run(self, as_of: str, code: Optional[str] = None, **kw) -> ToolResult:
        ...


_REGISTRY: dict[str, PyramidTool] = {}


def register(tool: PyramidTool) -> PyramidTool:
    """注册一个工具实例（幂等：同名覆盖，便于热重载测试）。"""
    if not getattr(tool, "name", None):
        raise ValueError("工具缺 name")
    if getattr(tool, "塔层", None) not in 塔层枚举:
        raise ValueError(f"工具 {tool.name} 塔层非法: {getattr(tool,'塔层',None)!r}")
    _REGISTRY[tool.name] = tool
    return tool


def get(name: str) -> PyramidTool:
    if name not in _REGISTRY:
        raise KeyError(
            f"未注册工具 {name!r}；已注册 {sorted(_REGISTRY)}。"
            "（工具窗口需先 import 其模块触发 register）"
        )
    return _REGISTRY[name]


def all_names() -> list[str]:
    """当前进程内已注册的工具名。"""
    return sorted(_REGISTRY)


def _yaml_path() -> str:
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(here, "tools", "config", "tool_registry.yaml")


def list_tools() -> list[dict]:
    """读 tool_registry.yaml（清单 SSOT）——Agent 开工先读它知道有哪些工具。

    yaml 缺失或不可解析时回退到进程内已注册工具的最小信息，绝不抛给调用方。
    """
    path = _yaml_path()
    try:
        import yaml  # 延迟导入，避免无 yaml 环境下 import registry 即崩

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        tools = data.get("tools") or []
        if isinstance(tools, list) and tools:
            return tools
    except Exception:
        pass
    return [{"name": n, "塔层": getattr(_REGISTRY[n], "塔层", "")} for n in all_names()]
