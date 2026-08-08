"""采集选型基准 + 口径校验(一次性脚本,用数据决策换源与并发度)。

对比三组拉全A日K的方式:
  (a) akshare 串行(现状 fetch_one 逐只多源 fallback);
  (b) akshare 线程池并发 8;
  (c) baostock query_history_k_data_plus(前复权 adjustflag=2)。
量:各自 wall-time、报错/被封数;并做 baostock vs akshare 同票同日 O/H/L/C 逐一对比,
判断能否换源(复权口径必须一致或差异可解释)。

用法:
    python -m tools.collectors.bench_fetch [--n 40] [--start 20250801] [--end 20260807]
结果打印到 stdout,并写 JSON 到 scratchpad(路径见 --out)。

注意:akshare 采样默认 ≤40 只、并发 ≤8,避免与主窗全A采集一起把源打封。
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from tools.collectors import baostock_src, market

# 40 只常见大中盘,覆盖 sh 主板/科创、sz 主板/中小/创业板
BENCH_CODES = [
    "600519", "601318", "600036", "000858", "000001", "600000", "601166", "002415",
    "300750", "000333", "600276", "601888", "000002", "600030", "601012", "002594",
    "600887", "000651", "601398", "600028", "601857", "300059", "002230", "688981",
    "688111", "600585", "601668", "600309", "002304", "000568", "600031", "601899",
    "300760", "002714", "600690", "601088", "000725", "002460", "600438", "300015",
]


def _fmt_dash(d: str) -> str:
    """YYYYMMDD → YYYY-MM-DD。"""
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if "-" not in d else d


# ————————————————————————————————————————————————
# 三组基准
# ————————————————————————————————————————————————
def bench_akshare_serial(codes, start, end):
    ok, err, t0 = {}, {}, time.time()
    for c in codes:
        try:
            ok[c] = market.fetch_one(c, start, end, "qfq")
        except Exception as e:
            err[c] = f"{type(e).__name__}: {str(e)[:60]}"
        time.sleep(market.settings.FETCH_SLEEP_SEC)
    return {"name": "akshare_serial", "wall_s": round(time.time() - t0, 1),
            "ok": len(ok), "err": len(err), "errors": err}, ok


def bench_akshare_pool(codes, start, end, workers=8):
    ok, err, t0 = {}, {}, time.time()

    def _one(c):
        return c, market.fetch_one(c, start, end, "qfq")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, c): c for c in codes}
        for f in as_completed(futs):
            c = futs[f]
            try:
                _, df = f.result()
                ok[c] = df
            except Exception as e:
                err[c] = f"{type(e).__name__}: {str(e)[:60]}"
    return {"name": f"akshare_pool{workers}", "wall_s": round(time.time() - t0, 1),
            "ok": len(ok), "err": len(err), "errors": err}, ok


def bench_baostock(codes, start, end):
    ok, err, t0 = {}, {}, time.time()
    s, e = _fmt_dash(start), _fmt_dash(end)
    login_ok = True
    try:
        with baostock_src.session():
            for c in codes:
                try:
                    ok[c] = baostock_src.fetch_one(c, s, e, adjust="qfq")
                except Exception as ex:
                    err[c] = f"{type(ex).__name__}: {str(ex)[:60]}"
    except Exception as ex:
        login_ok = False
        err["__login__"] = str(ex)
    return {"name": "baostock", "wall_s": round(time.time() - t0, 1),
            "ok": len(ok), "err": len(err), "login_ok": login_ok, "errors": err}, ok


# ————————————————————————————————————————————————
# 口径对比:baostock vs akshare 同票同日 OHLC
# ————————————————————————————————————————————————
def compare_ohlc(ak_data: dict, bs_data: dict):
    """逐票对齐日期,算 O/H/L/C 相对偏差(%)。返回逐票统计 + 汇总。"""
    per, all_rel = [], {"open": [], "high": [], "low": [], "close": []}
    for c in sorted(set(ak_data) & set(bs_data)):
        a = ak_data[c].copy()
        b = bs_data[c].copy()
        a["d"] = pd.to_datetime(a["date"]).dt.strftime("%Y-%m-%d")
        b["d"] = pd.to_datetime(b["date"]).dt.strftime("%Y-%m-%d")
        m = a.merge(b, on="d", suffixes=("_ak", "_bs"))
        if m.empty:
            per.append({"code": c, "overlap": 0})
            continue
        rec = {"code": c, "overlap": int(len(m))}
        for col in ("open", "high", "low", "close"):
            ak_v = m[f"{col}_ak"].astype(float)
            bs_v = m[f"{col}_bs"].astype(float)
            denom = ak_v.abs().replace(0, pd.NA)
            rel = ((bs_v - ak_v).abs() / denom * 100).dropna()
            rec[f"{col}_max_rel%"] = round(float(rel.max()), 4) if len(rel) else None
            rec[f"{col}_mean_rel%"] = round(float(rel.mean()), 4) if len(rel) else None
            all_rel[col].extend(rel.tolist())
        last = m.sort_values("d").iloc[-1]
        rec["last_date"] = last["d"]
        rec["last_close_ak"] = round(float(last["close_ak"]), 3)
        rec["last_close_bs"] = round(float(last["close_bs"]), 3)
        per.append(rec)
    summary = {}
    for col, vals in all_rel.items():
        if vals:
            s = pd.Series(vals)
            summary[col] = {"n": len(vals), "max_rel%": round(float(s.max()), 4),
                            "mean_rel%": round(float(s.mean()), 4),
                            "p95_rel%": round(float(s.quantile(0.95)), 4)}
    return per, summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--start", default="20250801")
    ap.add_argument("--end", default="20260807")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default="bench_result.json")
    ap.add_argument("--skip-serial", action="store_true", help="跳过串行(省时,主窗在跑时用)")
    args = ap.parse_args()

    codes = BENCH_CODES[:args.n]
    print(f"基准:{len(codes)} 只  {args.start}~{args.end}  workers={args.workers}")

    results = {}
    print("\n[c] baostock ...")
    bs_res, bs_data = bench_baostock(codes, args.start, args.end)
    results["baostock"] = bs_res
    print("   ", {k: bs_res[k] for k in ("name", "wall_s", "ok", "err", "login_ok")})

    print(f"\n[b] akshare 线程池并发 {args.workers} ...")
    pool_res, ak_pool_data = bench_akshare_pool(codes, args.start, args.end, args.workers)
    results["akshare_pool"] = pool_res
    print("   ", {k: pool_res[k] for k in ("name", "wall_s", "ok", "err")})

    ak_data = ak_pool_data
    if not args.skip_serial:
        print("\n[a] akshare 串行(含 0.5s sleep)...")
        ser_res, ak_ser_data = bench_akshare_serial(codes, args.start, args.end)
        results["akshare_serial"] = ser_res
        print("   ", {k: ser_res[k] for k in ("name", "wall_s", "ok", "err")})
        if len(ak_ser_data) >= len(ak_pool_data):
            ak_data = ak_ser_data

    print("\n=== 口径对比 baostock vs akshare(前复权)===")
    per, summary = compare_ohlc(ak_data, bs_data)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    payload = {"params": vars(args), "codes": codes, "bench": results,
               "ohlc_summary": summary, "ohlc_per_code": per}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n结果写入 {args.out}")


if __name__ == "__main__":
    main()
