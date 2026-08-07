"""数据来源"钢印":载荷 HMAC-SHA256 签名 / 验签(本地端签、展示端验,共用一套)。

签名对象 = 除 `meta.sig` 外整个信封的**确定性 JSON 字节**(sort_keys + 固定分隔符 +
UTF-8)。因此信封里任一字节(载荷、date、ts、nonce、key_id、sig_alg…)被改,验签即失败。
对称 HMAC 起步;信封 `meta.sig_alg` 字段留位,将来可切非对称。展示端可持"当前+旧"多把
密钥,按信封 `meta.key_id` 选,选不中再逐把试——支撑密钥轮换窗口平滑切换。

只做签/验的纯函数,不碰网络/DB/配置解析(密钥由调用方从 settings 取好传入)。
"""
from __future__ import annotations

import hashlib
import hmac
import json

SIG_ALG = "HMAC-SHA256"


def canonical_bytes(obj) -> bytes:
    """确定性 JSON 字节:两端对同一结构必得同一字节串(签名/验签的唯一口径)。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def signing_bytes(envelope: dict) -> bytes:
    """取信封的可签字节:剔除 meta.sig,其余(含 meta 其它字段 + 载荷)全参与签名。"""
    meta = {k: v for k, v in (envelope.get("meta") or {}).items() if k != "sig"}
    return canonical_bytes({**envelope, "meta": meta})


def sign_envelope(envelope: dict, key: str) -> str:
    """用 key 对信封算 HMAC-SHA256,返回十六进制签名(不修改入参)。"""
    return hmac.new(key.encode("utf-8"), signing_bytes(envelope), hashlib.sha256).hexdigest()


def verify_envelope(envelope: dict, keys: dict[str, str]) -> bool:
    """验签:keys={key_id: key}。优先用信封 meta.key_id 对应的 key,选不中则逐把试
    (支持轮换窗口:当前+旧密钥都在 keys 里)。任一命中即通过。"""
    meta = envelope.get("meta") or {}
    sig = meta.get("sig")
    if not sig or not keys:
        return False
    kid = meta.get("key_id")
    candidates = [keys[kid]] if kid in keys else list(keys.values())
    for k in candidates:
        if hmac.compare_digest(sig, sign_envelope(envelope, k)):
            return True
    return False


def signing_keys(current_id: str, current_key: str,
                 old_id: str = "", old_key: str = "") -> dict[str, str]:
    """从"当前+旧"密钥拼出 {key_id: key} 表(空值忽略),供 verify_envelope 用。"""
    keys: dict[str, str] = {}
    if current_key:
        keys[current_id or "k1"] = current_key
    if old_key:
        keys[old_id or "k0"] = old_key
    return keys
