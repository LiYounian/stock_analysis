"""消息面历史重建原型(WI-6 支线 · 研究/原型,非生产采集)。⚠️ 非投资建议。

命题(用户提出):能不能用"每条消息的最早发布/公告时间戳"把历史消息面重建出来,
让 LLM 情绪/新闻那类"只能前向"的信号,变成"可历史回测"?

结论(见分支报告):**只有带真实『发布/公告日』且源支持历史区间拉取的结构化事件
类**(业绩预告 yjyg / 公司公告 cninfo / 龙虎榜 lhb)能干净重建;**泛新闻/舆情/一致
预期不能**(新闻 API 仅回溯 ~2周且幸存者偏差;一致预期只有当前快照)。

本原型选**最干净的一路:业绩预告 yjyg**(有真实『公告日期』、历史可回到 2018),
重建"截至某 as-of 日 T,已公开的业绩预告集合"。

红线(防未来函数):
  - **只保留 公告日期 ≤ T 的记录**(绝不用报告期/事件日回填);
  - 同 (报告期, code) 取**最早公告日**那条(首次预告 = 干净事件锚);
  - 不引入任何 T 之后才可知的字段(如 akshare 给龙虎榜预算的『上榜后N日』收益列)。

用法:
    python -m tools.backtest.reconstruct_asof_msg --periods 20240930 20241231 \
        --asof 2024-11-01 2024-11-15 2025-01-20

自检:重建集合内 max(公告日期) 必 ≤ T(断言);否则报未来泄漏并退出非零。
"""
from __future__ import annotations

import argparse
import logging
import sys
import warnings

import pandas as pd

logger = logging.getLogger("backtest.reconstruct_asof_msg")

_DISCLAIMER = "历史重建原型,仅用于可回测性研究;事件方向仅用公告日及之前信息。非投资建议。"


def _akshare():
    warnings.filterwarnings("ignore")
    pd.set_option("future.infer_string", False)
    import akshare as ak
    return ak


def _find_col(df: pd.DataFrame, *keys: str) -> str | None:
    for k in keys:
        for c in df.columns:
            if k in str(c):
                return c
    return None


def fetch_yjyg_events(periods: list[str]) -> pd.DataFrame:
    """拉若干报告期业绩预告,归一为带真实公告日的事件表。

    Returns df[code, 简称, 报告期, 公告日期(Timestamp), 预告类型, 变动幅度];
    同 (报告期, code) 保留最早公告日一条。akshare 某期失败 → 跳过并 log,不抛。
    """
    ak = _akshare()
    frames: list[pd.DataFrame] = []
    for p in periods:
        try:
            raw = ak.stock_yjyg_em(date=p)
        except Exception as e:  # noqa: BLE001
            logger.warning("yjyg %s 采集失败,跳过: %s", p, e)
            continue
        if raw is None or len(raw) == 0:
            continue
        code_c = _find_col(raw, "代码")
        name_c = _find_col(raw, "简称", "名称")
        disc_c = _find_col(raw, "公告日")
        type_c = _find_col(raw, "预告类型")
        amp_c = _find_col(raw, "业绩变动幅度", "变动幅度")
        if not (code_c and disc_c):
            logger.warning("yjyg %s 缺代码/公告日列,跳过", p)
            continue
        sub = pd.DataFrame({
            "code": raw[code_c].astype(str).str.zfill(6),
            "简称": raw[name_c] if name_c else None,
            "报告期": p,
            "公告日期": pd.to_datetime(raw[disc_c], errors="coerce"),
            "预告类型": raw[type_c] if type_c else None,
            "变动幅度": pd.to_numeric(raw[amp_c], errors="coerce") if amp_c else None,
        }).dropna(subset=["公告日期"])
        # 同(报告期,code)取最早披露
        sub = sub.sort_values("公告日期").drop_duplicates(
            subset=["报告期", "code"], keep="first")
        frames.append(sub)
    if not frames:
        return pd.DataFrame(columns=["code", "简称", "报告期", "公告日期",
                                     "预告类型", "变动幅度"])
    return pd.concat(frames, ignore_index=True)


def reconstruct_asof(events: pd.DataFrame, asof: str) -> pd.DataFrame:
    """重建"截至 as-of 日 T 已公开"的业绩预告集合。

    红线:只保留 公告日期 <= T 的记录(严格防未来函数)。
    """
    T = pd.Timestamp(asof)
    panel = events[events["公告日期"] <= T].copy()
    panel = panel.sort_values("公告日期")
    return panel


def _leakage_check(panel: pd.DataFrame, asof: str) -> None:
    """自检:重建集合内 max(公告日期) 必 <= T。泄漏则退出非零。"""
    if panel.empty:
        return
    T = pd.Timestamp(asof)
    mx = panel["公告日期"].max()
    if mx > T:
        logger.error("未来泄漏!as_of=%s 但集合含 公告日期=%s > T", asof, mx)
        sys.exit(2)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="消息面历史重建原型(yjyg as-of)")
    ap.add_argument("--periods", nargs="+", default=["20240930"],
                    help="报告期 YYYYMMDD(可多个)")
    ap.add_argument("--asof", nargs="+", default=["2024-11-01"],
                    help="as-of 日 YYYY-MM-DD(可多个)")
    args = ap.parse_args(argv)

    print(f"# 消息面历史重建原型(yjyg 业绩预告)  {_DISCLAIMER}")
    events = fetch_yjyg_events(args.periods)
    if events.empty:
        print("无事件(采集全失败或空)。")
        return 1
    print(f"\n拉取报告期={args.periods}:事件(去重后)={len(events)} 条,"
          f"公告日区间={events['公告日期'].min().date()}~{events['公告日期'].max().date()}")

    for asof in args.asof:
        panel = reconstruct_asof(events, asof)
        _leakage_check(panel, asof)
        n = len(panel)
        mxd = panel["公告日期"].max().date() if n else None
        # 方向分布(仅用公告日当日及之前可知的『预告类型』)
        vc = panel["预告类型"].value_counts().head(6).to_dict() if n else {}
        print(f"\n=== as_of {asof} ===")
        print(f"  已公开预告 = {n} 条(max 公告日={mxd} ≤ {asof} ✔ 无未来泄漏)")
        print(f"  预告类型分布(前6): {vc}")
        if n:
            samp = panel.tail(3)[["code", "简称", "公告日期", "预告类型"]]
            for _, r in samp.iterrows():
                print(f"    {r['code']} {r['简称']} 公告日={r['公告日期'].date()} "
                      f"类型={r['预告类型']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
