"""本地端上传工具单测(tools.sync.upload)。

锁住:分片打包(每票+池级视图)/ 指数退避重试(网络/5xx 重试、4xx 不重试)/
断点续传(回执跳过已成功,只补失败项)。用假 post_fn + 假 sleep,不触网。
"""
import json

from tools.sync import upload


def _payload():
    return {
        "records": {"000021": {"meta": {"code": "000021"}}, "600519": {"meta": {"code": "600519"}}},
        "views": {"panel": {"rows": []}},
        "code_views": {"chart": {"000021": {"close": [1.0]}}},
    }


def _seed_analysis(root, date):
    d = root / date
    (d / "chart").mkdir(parents=True)
    (d / "000021.json").write_text(json.dumps({"meta": {"code": "000021"}}), encoding="utf-8")
    (d / "600519.json").write_text(json.dumps({"meta": {"code": "600519"}}), encoding="utf-8")
    (d / "panel.json").write_text(json.dumps({"rows": []}), encoding="utf-8")
    (d / "chart" / "000021.json").write_text(json.dumps({"close": [1.0]}), encoding="utf-8")


def test_build_shards():
    shards = upload.build_shards(_payload())
    assert set(shards) == {"000021", "600519", "__views__"}
    # 每票带自己的记录 + 其按票视图
    assert shards["000021"]["records"] == {"000021": {"meta": {"code": "000021"}}}
    assert shards["000021"]["code_views"] == {"chart": {"000021": {"close": [1.0]}}}
    assert shards["600519"]["code_views"] == {}          # 该票无 chart
    assert shards["__views__"]["views"] == {"panel": {"rows": []}}


def test_sign_and_post_retries_on_5xx():
    seq = [(500, {"e": 1}), (503, {"e": 2}), (200, {"ok": True})]
    calls, sleeps = [], []

    def post(url, token, env):
        calls.append(env["meta"]["nonce"])
        return seq[len(calls) - 1]

    ok, status, _ = upload.sign_and_post(
        {"records": {}, "views": {}, "code_views": {}},
        {"date": "2026-08-07", "source": "s", "key_id": "k1", "sig_alg": "HMAC-SHA256"},
        "K", "http://x", "t", post, retries=5, base_delay=1.0, sleep_fn=sleeps.append)
    assert ok is True and status == 200
    assert len(calls) == 3                                # 试到第 3 次成功
    assert sleeps == [1.0, 2.0]                           # 指数退避
    assert len(set(calls)) == 3                           # 每次尝试新 nonce(防重放)


def test_sign_and_post_no_retry_on_4xx():
    calls, sleeps = [], []

    def post(url, token, env):
        calls.append(1)
        return 403, {"error": "sig"}

    ok, status, _ = upload.sign_and_post(
        {"records": {}, "views": {}, "code_views": {}},
        {"date": "2026-08-07", "source": "s", "key_id": "k1", "sig_alg": "HMAC-SHA256"},
        "K", "http://x", "t", post, retries=5, base_delay=0, sleep_fn=sleeps.append)
    assert ok is False and status == 403
    assert len(calls) == 1 and sleeps == []              # 4xx 永久失败,不重试


def test_partial_resend_via_receipt(tmp_path):
    analysis = tmp_path / "analysis"
    _seed_analysis(analysis, "2026-08-07")
    rp = tmp_path / "receipt.json"
    fail = {"000021"}                                    # 首轮让这票失败
    calls = []

    def post(url, token, env):
        code = next(iter(env["records"]), "__views__")
        calls.append(code)
        return (500, {"e": "boom"}) if code in fail else (200, {"ok": True})

    r1 = upload.upload_date("2026-08-07", url="http://x", token="t", source="s",
                            key_id="k1", key="K", analysis_dir=analysis, receipt_path=rp,
                            post_fn=post, retries=1, base_delay=0, sleep_fn=lambda s: None)
    assert r1["summary"]["failed"] == 1                  # 000021 失败
    assert rp.exists()

    fail.clear()                                         # 修好了
    calls.clear()
    r2 = upload.upload_date("2026-08-07", url="http://x", token="t", source="s",
                            key_id="k1", key="K", analysis_dir=analysis, receipt_path=rp,
                            post_fn=post, retries=1, base_delay=0, sleep_fn=lambda s: None)
    assert set(calls) == {"000021"}                      # 只补失败项,不重发已成功的
    assert r2["summary"]["failed"] == 0
