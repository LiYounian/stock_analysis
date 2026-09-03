"""**代码 → 交易所** 的单一真源(零依赖纯函数)。

## 为什么必须只有一处

这条规则曾在项目里被独立实现 9 遍,各自演化 → 2026-09-03 发现北交所现行 920 段
被**同时判错 3 处**(`fa4c37b`),整段 333 只(主档 6.1%,且主档里北交所全部在这一段)
静默取不到数据。同一天还查出第 4~6 处:新浪个股新闻串号、baostock 静默空、
东财 by_report 把 900 沪B 判成深市。**判据只写这一份**,各源要的不同输出形式
(`bj` / `bj920002` / `BJ920002` / `sh.600000`)全部由这里派生。

## 判据(唯一一份,顺序有意义)

    920      → bj   北交所**现行**代码段;必须先于 9→sh,否则整段丢数
    6, 9     → sh   沪市;含 900xxx 沪B(实测东财 by_report 只认 SH900901,SZ900901 报错)
    4, 8     → bj   北交所历史段(43/83/87)
    0, 2, 3  → sz   深主板/深B/创业板
    其余      → None 不是 6 位 A 股代码(指数/港股/脏输入),**不猜**

## 为什么放在 `tools/config/`

"代码段属于哪个交易所"是**领域常量**,既不是采集实现细节,也不是分析口径。
放这一层:①本模块零项目依赖,`tools/config/` 整个目录也只有 `stock_pool → settings`
一条边,不会成环;②`collectors/` 早已 import `tools.config.*`;③`analysis/`、
`pipeline/`、`backtest/` 都能直接 import 而**不产生 analysis→collectors 的反向依赖**
(这正是不把真源放 `collectors/` 的原因);④`stock_pool.is_hk` 已在这一层做代码分类,有先例。

注意与 `collectors/board.py:board_of()` 区分:那个读 `board_membership` 落盘文件查
申万/证监会行业,是**数据查询**(触 IO、会 FileNotFoundError),不是代码段推断,不能当真源。

## 派生形式的"未命中"策略由调用方定

本模块命中不了就返回 `None`,**绝不猜一个前缀**——错前缀不是取不到数,而是可能
**静默拿到别人的数据**(实测:新浪 `sz920002` 返 HTTP 200 + 40 条全站泛资讯,
和 `sh920002`/`sz430047` 前三条完全相同)。各源的兜底策略各不相同(有的回落 sz、
有的原样透传、有的必须显式降级),那是调用方的口径,写在调用方,不藏在这里。
"""
from __future__ import annotations

EXCHANGES = ("sh", "sz", "bj")

# 前缀 → 交易所。**按前缀长度从长到短匹配**(920 必须赢过 9),不是只看首位。
_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("920", "bj"),      # 北交所现行段(先于 9→sh)
    ("6", "sh"),
    ("9", "sh"),        # 900xxx 沪B
    ("4", "bj"),        # 北交所历史段 43xxxx
    ("8", "bj"),        # 北交所历史段 83xxxx / 87xxxx
    ("0", "sz"),
    ("2", "sz"),        # 200xxx 深B
    ("3", "sz"),
)


def exchange_of(code) -> str | None:
    """6 位 A 股代码 → `"sh"` / `"sz"` / `"bj"`;判不出返回 `None`(**唯一判定点**)。

    非 6 位、非全数字一律 `None`——长度不对就不是 A 股代码,别拿前缀去蒙。
    """
    c = str(code).strip()
    if len(c) != 6 or not c.isdigit():
        return None
    for pre, ex in _BY_PREFIX:
        if c.startswith(pre):
            return ex
    return None


def prefixed(code) -> str | None:
    """→ `"bj920002"`(小写前缀 + 代码)。腾讯 gtimg / 新浪日K与新闻页用。判不出 `None`。"""
    ex = exchange_of(code)
    return None if ex is None else f"{ex}{str(code).strip()}"


def upper_prefixed(code) -> str | None:
    """→ `"BJ920002"`(大写前缀 + 代码)。东财 by_report 系接口用。判不出 `None`。"""
    ex = exchange_of(code)
    return None if ex is None else f"{ex.upper()}{str(code).strip()}"


def dotted(code) -> str | None:
    """→ `"sh.600000"`(baostock 协议)。**北交所返回 `None`:baostock 不覆盖北交所。**

    实测(2026-09-03):`bj.920002`/`bj.430047` → error 10004011「股票代码未标识sh或sz」;
    `sz.920002`/`sh.920002` → error_code 0 **success 但 0 行**(静默空,会被误读成
    "这只票没有历史数据")。故北交所在这里显式表达"源不支持",由调用方记降级。
    """
    ex = exchange_of(code)
    if ex is None or ex == "bj":
        return None
    return f"{ex}.{str(code).strip()}"


def is_bj(code) -> bool:
    """是否北交所(920 现行段 + 43/83/87 历史段)。"""
    return exchange_of(code) == "bj"


def is_a_code(code) -> bool:
    """是否 6 位 A 股代码(能判出交易所即为是)。不排除指数代码,调用方另行剔除。"""
    return exchange_of(code) is not None
