"""金字塔工具 CLI。

    python -m tools.pyramid list
    python -m tools.pyramid tool <name> --as-of 2026-09-17 [--code 300308] [--data-root .]

只吐浓缩块（禁裸 json）。Agent 用工具的两种方式之一（另一种是 Python API import registry）。
"""
from __future__ import annotations

import argparse
import sys


def _load_all_tools() -> None:
    # import 触发各工具 register()
    import tools.pyramid.tools  # noqa: F401


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.pyramid")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列注册表清单（读 tool_registry.yaml）")
    p_list.add_argument("--data-root", default=None)

    p_tool = sub.add_parser("tool", help="跑一个工具，打印浓缩块")
    p_tool.add_argument("name")
    p_tool.add_argument("--as-of", required=True)
    p_tool.add_argument("--code", default=None)
    p_tool.add_argument("--data-root", default=None)
    p_tool.add_argument("--method", default=None, help="entry_price 入场方式(可选)")
    p_tool.add_argument("--stage", default=None, help="experience_rules 作用环节(召回/排雷/排序/价位/择时,可选)")

    args = p.parse_args(argv)
    _load_all_tools()
    from tools.pyramid import registry

    if args.cmd == "list":
        for t in registry.list_tools():
            name = t.get("name", "?")
            塔层 = t.get("塔层", "")
            registered = "●" if name in registry.all_names() else "○"
            print(f"{registered} {name:<20} {塔层}")
        return 0

    if args.cmd == "tool":
        kw = {}
        if args.data_root:
            kw["root"] = args.data_root
        if args.method:
            kw["method"] = args.method
        if args.stage:
            kw["环节"] = args.stage
        try:
            tool = registry.get(args.name)
        except KeyError as e:
            print(str(e), file=sys.stderr)
            return 2
        res = tool.run(args.as_of, args.code, **kw)
        print(res.to_prompt())
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
