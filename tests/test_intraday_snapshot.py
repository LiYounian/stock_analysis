"""盘中定时快照节点单测(pipeline.intraday_snapshot)。不触网(gtimg 抓取全 monkeypatch)。

锁语义(为什么这么写,防以后被无意改掉):
  · **准点性可核验**:captured_at 写真实抓取时刻,drift_seconds = 真实时刻 − slot 名义时刻。
    这是整件事的初衷——定时会话被挂起数小时时,下游必须能看出这份快照是不是真的 10:30 的。
  · **幂等**:同 slot 已有文件不覆盖(退 0);只有 --force 才重写。定时+手动补跑不会互相踩。
  · **容错**:单票抓不到只进 errors,其余票照落;全部失败才非 0 且**不落文件**
    (下游"文件存在 ⇒ 可用"的不变式)。
  · **非交易日跳过**:launchd 只认周一~周五,节假日靠脚本自己判。
  · **标的解析**:能从上一交易日选股 md 里把股票代码捞出来,且不把指数代码/噪声数字当成票。
  · **schema 齐全**:字段少一个,下游(盘中核实)就少一维,必须锁住。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from tools.pipeline import intraday_snapshot as snap

_DATE = "2026-09-03"          # 周四


def _quote(price=10.0, name="测试票"):
    """一条完整 gtimg 解析结果(字段口径同 collectors.gtimg_quote.parse_line)。"""
    return {"name": name, "price": price, "prev_close": 9.5, "open": 9.6, "high": 10.2,
            "low": 9.55, "volume": 123456.0, "amount_wan": 4567.0, "pct_chg": 5.26,
            "change": 0.5, "vol_ratio": 1.8, "turnover": 3.3, "amplitude": 6.8,
            "quote_time": "20260903103000"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离环境:产出根/选股目录指向 tmp;交易日恒真;自选池固定两只;网络全 mock。"""
    monkeypatch.setattr(snap, "OUT_ROOT", tmp_path / "intraday")
    monkeypatch.setattr(snap, "PICK_DIR", tmp_path / "选股")
    (tmp_path / "选股").mkdir()
    monkeypatch.setattr(snap.cal, "is_trading_day", lambda d=None, **k: True)
    monkeypatch.setattr(snap, "prev_trading_day", lambda d: "2026-09-02")
    monkeypatch.setattr(snap.stock_pool, "get_codes_by_market", lambda market="A": ["000021", "300308"])
    monkeypatch.setattr(snap.gtimg_quote, "fetch_quotes",
                        lambda codes: {c: _quote() for c in codes})
    monkeypatch.setattr(snap.gtimg_quote, "fetch_symbols",
                        lambda symbols: {s[2:]: _quote(3000.0, "指数") for s in symbols})
    return tmp_path


# ───────────────── 标的解析 ─────────────────

def test_parse_pick_codes_picks_stocks_from_md(env):
    """选股 md 里的股票代码要能捞出来;指数代码/噪声数字不能混进来。"""
    md = env / "选股" / "2026-09-02.md"
    md.write_text(
        "# 每日选股 2026-09-02\n"
        "| **002811 郑中设计** | 买入候选 |\n"
        "| 603270 金帝股份 | 买入候选 |\n"
        "### 3. 铁科轨道 688569 ｜ 距250日高 91.08%\n"
        "沪深300 000300 收 4547.96,深成指 399001;成交 1234567 万\n"
        "无效代码 999999 不应入选\n", encoding="utf-8")
    codes = snap.parse_pick_codes(md)
    assert codes == ["002811", "603270", "688569"]      # 顺序去重、只留真票
    assert "000300" not in codes and "399001" not in codes   # 指数不是标的(单列 indices)
    assert "999999" not in codes                        # 不在全A代码表 → 剔除


def test_parse_pick_codes_missing_file_returns_empty(env):
    assert snap.parse_pick_codes(env / "选股" / "不存在.md") == []


