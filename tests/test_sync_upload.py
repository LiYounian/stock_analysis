"""本地端上传工具单测(tools.sync.upload)。

锁住:分片打包(每票+池级视图)/ 指数退避重试(网络/5xx 重试、4xx 不重试)/
断点续传(回执跳过已成功,只补失败项)。用假 post_fn + 假 sleep,不触网。
"""
import json

from tools.sync import upload


def test_default_post_scopes_ca_to_upload(monkeypatch):
    """上传用显式 verify=SYNC_INGEST_CA(仅约束上传),缺省 verify=True 走系统 CA。
    绝不用全局 REQUESTS_CA_BUNDLE——那会把采集端 HTTPS 的 CA 也换掉、导致采集全线验签失败。"""
    import requests
    captured = {}

    class _Resp:
        status_code = 200
        def json(self):
            return {"ok": True}

    def fake_post(url, json=None, verify=None, headers=None, timeout=None):
        captured["verify"] = verify
        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setenv("SYNC_INGEST_CA", "/tmp/ingest-ca.crt")
    upload._default_post("https://host/ingest", "tok", {"a": 1})
    assert captured["verify"] == "/tmp/ingest-ca.crt"      # 上传信任自签证书
    monkeypatch.delenv("SYNC_INGEST_CA", raising=False)
    upload._default_post("https://host/ingest", "tok", {"a": 1})
    assert captured["verify"] is True                       # 未配则走系统/certifi 默认 CA


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
    # 每票一片 + 每个池级视图各自独立成片(__view__:<name>)
    assert set(shards) == {"000021", "600519", "__view__:panel"}
    # 每票带自己的记录 + 其按票视图
    assert shards["000021"]["records"] == {"000021": {"meta": {"code": "000021"}}}
    assert shards["000021"]["code_views"] == {"chart": {"000021": {"close": [1.0]}}}
    assert shards["600519"]["code_views"] == {}          # 该票无 chart
    # 池级视图独立成片:片内只含自己那一个视图
    assert shards["__view__:panel"]["views"] == {"panel": {"rows": []}}
    assert shards["__view__:panel"]["records"] == {}


def test_build_shards_one_shard_per_view():
    """多个池级视图各自独立成片,单片只含一个视图(规避合并大 payload 超时)。"""
    payload = {
        "records": {},
        "views": {"panel": {"a": 1}, "screen": {"b": 2}, "sentiment_policy": {"c": 3}},
        "code_views": {},
    }
    shards = upload.build_shards(payload)
    assert set(shards) == {"__view__:panel", "__view__:screen", "__view__:sentiment_policy"}
    for name in ("panel", "screen", "sentiment_policy"):
        sp = shards[f"__view__:{name}"]
        assert set(sp["views"]) == {name}                # 每片恰好一个视图,互不裹挟
        assert sp["records"] == {} and sp["code_views"] == {}


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


def test_sign_and_post_retries_on_429():
    """回归(2026-08-24事故):429 限流须**可重试**(退避≥限流窗口),不能当永久 4xx 直接丢。
    历史:每日尾部全A view 分片撞远端 120/60s 限流被 429,旧码 status<500 当永久失败 → 面板天天"待运行"。"""
    seq = [(429, {"error": "rate"}), (429, {"error": "rate"}), (200, {"ok": True})]
    calls, sleeps = [], []

    def post(url, token, env):
        calls.append(env["meta"]["nonce"])
        return seq[len(calls) - 1]

    ok, status, _ = upload.sign_and_post(
        {"records": {}, "views": {}, "code_views": {}},
        {"date": "2026-08-24", "source": "s", "key_id": "k1", "sig_alg": "HMAC-SHA256"},
        "K", "http://x", "t", post, retries=5, base_delay=1.0, sleep_fn=sleeps.append,
        rate_window_s=60.0)
    assert ok is True and status == 200                  # 429→429→200,重试到成功
    assert len(calls) == 3
    assert sleeps and all(s >= 60.0 for s in sleeps)     # 429 退避≥限流窗口(非短指数)
    assert len(set(calls)) == 3                          # 每次新 nonce(防重放)


def test_upload_date_throttles_sent_shards(tmp_path):
    """节流:实际发送的分片之间按 min_interval 间隔发(压到远端限流以内、防尾部 429);跳过的不计间隔。"""
    analysis = tmp_path / "analysis"
    _seed_analysis(analysis, "2026-08-07")
    sleeps = []

    def post(url, token, env):
        return 200, {"ok": True}

    r = upload.upload_date("2026-08-07", url="http://x", token="t", source="s",
                           key_id="k1", key="K", analysis_dir=analysis,
                           post_fn=post, retries=0, base_delay=0,
                           sleep_fn=sleeps.append, min_interval=0.5)
    n = r["summary"]["total"]
    assert n >= 2                                        # 至少两个分片才有间隔
    assert len([s for s in sleeps if s == 0.5]) == n - 1  # 发 n 片 → 节流 n-1 次


