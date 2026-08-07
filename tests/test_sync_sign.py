"""钢印签名/验签单测(tools.sync.sign)。

锁住:同 key 往返验通过;载荷/元数据任一被改验失败;错 key 失败;
密钥轮换——旧 key 签的信封,展示端持"当前+旧"两把时仍验通过,只持当前则失败。
"""
from tools.sync import sign


def _env(sig=None):
    e = {"meta": {"date": "2026-08-07", "source": "local", "ts": "2026-08-07T10:00:00",
                  "nonce": "n1", "key_id": "k1", "sig_alg": "HMAC-SHA256"},
         "records": {"000021": {"meta": {"code": "000021"}, "v": 1}},
         "views": {"panel": {"rows": [1, 2]}}, "code_views": {}}
    if sig is not None:
        e["meta"]["sig"] = sig
    return e


def test_sign_verify_roundtrip():
    e = _env()
    e["meta"]["sig"] = sign.sign_envelope(e, "K")
    assert sign.verify_envelope(e, {"k1": "K"}) is True


def test_tamper_payload_fails():
    e = _env()
    e["meta"]["sig"] = sign.sign_envelope(e, "K")
    e["records"]["000021"]["v"] = 999          # 改一个字节
    assert sign.verify_envelope(e, {"k1": "K"}) is False


def test_tamper_meta_fails():
    e = _env()
    e["meta"]["sig"] = sign.sign_envelope(e, "K")
    e["meta"]["date"] = "2026-08-06"           # 改 meta 也应失败
    assert sign.verify_envelope(e, {"k1": "K"}) is False


def test_wrong_key_fails():
    e = _env()
    e["meta"]["sig"] = sign.sign_envelope(e, "K")
    assert sign.verify_envelope(e, {"k1": "WRONG"}) is False


def test_missing_sig_fails():
    assert sign.verify_envelope(_env(), {"k1": "K"}) is False


def test_key_rotation_old_key_still_verifies():
    # 用旧 key 签(key_id 标 k0)
    e = _env()
    e["meta"]["key_id"] = "k0"
    e["meta"]["sig"] = sign.sign_envelope(e, "OLD")
    keys = sign.signing_keys("k1", "NEW", "k0", "OLD")   # 展示端持当前+旧
    assert sign.verify_envelope(e, keys) is True
    # 只持当前 key 时,旧签名验不过
    assert sign.verify_envelope(e, {"k1": "NEW"}) is False


def test_verify_tries_all_keys_when_keyid_unknown():
    e = _env()
    e["meta"]["key_id"] = "unknown"
    e["meta"]["sig"] = sign.sign_envelope(e, "OLD")
    assert sign.verify_envelope(e, {"k1": "NEW", "k0": "OLD"}) is True