def test_resolve_targets_union_pick_pool_explicit(env):
    """标的 = 选股md ∪ 自选池 ∪ --codes,且来源明细可追溯。"""
    (env / "选股" / "2026-09-02.md").write_text("| 002811 郑中设计 |", encoding="utf-8")
    codes, src = snap.resolve_targets(_DATE, ["603270"])
    assert codes == ["002811", "000021", "300308", "603270"]
    assert src["pick_codes"] == ["002811"] and src["pool_codes_n"] == 2
    assert src["explicit_codes"] == ["603270"] and src["pick_date"] == "2026-09-02"
    assert src["pick_md"].endswith("2026-09-02.md")


def test_resolve_targets_without_pick_md_uses_pool_only(env):
    codes, src = snap.resolve_targets(_DATE)
    assert codes == ["000021", "300308"] and src["pick_md"] is None and src["pick_codes"] == []


# ───────────────── 准点性(初衷) ─────────────────

def test_slot_nominal_dt():
    assert snap.slot_nominal_dt(_DATE, "1030").strftime("%H:%M") == "10:30"
    assert snap.slot_nominal_dt(_DATE, "testrun") is None        # 非 HHMM → 名义时刻未知


def test_drift_seconds_is_real_minus_nominal():
    """drift = 真实抓取时刻 − slot 名义时刻(正=晚到)。会话被挂起 3h20m 就该看到 ~12000s。"""
    nominal = snap.slot_nominal_dt(_DATE, "1030")
    late = nominal + timedelta(hours=3, minutes=20)
    p = snap.build_payload(_DATE, "1030", late, {}, {"000001": {}}, [], {})
    assert p["drift_seconds"] == 3 * 3600 + 20 * 60
    assert p["captured_at"].startswith(f"{_DATE}T13:50")         # 写真实时刻,不是名义时刻
    early = nominal - timedelta(seconds=8)
    assert snap.build_payload(_DATE, "1030", early, {}, {}, [], {})["drift_seconds"] == -8


def test_drift_none_for_non_hhmm_slot():
    p = snap.build_payload(_DATE, "testrun", datetime.now().astimezone(), {}, {}, [], {})
    assert p["drift_seconds"] is None and p["nominal_at"] is None


# ───────────────── 幂等 / force ─────────────────

def test_idempotent_does_not_overwrite(env):
    assert snap.run("1030", date=_DATE) == 0
    out = snap.snapshot_path(_DATE, "1030")
    first = json.loads(out.read_text(encoding="utf-8"))["captured_at"]
    out.write_text(json.dumps({"captured_at": "SENTINEL"}), encoding="utf-8")
    assert snap.run("1030", date=_DATE) == 0                     # 已存在 → 跳过、退 0
    assert json.loads(out.read_text(encoding="utf-8"))["captured_at"] == "SENTINEL"
    assert first != "SENTINEL"


def test_force_overwrites(env):
    out = snap.snapshot_path(_DATE, "1030")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"captured_at": "SENTINEL"}), encoding="utf-8")
    assert snap.run("1030", date=_DATE, force=True) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["captured_at"] != "SENTINEL"


def test_multiple_slots_coexist(env):
    assert snap.run("1030", date=_DATE) == 0
    assert snap.run("1400", date=_DATE) == 0
    names = sorted(p.name for p in (snap.OUT_ROOT / _DATE).iterdir())
    assert names == ["T1030.json", "T1400.json"]


# ───────────────── 容错 ─────────────────

def test_single_code_failure_goes_to_errors_not_crash(env, monkeypatch):
    """一只票源方没数据 → 只进 errors,其余票照落、退出码仍 0。"""
    monkeypatch.setattr(snap.gtimg_quote, "fetch_quotes",
                        lambda codes: {c: _quote() for c in codes if c != "000021"})
    assert snap.run("1030", date=_DATE) == 0
    d = json.loads(snap.snapshot_path(_DATE, "1030").read_text(encoding="utf-8"))
    assert "000021" not in d["codes"] and "300308" in d["codes"]
    errs = [e for e in d["errors"] if e["code"] == "000021"]
    assert len(errs) == 1 and errs[0]["scope"] == "code" and errs[0]["reason"]
    assert d["meta"]["codes_err_n"] == 1 and d["meta"]["codes_ok_n"] == 1


