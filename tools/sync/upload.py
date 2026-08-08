"""本地端上传工具:把某日 analysis 产物打包 → 钢印签名 → HTTPS POST 到展示端 ingest。

- 产物枚举复用 import_to_db.collect_date(同一口径,不另写一套)。
- **按票分片**:每只股票(中心记录 + 其按票视图)是一个分片;**每个池级视图各自
  独立成片**(键 `__view__:<name>`,如 __view__:panel / __view__:sentiment_policy)。
  历史上池级视图曾合为单个 `__views__` 大分片,42 池 + 完整分析把它撑到 ~118K 后远端
  稳定超时;按视图拆片后单片更小、可独立签名 / POST / 断点续传,规避大 payload 超时。
  每个分片独立签名(带 key_id/ts/nonce)、独立 POST。
- **失败指数退避重试**;网络错误 / 5xx 退避重试,4xx 视为永久失败不重试。
- **断点续传**:回执记录每个分片成败,重跑跳过已成功分片,只补失败项(可 --force 全重发)。
- 回执落 data/sync_receipts/<date>.json(本地运行态,不入库)。

只出站、不开端口;远端只接收存储、不跑采集/LLM。
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
from tools.sync import import_to_db, sign

logger = logging.getLogger("sync.upload")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _nonce() -> str:
    return uuid.uuid4().hex


def _receipt_dir() -> Path:
    return settings.PROJECT_ROOT / "data" / "sync_receipts"


# ————————————————————————————————————————————————
# 打包分片
# ————————————————————————————————————————————————
VIEW_SHARD_PREFIX = "__view__:"   # 池级视图分片键前缀:__view__:panel / __view__:sentiment_policy …


def build_shards(payload: dict) -> dict[str, dict]:
    """把某日产物切成分片:
    - 每票一个分片:中心记录 + 该票的按票视图;
    - **每个池级视图各自独立成片**(键 `__view__:<name>`),独立签名 / POST / 断点续传。

    早期把所有池级视图合成单个 `__views__` 分片,42 池后该片被 sentiment_policy(~96K)
    + panel(~45K)撑到 ~118K,远端稳定 POST 超时(单个个股分片最大仅 ~47K,均成功)。
    按视图拆片后:panel/screen 各自变成与个股同量级的小片、可独立重试;最大的
    sentiment_policy 也单独成片,失败可精确定位并单独续传,不再拖累其它视图。
    远端 ingest 天然按信封里的 `views` 字典处理,分片键与视图数量对它透明,无需改动。
    """
    shards: dict[str, dict] = {}
    for code, rec in payload["records"].items():
        cv = {name: {code: per[code]} for name, per in payload["code_views"].items() if code in per}
        shards[code] = {"records": {code: rec}, "views": {}, "code_views": cv}
    for name, obj in payload["views"].items():
        shards[f"{VIEW_SHARD_PREFIX}{name}"] = {"records": {}, "views": {name: obj}, "code_views": {}}
    return shards


# ————————————————————————————————————————————————
# 签名 + POST(含退避重试)
# ————————————————————————————————————————————————
def _default_post(url: str, token: str, envelope: dict):
    import os

    import requests
    # 只让"上传"信任自签 ingest 证书,用显式 verify=<证书路径>(SYNC_INGEST_CA);
    # 绝不用全局 REQUESTS_CA_BUNDLE——那会把采集端所有 HTTPS(sina/东财/巨潮…)的 CA 也换成
    # 这张自签证书,导致采集全线 CERTIFICATE_VERIFY_FAILED。verify=True 时走系统/certifi 默认 CA。
    ca = os.getenv("SYNC_INGEST_CA")
    # POST 超时可经 SYNC_UPLOAD_TIMEOUT_S 调,默认放宽到 120s:按视图拆片后单片已很小,
    # 但最大的 sentiment_policy(~96K)单独成片时仍可能偏慢,给足余量避免误判超时(治本
    # 是拆片,这里是兜底;非法值回落默认)。
    try:
        timeout = float(os.getenv("SYNC_UPLOAD_TIMEOUT_S", "") or 120)
    except ValueError:
        timeout = 120.0
    r = requests.post(url, json=envelope, verify=(ca or True),
                      headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = {"text": r.text[:200]}
    return r.status_code, body


def sign_and_post(shard_payload: dict, meta_base: dict, key: str, url: str, token: str,
                  post_fn, retries: int, base_delay: float, sleep_fn) -> tuple[bool, int, str]:
    """对一个分片:每次尝试都新签(新 ts/nonce,防重放),失败按指数退避重试。
    返回 (成功?, 最后状态码, 说明)。4xx 永久失败不重试;网络(0)/5xx 才重试。"""
    status, msg = 0, "no attempt"
    for attempt in range(retries + 1):
        meta = {**meta_base, "ts": _now_iso(), "nonce": _nonce()}
        env = {"meta": meta, **shard_payload}
        env["meta"]["sig"] = sign.sign_envelope(env, key)
        try:
            status, body = post_fn(url, token, env)
        except Exception as e:                       # 网络异常按可重试处理
            status, body = 0, {"error": str(e)}
        if 200 <= status < 300:
            return True, status, "ok"
        msg = str(body)[:200]
        if status != 0 and status < 500:             # 4xx:永久失败,别重试
            return False, status, msg
        if attempt < retries:                        # 网络/5xx:退避后重试
            sleep_fn(base_delay * (2 ** attempt))
    return False, status, msg


# ————————————————————————————————————————————————
# 回执
# ————————————————————————————————————————————————
def _load_receipt(path: Path, date: str) -> dict:
    if path and path.exists():
        try:
            r = json.loads(path.read_text(encoding="utf-8"))
            if r.get("date") == date:
                return r
        except (ValueError, OSError):
            pass
    return {"date": date, "shards": {}}


def _save_receipt(path: Path, receipt: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _summary(receipt: dict) -> dict:
    shards = receipt["shards"]
    ok = sum(1 for v in shards.values() if v.get("ok"))
    return {"total": len(shards), "ok": ok, "failed": len(shards) - ok}


# ————————————————————————————————————————————————
# 主流程
# ————————————————————————————————————————————————
def upload_date(date: str, *, url: str, token: str, source: str, key_id: str, key: str,
                analysis_dir: Path | None = None, receipt_path: Path | None = None,
                post_fn=None, retries: int = 5, base_delay: float = 1.0,
                sleep_fn=time.sleep, force: bool = False) -> dict:
    """打包并上传某日产物;断点续传(跳过已成功分片)。返回回执 dict。"""
    analysis_dir = analysis_dir or import_to_db._analysis_dir()
    payload = import_to_db.collect_date(analysis_dir, date)
    shards = build_shards(payload)
    post_fn = post_fn or _default_post
    receipt = _load_receipt(receipt_path, date) if receipt_path else {"date": date, "shards": {}}
    meta_base = {"date": date, "source": source, "key_id": key_id,
                 "generated_at": _now_iso(), "sig_alg": sign.SIG_ALG}

    for skey, sp in shards.items():
        prev = receipt["shards"].get(skey)
        if prev and prev.get("ok") and not force:
            continue                                  # 断点续传:已成功不重发
        ok, status, msg = sign_and_post(sp, dict(meta_base), key, url, token,
                                        post_fn, retries, base_delay, sleep_fn)
        receipt["shards"][skey] = {"ok": ok, "status": status, "at": _now_iso(), "msg": msg}
        logger.info("分片 %s → %s (status=%s)", skey, "OK" if ok else "FAIL", status)

    receipt["summary"] = _summary(receipt)
    if receipt_path:
        _save_receipt(receipt_path, receipt)
    return receipt


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地端:打包并签名上传某日 analysis 产物到展示端 ingest")
    ap.add_argument("--date", required=True, help="要上传的日期 YYYY-MM-DD")
    ap.add_argument("--force", action="store_true", help="忽略回执,全部分片重发")
    ap.add_argument("--retries", type=int, default=5, help="每分片最大重试次数(指数退避)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    missing = [n for n in ("SYNC_INGEST_URL", "SYNC_INGEST_TOKEN", "SYNC_SIGNING_KEY")
               if not getattr(settings, n)]
    if missing:
        print(f"缺少必需环境变量:{', '.join(missing)}(见 settings.py 同步配置块)")
        return 2

    receipt = upload_date(
        args.date, url=settings.SYNC_INGEST_URL, token=settings.SYNC_INGEST_TOKEN,
        source=settings.SYNC_SOURCE_ID, key_id=settings.SYNC_KEY_ID, key=settings.SYNC_SIGNING_KEY,
        receipt_path=_receipt_dir() / f"{args.date}.json", retries=args.retries, force=args.force)
    s = receipt["summary"]
    print(f"上传完成 {args.date}:共 {s['total']} 分片,成功 {s['ok']},失败 {s['failed']}")
    print(f"回执:{_receipt_dir() / (args.date + '.json')}")
    return 0 if s["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
