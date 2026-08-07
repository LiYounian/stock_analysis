"""展示端 ingest 收录服务(独立 FastAPI 应用,独立端口 SYNC_INGEST_PORT 默认 8802)。

只做被动收录:接收本地端签名上传的"某日产物包",按序过五关后幂等落库——
  ① Bearer token 鉴权      缺/错 → 401
  ② HMAC 钢印验签          不符 → 403
  ③ 时间戳+nonce 防重放     超窗/重放 → 409
  ④ 时效校验               未来/过老 → 422;旧 generated_at 盖新 → 409
  ⑤ 契约校验               record 不合规 → 422
  → 通过:经 store 公开 API(backend_db)幂等 upsert 落库 + 记 snapshot + 记 nonce
每次请求(成功或失败)都落一条 ingest 审计。展示端只读不算不触网——本服务不 import
任何分析器、不采集、不调 LLM,只写 DB。

独立于展示 web(8801);不塞进 web/app.py。启动:python -m tools.sync.ingest。
"""
from __future__ import annotations

import hmac
import logging
from datetime import datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tools.config import settings
from tools.contracts import validate_record
from tools.store import backend_db
from tools.sync import audit, sign

logger = logging.getLogger("sync.ingest")

app = FastAPI(title="展示端 ingest 收录服务", docs_url=None, redoc_url=None)


class _Reject(Exception):
    """一次收录被拒:带 HTTP 状态码 + 审计 result 标签 + 说明。"""
    def __init__(self, status: int, result: str, msg: str):
        self.status, self.result, self.msg = status, result, msg
        super().__init__(msg)


def _bearer(headers) -> str:
    v = headers.get("authorization", "")
    return v[7:].strip() if v.lower().startswith("bearer ") else ""


def _ts_epoch(ts) -> float | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except ValueError:
        try:
            return float(ts)
        except (TypeError, ValueError):
            return None


def _check_token(headers) -> None:
    token = settings.SYNC_INGEST_TOKEN
    if not token:
        raise _Reject(503, "misconfig", "ingest 未配置 SYNC_INGEST_TOKEN")
    if not hmac.compare_digest(_bearer(headers), token):
        raise _Reject(401, "auth_fail", "token 缺失或不匹配")


def _check_sig(envelope: dict) -> None:
    keys = sign.signing_keys(settings.SYNC_KEY_ID, settings.SYNC_SIGNING_KEY,
                             settings.SYNC_KEY_ID_OLD, settings.SYNC_SIGNING_KEY_OLD)
    if not keys:
        raise _Reject(503, "misconfig", "ingest 未配置 SYNC_SIGNING_KEY")
    if not sign.verify_envelope(envelope, keys):
        raise _Reject(403, "sig_fail", "钢印验签失败(载荷被篡改或密钥不符)")


def _check_replay(meta: dict) -> None:
    ts = _ts_epoch(meta.get("ts"))
    if ts is None:
        raise _Reject(409, "replay", "缺时间戳或不可解析")
    if abs(datetime.now().astimezone().timestamp() - ts) > settings.SYNC_REPLAY_WINDOW_S:
        raise _Reject(409, "replay", "时间戳超出允许窗口")
    nonce = meta.get("nonce")
    if not nonce:
        raise _Reject(409, "replay", "缺 nonce")
    if audit.nonce_seen(nonce):
        raise _Reject(409, "replay", "nonce 已使用(重放)")


def _check_freshness(meta: dict) -> None:
    date = meta.get("date")
    try:
        d = datetime.strptime(str(date), "%Y-%m-%d").date()
    except ValueError:
        raise _Reject(422, "stale", f"date 非法: {date!r}")
    today = datetime.now().date()
    if d > today:
        raise _Reject(422, "stale", f"date 是未来日期: {date}")
    if d < today - timedelta(days=settings.SYNC_MAX_AGE_DAYS):
        raise _Reject(422, "stale", f"date 超出保留窗口 {settings.SYNC_MAX_AGE_DAYS} 天: {date}")
    prev = audit.get_snapshot_generated_at(str(date))
    gen = meta.get("generated_at")
    if prev and gen and str(gen) < str(prev):
        raise _Reject(409, "stale", f"generated_at 早于已收录版本,拒绝旧盖新({gen} < {prev})")


def _check_contracts(records: dict) -> None:
    bad: list[str] = []
    for code, rec in records.items():
        errs = validate_record(rec)
        if errs:
            bad.append(f"{code}: {'; '.join(errs[:3])}")
    if bad:
        raise _Reject(422, "invalid", "契约校验失败: " + " | ".join(bad[:5]))


def _persist(date: str, records: dict, views: dict, code_views: dict) -> int:
    """经 store 公开 API 幂等 upsert 落库(backend_db 的 _upsert 天然幂等)。返回记录数。"""
    for rec in records.values():
        backend_db.put_record(rec, date)
    for name, obj in views.items():
        backend_db.put_view(name, obj, date)
    for name, per_code in code_views.items():
        for code, obj in per_code.items():
            backend_db.put_code_view(name, code, obj, date)
    return len(records)


@app.get("/health")
def health():
    return {"ok": True, "service": "ingest"}


@app.post("/ingest")
async def ingest(request: Request):
    try:
        body = await request.json()
    except Exception:
        audit.record_audit(source=None, key_id=None, date=None, rows=None,
                           verify_ok=False, result="error", msg="请求体非合法 JSON")
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求体非合法 JSON"})

    meta = (body or {}).get("meta") or {}
    src, kid, date = meta.get("source"), meta.get("key_id"), meta.get("date")
    records = (body or {}).get("records") or {}
    try:
        _check_token(request.headers)      # ① 鉴权
        _check_sig(body)                   # ② 验签
        _check_replay(meta)                # ③ 防重放
        _check_freshness(meta)             # ④ 时效
        _check_contracts(records)          # ⑤ 契约
        rows = _persist(date, records, (body.get("views") or {}), (body.get("code_views") or {}))
        audit.remember_nonce(meta["nonce"])
        audit.upsert_snapshot(str(date), str(meta.get("generated_at") or ""), src)
        audit.record_audit(source=src, key_id=kid, date=date, rows=rows,
                           verify_ok=True, result="ok", msg="")
        return {"ok": True, "date": date, "rows": rows}
    except _Reject as r:
        audit.record_audit(source=src, key_id=kid, date=date, rows=len(records) or None,
                           verify_ok=(r.result not in ("auth_fail", "sig_fail")),
                           result=r.result, msg=r.msg)
        return JSONResponse(status_code=r.status, content={"ok": False, "error": r.msg})


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logger.info("ingest 服务启动:端口=%s", settings.SYNC_INGEST_PORT)
    uvicorn.run(app, host="0.0.0.0", port=settings.SYNC_INGEST_PORT)


if __name__ == "__main__":
    main()
