"""本地端拉取客户端:从远端 ingest 的 GET /pull 增量拉取原始数据 → 写入本地主档。

远端数据仓库 Phase 1 的"本地按需拉取"半环(与 upload.py 的"本地上传"对称)。
  ① 按 kind 组签名请求要素(ts/nonce/key_id)→ sign.pull_envelope 钢印(与 /ingest 同一把密钥)
  ② GET 远端 /pull(带签名头),网络错误 / 5xx 指数退避重试,4xx 视为永久失败
  ③ 校验响应 → 逐股写本地主档(kline→append_master_kline,按 date 幂等去重)
  ④ 推进"上次拉取水位"(data/sync_receipts/pull_<kind>.json)→ 下次只增量拉 date>水位

只入站取数、不改远端;远端不可达 / 拉取失败 → 明确报错 + **保留本地旧数据不清空**。
不重写 tools.sync.* / tools.store.* / 采集层,只编排调用。

CLI:python -m tools.sync.pull --kind kline [--since YYYY-MM-DD] [--codes 000001,600000] [--url ...]
"""
from __future__ import annotations

import argparse
import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path

from tools.config import settings
from tools.store import repo
from tools.sync import sign

logger = logging.getLogger("sync.pull")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _nonce() -> str:
    return uuid.uuid4().hex


def _receipt_dir() -> Path:
    return settings.PROJECT_ROOT / "data" / "sync_receipts"


def _watermark_path(kind: str) -> Path:
    return _receipt_dir() / f"pull_{kind}.json"


def derive_pull_url(ingest_url: str) -> str:
    """从 SYNC_INGEST_URL(.../ingest)推出 /pull 地址;非 /ingest 结尾则原样返回。"""
    u = (ingest_url or "").rstrip("/")
    if u.endswith("/ingest"):
        return u[: -len("/ingest")] + "/pull"
    if u.endswith("/pull"):
        return u
    return u + "/pull" if u else u


# ————————————————————————————————————————————————
# 水位(上次拉取到的最新日期)
# ————————————————————————————————————————————————
def read_watermark(kind: str, path: Path | None = None) -> str | None:
    """读上次拉取水位(该 kind 已收到的最新 date);无则 None(触发全量首拉)。"""
    p = path or _watermark_path(kind)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("last_max_date")
    except (ValueError, OSError):
        return None


