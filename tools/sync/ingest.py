"""展示端 ingest 收录服务(独立 FastAPI 应用,独立端口 SYNC_INGEST_PORT 默认 8802)。

只做被动收录:接收本地端签名上传的"某日产物包",按序过关后幂等落库——
  ⓪ 请求体大小上限         超限 → 413
  ① Bearer token 鉴权      缺/错 → 401
  ② 速率限制(按 token 滑窗) 超限 → 429
  ③ HMAC 钢印验签          不符 → 403
  ④ 时间戳+nonce 防重放     超窗/重放 → 409
  ⑤ 时效校验               未来/过老 → 422;旧 generated_at 盖新 → 409
  ⑥ 契约校验               record 不合规 → 422
  → 通过:经 store 公开 API(backend_db)幂等 upsert 落库 + 记 snapshot + 记 nonce
每次请求(成功或失败)都落一条 ingest 审计。另提供只读审计查询 /audit(JSON)与
/audit/view(HTML,token 保护)——本服务自持,不碰主展示端 web。防重放表由
`python -m tools.sync.audit` 定时清理(见 ops/ timer 模板)。

展示端只读不算不触网——本服务不 import 任何分析器、不采集、不调 LLM,只写 DB。
独立于展示 web(8801);不塞进 web/app.py。启动:python -m tools.sync.ingest。
"""
from __future__ import annotations

import hmac
import html
import json
import logging
import time
from collections import deque
from datetime import datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from tools.config import settings
from tools.contracts import validate_record
from tools.store import backend_db
from tools.sync import audit, sign

logger = logging.getLogger("sync.ingest")

app = FastAPI(title="展示端 ingest 收录服务", docs_url=None, redoc_url=None)


class _RateLimiter:
    """按 key(token)滑动窗口限流:窗口内计数超上限即拒。进程内内存态(单 worker 足够)。"""
    def __init__(self):
        self._hits: dict[str, deque] = {}

    def allow(self, key: str, max_n: int, window_s: float, now: float) -> bool:
        dq = self._hits.setdefault(key, deque())
        while dq and dq[0] <= now - window_s:
            dq.popleft()
        if len(dq) >= max_n:
            return False
        dq.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()


_rate = _RateLimiter()


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


def _check_body_size(raw: bytes, content_length: str | None) -> None:
    """请求体大小上限:先看 Content-Length 头快速拒,再核实际字节数。超限 413。"""
    limit = settings.SYNC_MAX_BODY_BYTES
    try:
        if content_length and int(content_length) > limit:
            raise _Reject(413, "too_large", f"请求体超上限({content_length}>{limit} 字节)")
    except ValueError:
        pass
    if len(raw) > limit:
        raise _Reject(413, "too_large", f"请求体超上限({len(raw)}>{limit} 字节)")


def _check_rate(token: str) -> None:
    """按 token 滑动窗口限流,超限 429。"""
    if not _rate.allow(token, settings.SYNC_RATE_MAX, settings.SYNC_RATE_WINDOW_S, time.time()):
        raise _Reject(429, "rate", f"超出速率限制({settings.SYNC_RATE_MAX}/{settings.SYNC_RATE_WINDOW_S}s)")


def _audit_authed(request: Request) -> bool:
    """审计查询鉴权:token 走 Bearer 头或 ?token= 查询参数(方便浏览器),比对 SYNC_INGEST_TOKEN。"""
    token = settings.SYNC_INGEST_TOKEN
    if not token:
        return False
    got = _bearer(request.headers) or request.query_params.get("token", "")
    return hmac.compare_digest(got, token)


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
    raw = await request.body()
    src = kid = date = None
    records: dict = {}
    try:
        _check_body_size(raw, request.headers.get("content-length"))   # ⓪ 体积上限(413)
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise _Reject(400, "error", "请求体非合法 JSON")
        meta = (body or {}).get("meta") or {}
        src, kid, date = meta.get("source"), meta.get("key_id"), meta.get("date")
        records = (body or {}).get("records") or {}
        _check_token(request.headers)      # ① 鉴权(401)
        _check_rate(_bearer(request.headers))  # ② 限流(429,按 token)
        _check_sig(body)                   # ③ 验签(403)
        _check_replay(meta)                # ④ 防重放(409)
        _check_freshness(meta)             # ⑤ 时效(422/409)
        _check_contracts(records)          # ⑥ 契约(422)
        rows = _persist(date, records, (body.get("views") or {}), (body.get("code_views") or {}))
        audit.remember_nonce(meta["nonce"])
        audit.upsert_snapshot(str(date), str(meta.get("generated_at") or ""), src)
        audit.record_audit(source=src, key_id=kid, date=date, rows=rows,
                           verify_ok=True, result="ok", msg="")
        return {"ok": True, "date": date, "rows": rows}
    except _Reject as r:
        audit.record_audit(source=src, key_id=kid, date=date, rows=len(records) or None,
                           verify_ok=(r.result not in ("auth_fail", "sig_fail", "too_large")),
                           result=r.result, msg=r.msg)
        return JSONResponse(status_code=r.status, content={"ok": False, "error": r.msg})


@app.get("/audit")
def audit_json(request: Request, limit: int = 100):
    """只读审计查询(JSON),token 保护。?limit=N ?token=<令牌>(或 Bearer 头)。"""
    if not _audit_authed(request):
        return JSONResponse(status_code=401, content={"ok": False, "error": "token 缺失或不匹配"})
    return {"ok": True, "rows": audit.recent_audits(limit)}


@app.get("/audit/view", response_class=HTMLResponse)
def audit_view(request: Request, limit: int = 100):
    """只读审计小页面(HTML 表格),token 保护(?token= 方便浏览器)。ingest 服务自持,不碰主展示端 web。"""
    if not _audit_authed(request):
        return HTMLResponse(status_code=401, content="<h3>401 token 缺失或不匹配</h3>")
    cols = ("id", "at", "source", "key_id", "date", "rows", "verify_ok", "result", "msg")
    head = "".join(f"<th>{c}</th>" for c in cols)
    body_rows = []
    for r in audit.recent_audits(limit):
        tds = "".join(f"<td>{html.escape(str(r.get(c, '')))}</td>" for c in cols)
        body_rows.append(f"<tr>{tds}</tr>")
    table = f"<table border=1 cellpadding=4><tr>{head}</tr>{''.join(body_rows)}</table>"
    return HTMLResponse(content=f"<html><head><meta charset='utf-8'><title>ingest 审计</title></head>"
                                f"<body><h3>ingest 审计(最近 {limit} 条)</h3>{table}</body></html>")


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logger.info("ingest 服务启动:端口=%s", settings.SYNC_INGEST_PORT)
    uvicorn.run(app, host="0.0.0.0", port=settings.SYNC_INGEST_PORT)


if __name__ == "__main__":
    main()
