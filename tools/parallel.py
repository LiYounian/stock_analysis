"""有界、顺序确定的并行 map(候选池富集提速统一入口)。

为什么要这个模块:候选池「消息面回灌」节点对候选逐只串行采集/跑 LLM(~30min),是「当天
选股当天出」的主因之一。IO / LLM 网络型任务用线程池并发即可近线性提速。本模块提供**唯一**的
有界并发原语,各叶子循环(新闻/舆情/情绪LLM/组装/回灌打分)统一调它,保证:

- **有界**:`max_workers = min(workers, n)` → 在飞任务数 ≤ workers(尊重 LLM 网关 429 限流,
  不无脑全开;并发上界即限流上界)。
- **确定性**:结果**按输入顺序回填**(index 对齐),**与完成顺序无关** —— 并发只改「怎么算」、
  不改「算什么」。下游若按序处理/汇总,行为与串行逐值一致。
- **可退串行**:`workers<=1` 或单元素 → 直接串行执行(零线程开销),行为等价旧串行路径
  (kill-switch:config 并发数=1 一键回退)。

失败隔离约定:`fn` **须自行 try/except**、失败返回哨兵值(与各串行循环原本的「单只失败降级
跳过」语义一致);本模块不吞异常语义,`fn` 抛出的异常会原样传播(视为编程错误,不静默)。
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def pmap(fn: Callable[[int, T], R], items: Sequence[T], workers: int) -> list[R]:
    """有界线程池执行 `fn(i, items[i])`,**按输入顺序**回填结果列表。

    Args:
        fn: 处理单元素的可调用,签名 `fn(index, item) -> result`;须自行做失败隔离。
        items: 输入序列。
        workers: 并发度;<=1 或单元素 → 串行(零线程开销,行为等价旧串行)。

    Returns:
        与 `items` **等长、同序**的结果列表(第 i 项 = fn(i, items[i]),与完成顺序无关)。
    """
    n = len(items)
    if n == 0:
        return []
    w = max(1, int(workers or 1))
    if w == 1 or n == 1:
        return [fn(i, items[i]) for i in range(n)]
    results: list = [None] * n
    with ThreadPoolExecutor(max_workers=min(w, n)) as pool:
        futs = {pool.submit(fn, i, items[i]): i for i in range(n)}
        for fut in futs:
            results[futs[fut]] = fut.result()   # 按 index 回填,保持输入顺序(不 as_completed)
    return results
