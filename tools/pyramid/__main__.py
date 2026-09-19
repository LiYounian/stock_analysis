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
    p_tool.add_argument("--json", action="store_true",
                        help="dump 机读全量 fields 为 json(而非只印浓缩块);"
                             "下钻工具(如 news_raw 的 url/source/摘要)靠它拿全量")

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
        if args.json:
            # 机读全量:浓缩块是给人/LLM 扫的 ≤8 行摘要,下钻(url/source/摘要/大盘全量结构)靠 fields
            import json as _json
            print(_json.dumps({
                "name": res.name, "塔层": res.塔层, "as_of": res.as_of, "code": res.code,
                "面": res.面, "freshness": res.freshness, "防未来": res.防未来,
                "source": res.source, "浓缩块": res.浓缩块, "fields": res.fields,
            }, ensure_ascii=False, indent=1))
        else:
            print(res.to_prompt())
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
