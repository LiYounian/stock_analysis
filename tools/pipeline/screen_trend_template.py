"""米勒维尼趋势模板筛选编排:全A → Return250 → 横截面 RPS → 逐票 8 条判定 → 按模式出候选。

一期:CLI + CSV/JSON 导出 + view 留痕(不含 Web)。只做第一层趋势过滤,不含 VCP/买点/下单。

流程(需求 §9):
  ① 取数(离线主档 / 联网)——需 ≥ min_bars(250)有效根
  ② 池内每票算 Return250 → `rps.rps_from_returns` 出 RPS250 横截面百分位
  ③ 逐票 `conditions.evaluate`(喂 rps250 + 当日成交额)→ 每条独立布尔 + pass_mode
  ④ 按所选模式(基础/完整/增强)过滤 → 结构化行 → CSV/JSON 导出 + view
  ⑤ 数据滞后提示:标注实际数据日期,数据未到 as_of 不静默当当日结果

入口:
  python -m tools.pipeline.screen_trend_template [--universe N|--codes ...] \
      [--mode 基础|完整|增强] [--date D] [--no-fetch] [--export csv,json]
⚠️ 非投资建议。未含 VCP / 枢轴点 / 突破放量 / 市场环境判断。
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime

import pandas as pd

from tools.analysis.trend_template import conditions as cond_mod
from tools.analysis.trend_template import indicators as ind
from tools.analysis.trend_template import rps as rps_mod
from tools.collectors import market
from tools.config import settings
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store

logger = logging.getLogger("pipeline.screen_trend_template")

_CFG = THRESHOLDS["趋势模板"]
_MODE_RANK = {"基础": 1, "完整": 2, "增强": 3}
_DISCLAIMER = "非买入信号,未含 VCP / 枢轴点 / 突破放量 / 市场环境判断"
_NAMES_PATH = settings.PROJECT_ROOT / "config" / "code_name.json"
_NAMES: dict[str, str] | None = None


def _names() -> dict[str, str]:
    global _NAMES
    if _NAMES is None:
        try:
            _NAMES = json.loads(_NAMES_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _NAMES = {}
    return _NAMES


def _name_of(code: str) -> str:
    return _names().get(code) or code


def _is_st(name: str) -> bool:
    return "ST" in (name or "").replace(" ", "").upper()


def _skip_code(code: str, cfg: dict) -> bool:
    heads = cfg.get("排除代码头") or []
    return bool(code) and code[0] in heads


def _offline_universe_codes(cfg: dict, limit: int | None = None) -> list[str]:
    """离线宇宙:主档已有的全部代码(排除北交/B 股)。"""
    codes = set(store.list_master_codes())
    raw_root = settings.DATA_RAW
    if raw_root.exists():
        for p in raw_root.glob("**/kline/*.parquet"):
            stem = p.stem
            if len(stem) == 6 and stem.isdigit():
                codes.add(stem)
    out = [c for c in sorted(codes) if not _skip_code(c, cfg)]
    return out[:limit] if limit else out


def _load_or_fetch_kline(code: str, fetch: bool):
    try:
        return market.load_kline_recent(code)
    except FileNotFoundError:
        if not fetch:
            return None
        try:
            return market.fetch_kline([code]).get(code)
        except Exception:  # noqa: BLE001
            return None


def _last_date(kdf) -> str | None:
    if kdf is None or len(kdf) == 0:
        return None
    return str(kdf["date"].iloc[-1])[:10]


def _amount_of(kdf) -> float | None:
    """当日成交额:末根 amount(spot 增量写入);缺列/NaN → None(增强模式不通过,不当 0)。"""
    if kdf is None or len(kdf) == 0 or "amount" not in getattr(kdf, "columns", []):
        return None
    v = kdf["amount"].iloc[-1]
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if ind._valid(v) and v > 0 else None


def _passes(pass_mode: str | None, requested: str) -> bool:
    return pass_mode is not None and _MODE_RANK[pass_mode] >= _MODE_RANK[requested]


def run_trend_template(codes: list[str], as_of: str | None = None, mode: str = "完整",
                       fetch: bool = False, cfg: dict | None = None,
                       export: tuple[str, ...] = ("csv", "json")) -> dict:
    """扫 codes 出趋势模板候选。返回 {rows, 数据日期, 滞后, 异常统计, ...}。"""
    c = cfg or _CFG
    mode = mode if mode in _MODE_RANK else "完整"
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    min_bars = int(c["min_bars"])
    win = int(c["week52_window"])

    # ① 取数 + 组池(算 Return250)
    kmap: dict[str, pd.DataFrame] = {}
    returns: dict[str, float] = {}
    freshest: str | None = None
    st_skip = skipped = 0
    for code in codes:
        if _skip_code(code, c):
            skipped += 1
            continue
        name = _name_of(code)
        if bool(c.get("排除ST", True)) and _is_st(name):
            st_skip += 1
            continue
        kdf = _load_or_fetch_kline(code, fetch)
        if kdf is None or len(kdf) < min_bars:
            skipped += 1
            continue
        kmap[code] = kdf
        ld = _last_date(kdf)
        if ld and (freshest is None or ld > freshest):
            freshest = ld
        r = ind.return_n(kdf["close"].to_numpy(dtype=float), len(kdf) - 1, win)
        if r is not None:
            returns[code] = r

    # ② 横截面 RPS
    rps = rps_mod.rps_from_returns(returns)

    # ③ 逐票判定
    rows: list[dict] = []
    exc = {"INSUFFICIENT_DATA": 0, "INVALID_DATA": 0, "RPS缺失": 0}
    for code, kdf in kmap.items():
        rps250 = rps.get(code)
        if rps250 is None:
            exc["RPS缺失"] += 1
        res = cond_mod.evaluate(kdf, rps250=rps250, amount=_amount_of(kdf), cfg=c)
        if res["异常"]:
            exc[res["异常"]] = exc.get(res["异常"], 0) + 1
            continue
        if not _passes(res["pass_mode"], mode):
            continue
        v, cd = res["values"], res["conditions"]
        rows.append({
            "symbol": code, "name": _name_of(code), "trade_date": _last_date(kdf),
            "close": v["close"], "ma50": v["ma50"], "ma150": v["ma150"], "ma200": v["ma200"],
            "lowest_low_250": v["lowest_low_250"], "highest_high_250": v["highest_high_250"],
            "return250": v["return250"], "rps250": v["rps250"], "amount": v["amount"],
            **{f"condition_a{i}": cd[f"a{i}"] for i in range(1, 9)},
            "pass_mode": res["pass_mode"], "generated_at": generated_at,
        })

    # ④ 排序:RPS 降序 → 涨幅降序 → 代码(§7 支持按 RPS/涨幅/额/代码排序,默认 RPS)
    rows.sort(key=lambda x: (-(x["rps250"] or -1), -(x["return250"] or -1), x["symbol"]))

    # ⑤ 滞后提示
    lag = bool(freshest and freshest < as_of)
    view = {
        "as_of": as_of, "数据日期": freshest, "滞后": lag, "模式": mode,
        "复权": c.get("adjustment"), "RPS池": c.get("universe"),
        "配置版本": c.get("配置版本"), "RPS门槛": c.get("min_rps"),
        "扫描数": len(codes), "有效样本": len(kmap), "候选数": len(rows),
        "跳过数": skipped, "ST跳过": st_skip, "异常统计": exc,
        "generated_at": generated_at, "rows": rows, "免责": _DISCLAIMER,
        "策略": "米勒维尼趋势模板筛选",
    }
    if lag:
        logger.warning("数据滞后:最新数据日期 %s < 目标 %s;结果不代表 %s 当日",
                       freshest, as_of, as_of)
    store.put_view("趋势模板候选", view)
    if export:
        _export(view, export)
    logger.info("趋势模板 %s:扫描 %d / 有效 %d / 候选 %d(数据日 %s%s)",
                mode, len(codes), len(kmap), len(rows), freshest, " 滞后!" if lag else "")
    return view


_CSV_COLS = ["symbol", "name", "trade_date", "close", "ma50", "ma150", "ma200",
             "lowest_low_250", "highest_high_250", "return250", "rps250", "amount",
             *[f"condition_a{i}" for i in range(1, 9)], "pass_mode", "generated_at"]


def _export(view: dict, fmts: tuple[str, ...]) -> dict:
    """CSV / JSON 导出到 data/reports/趋势模板/。返回写出的文件路径。"""
    out_dir = settings.REPORT_DIR / "趋势模板"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{view['as_of']}_{view['模式']}"
    written = {}
    if "json" in fmts:
        p = out_dir / f"{stem}.json"
        p.write_text(json.dumps(view, ensure_ascii=False, indent=2), encoding="utf-8")
        written["json"] = str(p)
    if "csv" in fmts:
        p = out_dir / f"{stem}.csv"
        with p.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=_CSV_COLS)
            w.writeheader()
            for row in view["rows"]:
                w.writerow({k: row.get(k) for k in _CSV_COLS})
        written["csv"] = str(p)
    return written


def _main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="米勒维尼趋势模板筛选(第一层趋势过滤)")
    ap.add_argument("--universe", type=int, metavar="N", help="全A前 N 只(不传=全量)")
    ap.add_argument("--codes", help="逗号分隔指定代码(优先于 --universe)")
    ap.add_argument("--mode", default="完整", choices=["基础", "完整", "增强"])
    ap.add_argument("--date", help="目标日期 YYYY-MM-DD(默认今天;仅用于滞后判定)")
    ap.add_argument("--no-fetch", action="store_true", help="只读本地主档,不触网")
    ap.add_argument("--export", default="csv,json", help="导出格式,逗号分隔;空串=不导出")
    a = ap.parse_args(argv)

    fetch = not a.no_fetch
    if a.codes:
        codes = [x.strip() for x in a.codes.split(",") if x.strip()]
    elif fetch:
        from tools.collectors import universe
        codes = [c for c in universe.universe_codes(limit=a.universe) if not _skip_code(c, _CFG)]
    else:
        codes = _offline_universe_codes(_CFG, limit=a.universe)
    if fetch:
        try:
            market.update_master_from_spot(codes, date=a.date)
        except Exception as e:  # noqa: BLE001
            logger.warning("当日 spot 增量失败,沿用本地主档: %s", e)
    export = tuple(x.strip() for x in a.export.split(",") if x.strip())
    v = run_trend_template(codes, as_of=a.date, mode=a.mode, fetch=fetch, export=export)
    print(f"候选 {v['候选数']} 只(模式={v['模式']} 数据日={v['数据日期']}"
          f"{' 滞后!' if v['滞后'] else ''});{_DISCLAIMER}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