def _save_watermark(kind: str, last_max_date: str | None, rows: int, path: Path | None = None) -> None:
    p = path or _watermark_path(kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    obj = {"kind": kind, "last_max_date": last_max_date,
           "last_rows": rows, "last_pull_at": _now_iso()}
    tmp = p.parent / (p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# ————————————————————————————————————————————————
# 签名 GET(含退避重试)
# ————————————————————————————————————————————————
def _default_get(url: str, params: dict, headers: dict):
    """真实 GET:显式 verify=<SYNC_INGEST_CA>(自签证书只信这一张,不动全局 CA)。"""
    import os

    import requests
    ca = os.getenv("SYNC_INGEST_CA")
    try:
        timeout = float(os.getenv("SYNC_PULL_TIMEOUT_S", "") or 120)
    except ValueError:
        timeout = 120.0
    r = requests.get(url, params=params, headers=headers, verify=(ca or True), timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = {"text": r.text[:200]}
    return r.status_code, body


def signed_get(kind: str, since: str, codes: str, *, url: str, token: str, key: str,
               key_id: str, get_fn, retries: int, base_delay: float, sleep_fn) -> tuple[bool, int, dict]:
    """对一次拉取:每次尝试新签(新 ts/nonce)→ GET;网络(0)/5xx 退避重试,4xx 永久失败。
    返回 (成功?, 最后状态码, 响应体)。"""
    status, body = 0, {"error": "no attempt"}
    for attempt in range(retries + 1):
        ts, nonce = _now_iso(), _nonce()
        env = sign.pull_envelope(kind, since, codes, ts, nonce, key_id)
        sig = sign.sign_envelope(env, key)
        headers = {"Authorization": f"Bearer {token}", "X-Sync-Ts": ts,
                   "X-Sync-Nonce": nonce, "X-Sync-Key-Id": key_id, "X-Sync-Sig": sig}
        params = {"kind": kind, "since": since or "", "codes": codes or ""}
        try:
            status, body = get_fn(url, params, headers)
        except Exception as e:                       # 网络异常按可重试处理
            status, body = 0, {"error": str(e)}
        if 200 <= status < 300:
            return True, status, body
        if status != 0 and status < 500:             # 4xx:鉴权/参数错,永久失败
            return False, status, body
        if attempt < retries:                        # 网络/5xx:退避后重试
            sleep_fn(base_delay * (2 ** attempt))
    return False, status, body


# ————————————————————————————————————————————————
# 落本地主档(幂等)
# ————————————————————————————————————————————————
def _persist_kline(data: dict) -> tuple[int, str | None]:
    """逐股把增量 bars append 到本地主档(按 date 去重幂等)。返回 (写入股票数, 最大 date)。"""
    import pandas as pd
    n = 0
    max_date: str | None = None
    for code, bars in (data or {}).items():
        if not bars:
            continue
        df = pd.DataFrame(bars)
        repo.append_master_kline(code, df, meta={"source": "remote_pull"})
        n += 1
        d = max(str(b.get("date")) for b in bars if b.get("date"))
        if d and (max_date is None or d > max_date):
            max_date = d
    return n, max_date


def _pool_pending_buffer() -> Path:
    return _receipt_dir() / "pool_pending.json"


def _persist_pool_pending(rows: list) -> Path:
    """把远端 pending 行整份覆盖写本地缓冲(供 ops.consume_pool_pending 读)。返回路径。

    pool_pending 不用日期水位(去重靠 consumed 回执):每次拉全部 pending,消化标 consumed
    后下次自然变少。故整份覆盖(非 append),原子替换避免半写。
    """
    p = _pool_pending_buffer()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / (p.name + ".tmp")
    tmp.write_text(json.dumps({"pulled_at": _now_iso(), "rows": rows or []},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


# ————————————————————————————————————————————————
# 主流程
# ————————————————————————————————————————————————
def pull(kind: str = "kline", *, url: str, token: str, key: str, key_id: str,
         since: str | None = None, codes: str | None = None,
         get_fn=None, watermark_path: Path | None = None,
         retries: int = 3, base_delay: float = 1.0, sleep_fn=time.sleep,
         advance_watermark: bool = True) -> dict:
    """拉取某 kind 的增量数据并写入本地主档。返回 {ok, kind, since, codes_written, rows, max_date, status?}。

    since 缺省取水位(上次拉到的最新 date)→ 增量;首拉无水位则全量。
    失败(远端不可达 / 非 2xx)→ ok=False + 明确 error,**不动本地旧数据、不推进水位**。
    """
    get_fn = get_fn or _default_get
    if since is None:
        since = read_watermark(kind, watermark_path) or ""
    codes = codes or ""
    logger.info("拉取 %s:since=%s codes=%s url=%s", kind, since or "(全量)", codes or "(全A)", url)

    ok, status, body = signed_get(kind, since, codes, url=url, token=token, key=key,
                                  key_id=key_id, get_fn=get_fn, retries=retries,
                                  base_delay=base_delay, sleep_fn=sleep_fn)
    if not ok:
        err = (body or {}).get("error") or str(body)[:200]
        logger.error("拉取失败(status=%s):%s;保留本地旧数据不动", status, err)
        return {"ok": False, "kind": kind, "since": since, "status": status, "error": err}

    if kind == "pool_pending":
        # 提案队列:整份落缓冲,不走 kline 主档/水位逻辑(去重靠 consumed 回执)
        rows = (body or {}).get("data") or []
        path = _persist_pool_pending(rows)
        logger.info("拉取完成 pool_pending:%d 条 → %s", len(rows), path)
        return {"ok": True, "kind": kind, "rows": len(rows), "buffer": str(path)}
    if kind != "kline":
        return {"ok": False, "kind": kind, "error": f"客户端不支持 kind={kind}"}

    data = (body or {}).get("data") or {}
    n, max_date = _persist_kline(data)
    rows = int((body or {}).get("count") or 0)
    new_wm = max_date or (since or None)
    if advance_watermark:
        _save_watermark(kind, new_wm, rows, watermark_path)
    logger.info("拉取完成 %s:写入 %d 只 / %d 条,水位 %s → %s", kind, n, rows, since or "(全量)", new_wm)
    return {"ok": True, "kind": kind, "since": since, "codes_written": n,
            "rows": rows, "max_date": new_wm}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地端:从远端 ingest /pull 增量拉取原始数据 → 写本地主档")
    ap.add_argument("--kind", default="kline", help="拉取数据类型(先支持 kline)")
    ap.add_argument("--since", default=None, help="增量起点 YYYY-MM-DD;缺省用上次拉取水位(首拉=全量)")
    ap.add_argument("--codes", default=None, help="逗号分隔 6 位码;缺省全A(远端所有已落主档的票)")
    ap.add_argument("--url", default=None, help="远端 /pull 地址;缺省从 SYNC_INGEST_URL 推导")
    ap.add_argument("--retries", type=int, default=3, help="网络/5xx 最大重试次数(指数退避)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    missing = [n for n in ("SYNC_INGEST_TOKEN", "SYNC_SIGNING_KEY")
               if not getattr(settings, n)]
    if missing:
        print(f"缺少必需环境变量:{', '.join(missing)}(见 settings.py 同步配置块 / launchd env 文件)")
        return 2
    url = args.url or derive_pull_url(settings.SYNC_INGEST_URL)
    if not url:
        print("无 /pull 地址:设 SYNC_INGEST_URL(.../ingest)或传 --url")
        return 2

    res = pull(args.kind, url=url, token=settings.SYNC_INGEST_TOKEN,
               key=settings.SYNC_SIGNING_KEY, key_id=settings.SYNC_KEY_ID,
               since=args.since, codes=args.codes, retries=args.retries)
    if res.get("ok"):
        print(f"拉取完成 {res['kind']}:写入 {res.get('codes_written', 0)} 只 / "
              f"{res.get('rows', 0)} 条,水位推进到 {res.get('max_date')}")
        return 0
    print(f"拉取未完成 {res['kind']}(status={res.get('status')}):{res.get('error')};本地旧数据保留")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