def test_sign_and_post_sanitizes_nan_to_valid_json():
    """回归:分片含 NaN/Inf(如 panel 视图 89 个 NaN)时,签名前须清成 null。

    Python json.dump 默认把 NaN 写成非法字面量 `NaN`,严格解析的远端 ingest 拒收致整片失败。
    锁死:发到 post_fn 的 env 严格 JSON 可序列化(allow_nan=False 不抛)且无残留 NaN/Inf。
    """
    captured = {}

    def post(url, token, env):
        captured["env"] = env
        return 200, {"ok": True}

    nan, inf = float("nan"), float("inf")
    payload = {"records": {}, "code_views": {},
               "views": {"panel": [{"code": "301583", "atr_pct": nan, "bias20": inf,
                                    "5日止盈%": 12.5, "备注": "正常字段"}]}}
    ok, status, _ = upload.sign_and_post(
        payload, {"date": "2026-08-08", "source": "s", "key_id": "k1", "sig_alg": "HMAC-SHA256"},
        "K", "http://x", "t", post, retries=0, base_delay=0, sleep_fn=lambda s: None)
    assert ok is True and status == 200
    env = captured["env"]
    # 严格 JSON(不容忍 NaN/Inf)必须能序列化,否则远端拒收
    json.dumps(env, allow_nan=False)
    row = env["views"]["panel"][0]
    assert row["atr_pct"] is None and row["bias20"] is None   # NaN/Inf → null
    assert row["5日止盈%"] == 12.5 and row["备注"] == "正常字段"  # 正常值不动


def test_partial_resend_via_receipt(tmp_path):
    analysis = tmp_path / "analysis"
    _seed_analysis(analysis, "2026-08-07")
    rp = tmp_path / "receipt.json"
    fail = {"000021"}                                    # 首轮让这票失败
    calls = []

    def post(url, token, env):
        code = next(iter(env["records"]), None) or f"__view__:{next(iter(env['views']))}"
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


def test_changed_content_resends_despite_receipt(tmp_path):
    """内容变了的分片即使回执标已成功也重发(根治'同日重传不覆盖');未变的仍跳过。"""
    analysis = tmp_path / "analysis"
    _seed_analysis(analysis, "2026-08-07")
    rp = tmp_path / "receipt.json"
    calls = []

    def post(url, token, env):
        code = next(iter(env["records"]), None) or f"__view__:{next(iter(env['views']))}"
        calls.append(code)
        return (200, {"ok": True})

    kw = dict(url="http://x", token="t", source="s", key_id="k1", key="K",
              analysis_dir=analysis, receipt_path=rp, post_fn=post, retries=1,
              base_delay=0, sleep_fn=lambda s: None)
    upload.upload_date("2026-08-07", **kw)               # 首轮:全部发出并记内容 hash
    assert "__view__:panel" in calls

    calls.clear()
    (analysis / "2026-08-07" / "panel.json").write_text(  # 只改 panel 视图内容
        json.dumps({"rows": [1, 2, 3]}), encoding="utf-8")
    upload.upload_date("2026-08-07", **kw)               # 次轮:仅内容变了的 panel 重发
    assert calls == ["__view__:panel"]                   # 变更分片重发,其余按 hash 跳过


def _seed_multi_views(root, date):
    """播种:1 票 + 3 个池级视图(含一个"大"视图),验证按视图独立分片/续传。"""
    d = root / date
    d.mkdir(parents=True)
    (d / "000021.json").write_text(json.dumps({"meta": {"code": "000021"}}), encoding="utf-8")
    (d / "panel.json").write_text(json.dumps({"rows": [1, 2]}), encoding="utf-8")
    (d / "screen.json").write_text(json.dumps({"rows": []}), encoding="utf-8")
    # 模拟撑大 __views__ 的元凶:大的 sentiment_policy 视图
    (d / "sentiment_policy.json").write_text(
        json.dumps([{"code": f"{i:06d}", "text": "x" * 200} for i in range(50)]), encoding="utf-8")