def test_price_none_counts_as_failure(env, monkeypatch):
    """停牌票源方返回结构但无现价 → 算失败进 errors(不落一条价格为 None 的假数据)。"""
    monkeypatch.setattr(snap.gtimg_quote, "fetch_quotes",
                        lambda codes: {c: {**_quote(), "price": None} for c in codes})
    assert snap.run("1030", date=_DATE) == 0                     # 指数还在 → 不算全挂
    d = json.loads(snap.snapshot_path(_DATE, "1030").read_text(encoding="utf-8"))
    assert d["codes"] == {} and len(d["errors"]) == 2


def test_all_failed_exits_1_and_writes_nothing(env, monkeypatch):
    """个股与指数都挂 → 非 0 退出且不落文件(保持"文件存在 ⇒ 可用")。"""
    def boom(*a, **k):
        raise ConnectionError("network down")
    monkeypatch.setattr(snap.gtimg_quote, "fetch_quotes", boom)
    monkeypatch.setattr(snap.gtimg_quote, "fetch_symbols", boom)
    assert snap.run("1030", date=_DATE) == 1
    assert not snap.snapshot_path(_DATE, "1030").exists()


def test_index_failure_alone_still_writes(env, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("index source down")
    monkeypatch.setattr(snap.gtimg_quote, "fetch_symbols", boom)
    assert snap.run("1030", date=_DATE) == 0
    d = json.loads(snap.snapshot_path(_DATE, "1030").read_text(encoding="utf-8"))
    assert d["indices"] == {} and len([e for e in d["errors"] if e["scope"] == "index"]) == 5


# ───────────────── 非交易日 ─────────────────

def test_non_trading_day_skips(env, monkeypatch):
    """节假日 launchd 照样触发 → 脚本自己判交易日,跳过且不落文件、退 0。"""
    monkeypatch.setattr(snap.cal, "is_trading_day", lambda d=None, **k: False)
    assert snap.run("1030", date="2026-10-01") == 0
    assert not snap.snapshot_path("2026-10-01", "1030").exists()


# ───────────────── schema ─────────────────

def test_output_schema_complete(env):
    assert snap.run("1030", date=_DATE) == 0
    d = json.loads(snap.snapshot_path(_DATE, "1030").read_text(encoding="utf-8"))
    for k in ("date", "slot", "captured_at", "nominal_at", "drift_seconds",
              "codes", "indices", "errors", "meta"):
        assert k in d, f"顶层字段缺失:{k}"
    for k in ("source", "script_version", "script", "targets", "index_codes",
              "codes_ok_n", "codes_err_n", "indices_ok_n"):
        assert k in d["meta"], f"meta 字段缺失:{k}"
    row = d["codes"]["000021"]
    for k in ("code", "name", "price", "pct_chg", "vol_ratio", "turnover",
              "open", "high", "low", "volume", "amount_wan", "prev_close", "quote_time"):
        assert k in row, f"个股字段缺失:{k}"
    assert set(d["indices"]) == set(snap.INDEX_CODES)             # 五大指数齐全
    assert d["indices"]["000300"]["alias"] == "沪深300"
    assert d["meta"]["source"] == "qt.gtimg.cn"


def test_cli_main_smoke(env, monkeypatch):
    """CLI 入口能跑通(--slot/--codes/--date),日志文件写到 logs/ 下不报错。"""
    monkeypatch.setattr(snap, "LOG_PATH", env / "logs" / "intraday_snapshot.log")
    assert snap.main(["--slot", "1030", "--date", _DATE, "--codes", "603270"]) == 0
    d = json.loads(snap.snapshot_path(_DATE, "1030").read_text(encoding="utf-8"))
    assert "603270" in d["codes"]
    assert (env / "logs" / "intraday_snapshot.log").exists()
