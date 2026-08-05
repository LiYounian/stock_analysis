"""编排入口:串起 采集 → 分析 → 组合聚合 → 筛重点票 → 深挖 → 出报告。

用法:
    python -m tools.run collect     # 只采集
    python -m tools.run analyze     # 只分析(读缓存)
    python -m tools.run report      # 只出报告
    python -m tools.run all         # 全流程

方案2 流程:先出组合层报告 → 根据 watchlist 对重点票深挖 → 出个股报告。
"""
import sys


def cmd_collect() -> None:
    """采集全池数据到 data/raw/。"""
    raise NotImplementedError("按 P1~P4 逐阶段接入")


def cmd_analyze() -> None:
    """读缓存做技术+情绪分析,组合聚合。"""
    raise NotImplementedError("按 P1~P4 逐阶段接入")


def cmd_report() -> None:
    """出组合层 + 重点票报告。"""
    raise NotImplementedError("按 P3~P4 逐阶段接入")


def cmd_all() -> None:
    cmd_collect()
    cmd_analyze()
    cmd_report()


_CMDS = {"collect": cmd_collect, "analyze": cmd_analyze,
         "report": cmd_report, "all": cmd_all}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in _CMDS:
        print(f"用法: python -m tools.run [{'|'.join(_CMDS)}]")
        return 1
    _CMDS[argv[1]]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