def test_each_view_is_own_shard_and_independently_resumable(tmp_path):
    """池级视图按视图分片:大 sentiment_policy 独立成片,失败只补它,其它视图不受牵连。"""
    analysis = tmp_path / "analysis"
    _seed_multi_views(analysis, "2026-08-07")
    rp = tmp_path / "receipt.json"
    fail = {"__view__:sentiment_policy"}                  # 首轮只让大视图那片失败
    calls = []

    def post(url, token, env):
        key = next(iter(env["records"]), None) or f"__view__:{next(iter(env['views']))}"
        calls.append(key)
        # 单片只含一个视图 → 天然规避合并大 payload
        assert len(env["views"]) <= 1
        return (500, {"e": "boom"}) if key in fail else (200, {"ok": True})

    r1 = upload.upload_date("2026-08-07", url="http://x", token="t", source="s",
                            key_id="k1", key="K", analysis_dir=analysis, receipt_path=rp,
                            post_fn=post, retries=1, base_delay=0, sleep_fn=lambda s: None)
    # 4 分片:1 票 + panel + screen + sentiment_policy,唯独大视图那片失败
    assert r1["summary"] == {"total": 4, "ok": 3, "failed": 1}
    assert r1["shards"]["__view__:sentiment_policy"]["ok"] is False
    assert r1["shards"]["__view__:panel"]["ok"] is True

    fail.clear()                                         # 远端恢复
    calls.clear()
    r2 = upload.upload_date("2026-08-07", url="http://x", token="t", source="s",
                            key_id="k1", key="K", analysis_dir=analysis, receipt_path=rp,
                            post_fn=post, retries=1, base_delay=0, sleep_fn=lambda s: None)
    assert set(calls) == {"__view__:sentiment_policy"}   # 断点续传:只补失败的那个视图
    assert r2["summary"]["failed"] == 0


def test_upload_timeout_configurable(monkeypatch):
    """POST 超时可经 SYNC_UPLOAD_TIMEOUT_S 覆盖,缺省放宽到 120s(兜底大视图慢)。"""
    import requests
    captured = {}

    class _Resp:
        status_code = 200
        def json(self):
            return {"ok": True}

    def fake_post(url, json=None, verify=None, headers=None, timeout=None):
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.delenv("SYNC_UPLOAD_TIMEOUT_S", raising=False)
    upload._default_post("https://h/ingest", "tok", {"a": 1})
    assert captured["timeout"] == 120.0                  # 缺省放宽到 120s

    monkeypatch.setenv("SYNC_UPLOAD_TIMEOUT_S", "45")
    upload._default_post("https://h/ingest", "tok", {"a": 1})
    assert captured["timeout"] == 45.0                   # 可按环境覆盖

    monkeypatch.setenv("SYNC_UPLOAD_TIMEOUT_S", "not-a-number")
    upload._default_post("https://h/ingest", "tok", {"a": 1})
    assert captured["timeout"] == 120.0                  # 非法值回落默认


def test_upload_date_only_view_single_shard(tmp_path):
    """只补传(策略9傍晚补跑用):only_shards 只发指定 view 分片,record 与其它 view 一概不发。

    锁死"零外溢":傍晚只补「最强选股」时,绝不能把 record/panel 等分片也重传出去。
    """
    analysis = tmp_path / "analysis"
    d = analysis / "2026-08-25"
    d.mkdir(parents=True)
    (d / "000021.json").write_text(json.dumps({"meta": {"code": "000021"}}), encoding="utf-8")
    (d / "600519.json").write_text(json.dumps({"meta": {"code": "600519"}}), encoding="utf-8")
    (d / "panel.json").write_text(json.dumps({"rows": []}), encoding="utf-8")
    (d / "最强选股.json").write_text(json.dumps({"入选数": 5, "入选清单": []}), encoding="utf-8")
    posted = []

    def post(url, token, env):
        posted.append(env)
        return 200, {"ok": True}

    upload.upload_date("2026-08-25", url="http://x", token="t", source="s",
                       key_id="k1", key="K", analysis_dir=analysis,
                       post_fn=post, retries=0, base_delay=0, sleep_fn=lambda *_: None,
                       only_shards={"__view__:最强选股"})
    assert len(posted) == 1                               # 只 POST 1 次
    env = posted[0]
    assert set(env["views"]) == {"最强选股"}               # 片内只含这一个 view
    assert env["records"] == {} and env["code_views"] == {}   # record/按票视图一律不发
    assert env["views"]["最强选股"] == {"入选数": 5, "入选清单": []}
    assert env["meta"].get("sig")                         # 已签名


def test_only_view_key_matches_build_shards():
    """--only-view <name> 拼出的过滤键 == build_shards 对该 view 生成的分片键(防前缀漂移)。"""
    payload = {"records": {}, "views": {"最强选股": {"入选数": 5}}, "code_views": {}}
    shards = upload.build_shards(payload)
    assert set(shards) == {"__view__:最强选股"}
    assert f"{upload.VIEW_SHARD_PREFIX}最强选股" in shards   # CLI 拼键与 build 键一致
