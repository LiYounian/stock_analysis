"""午盘全A量价重筛(noon_fullA_screen)单测。

锁语义(为什么这么写,防未来重写误删规则):
  ① 防未来·只吃快照:screen() 是**纯函数**,只吃传入 quotes,不联网、不读当日/未来行——
     用合成快照喂入即可跑通,断言产出只由快照决定(网络桩不被调用)。
  ② 涨停剔除:午盘已封板的票**不进可买候选**(够不着=不算数),移入「已封板」旁列。
  ③ 排序:量价综合分越高排越前;强动量+放量票压过弱票(横截面百分位打分)。
  ④ 去重盯盘集:exclude_codes 命中的票被移出候选(与盯盘研判不重复)。
  ⑤ 流动性门:半日成交额 < 下限的票被移出打分池。
  ⑥ 突破昨高因子可选:prev_high_map 缺省时不报错;给定时突破票拿到该维分。
  ⑦ 快照落盘含真实 captured_at + 覆盖率;md 顶部有 PICKS 机读锚点。
"""
import json

import pytest

from tools.pipeline import noon_fullA_screen as nfs


def _q(price, prev_close, open_, high, low, *, vol_ratio=1.0, turnover=2.0,
       amount_wan=8000.0, name="测试", pct_chg=None):
    """构造一只合成午盘快照 quote(字段口径同 gtimg_quote.parse_line)。"""
    if pct_chg is None:
        pct_chg = (price / prev_close - 1.0) * 100.0
    return {"name": name, "price": price, "prev_close": prev_close, "open": open_,
            "high": high, "low": low, "vol_ratio": vol_ratio, "turnover": turnover,
            "amount_wan": amount_wan, "pct_chg": round(pct_chg, 3)}


def _sample_quotes():
    """合成一小池全A午盘快照:含强势票 / 弱票 / 涨停封板票 / 低流动性票。"""
    return {
        # 强势:半日 +6%、贴日内高、量比放大、换手大 → 应排前
        "600001": _q(10.6, 10.0, 10.1, 10.7, 10.05, vol_ratio=2.5, turnover=6.0, amount_wan=30000.0, name="强势A"),
        # 中等:+2%
        "600002": _q(10.2, 10.0, 10.0, 10.3, 9.95, vol_ratio=1.2, turnover=2.5, amount_wan=15000.0, name="中等B"),
        # 弱:-1.5%,破位(现价贴日内低)
        "600003": _q(9.85, 10.0, 10.0, 10.05, 9.8, vol_ratio=0.8, turnover=1.5, amount_wan=12000.0, name="弱C"),
        # 涨停封板:主板 +10%、close≈high → is_limit_hit=True,应进已封板旁列
        "600004": _q(11.0, 10.0, 10.5, 11.0, 10.4, vol_ratio=3.0, turnover=8.0, amount_wan=25000.0, name="涨停D"),
        # 低流动性:半日成交额 800 万 < 5000 万门 → 剔除
        "600005": _q(10.4, 10.0, 10.0, 10.5, 9.98, vol_ratio=2.0, turnover=5.0, amount_wan=800.0, name="小票E"),
    }


# ───────────────── ① 防未来·纯函数(不联网) ─────────────────
def test_screen_is_pure_no_network(monkeypatch):
    """screen() 只吃快照:即便把 gtimg 抓取打成会爆炸的桩,screen() 照样跑通。"""
    def _boom(*a, **k):
        raise AssertionError("screen() 不应联网抓取")
    monkeypatch.setattr(nfs.gtimg_quote, "fetch_quotes", _boom)
    res = nfs.screen(_sample_quotes(), as_of="2026-09-16", exclude_codes=set())
    assert res["候选"], "应产出候选"
    assert res["市场环境"]["样本"] == 5


# ───────────────── ② 涨停剔除 ─────────────────
def test_limit_up_excluded_from_buyable():
    res = nfs.screen(_sample_quotes(), as_of="2026-09-16", exclude_codes=set())
    cand_codes = {r["code"] for r in res["候选"]}
    sealed_codes = {r["code"] for r in res["已封板"]}
    assert "600004" not in cand_codes, "涨停封板票不进可买候选"
    assert "600004" in sealed_codes, "涨停封板票进已封板旁列"


# ───────────────── ③ 排序:强势票压过弱票 ─────────────────
def test_ranking_strong_over_weak():
    res = nfs.screen(_sample_quotes(), as_of="2026-09-16", exclude_codes=set())
    order = [r["code"] for r in res["候选"]]
    assert order.index("600001") < order.index("600002") < order.index("600003"), \
        "强势A 应排在中等B 前、中等B 排在弱C 前"
    assert res["候选"][0]["量价综合分"] >= res["候选"][-1]["量价综合分"]


# ───────────────── ④ 去重盯盘集 ─────────────────
def test_dedup_watch_set():
    res = nfs.screen(_sample_quotes(), as_of="2026-09-16", exclude_codes={"600002"})
    cand_codes = {r["code"] for r in res["候选"]}
    assert "600002" not in cand_codes, "盯盘集里的票被去重移出候选"
    assert res["过滤"]["盯盘集去重"] == 1


# ───────────────── ⑤ 流动性门 ─────────────────
def test_liquidity_gate():
    res = nfs.screen(_sample_quotes(), as_of="2026-09-16", exclude_codes=set())
    cand_codes = {r["code"] for r in res["候选"]}
    assert "600005" not in cand_codes, "低流动性票(800万<5000万门)被剔除"
    assert res["过滤"]["流动性门剔除"] == 1


# ───────────────── ⑥ 突破昨高因子(可选) ─────────────────
def test_prev_high_optional():
    """不给 prev_high_map 不报错;给了突破票拿到突破昨高=1。"""
    q = _sample_quotes()
    res0 = nfs.screen(q, as_of="2026-09-16", exclude_codes=set())
    assert all(r["factors"]["突破昨高"] is None for r in res0["候选"])
    # 600001 今日 high=10.7 > 昨高 10.2 且现价 10.6>10.2 → 突破=1;600002 昨高 10.5 未破
    res1 = nfs.screen(q, as_of="2026-09-16", exclude_codes=set(),
                      prev_high_map={"600001": 10.2, "600002": 10.5})
    fac = {r["code"]: r["factors"]["突破昨高"] for r in res1["候选"]}
    assert fac["600001"] == 1.0
    assert fac["600002"] == 0.0


# ───────────────── ⑦ compute_factors 缺失稳健 ─────────────────
def test_compute_factors_missing():
    assert nfs.compute_factors({"price": None, "prev_close": 10.0}) is None
    assert nfs.compute_factors({"price": 10.0, "prev_close": 0}) is None
    f = nfs.compute_factors(_q(10.6, 10.0, 10.1, 10.7, 10.05))
    assert f["半日涨幅"] == pytest.approx(6.0, abs=0.01)
    assert 0 < f["贴日内高"] <= 1
    assert f["集合竞价高开"] == pytest.approx(1.0, abs=0.01)


# ───────────────── ⑧ _pct_rank 语义 ─────────────────
def test_pct_rank_semantics():
    r = nfs._pct_rank([1.0, 2.0, 3.0])
    assert r[0] < r[1] < r[2]
    assert nfs._pct_rank([5.0]) == [0.5]                 # 单样本无区分度
    r2 = nfs._pct_rank([1.0, None, 3.0])
    assert r2[1] is None                                 # None 保持 None(不沉底 0)


# ───────────────── ⑨ 快照落盘 + md/JSON 产出 ─────────────────
def test_capture_snapshot_and_outputs(tmp_path):
    q = _sample_quotes()
    payload, path = nfs.capture_snapshot("2026-09-16", codes=list(q), quotes=q,
                                         out_root=tmp_path)
    assert path.exists()
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk["captured_at"]                            # 真实抓取时刻
    assert disk["meta"]["sampled_n"] == 5
    assert 0 < disk["meta"]["coverage"] <= 1.0

    res = nfs.run_noon_fullA_screen("2026-09-16", quotes=q, exclude_codes=set(),
                                    out_root=tmp_path, out_dir=tmp_path)
    md = (tmp_path / f"{nfs.MD_PREFIX}_2026-09-16.md").read_text(encoding="utf-8")
    assert md.startswith("<!-- PICKS:")                   # 顶部机读锚点
    assert "午盘全A重筛" in md
    assert "非投资建议" in md
    js = json.loads((tmp_path / "2026-09-16" / nfs.SCREEN_JSON_NAME).read_text(encoding="utf-8"))
    assert js["强势候选代码"]
    assert "600004" in js["已封板代码"]


# ───────────────── ⑩ 高开回踩标 + 尾盘参考限价 ─────────────────
def test_gap_pullback_tag_and_limit_price():
    # 集合竞价 +6% 高开、贴顶(现价≈high) → 高开回踩标=True;尾盘参考限价=现价(回踩不追高)
    q = {"600009": _q(10.6, 10.0, 10.6, 10.62, 10.5, vol_ratio=2.0, turnover=5.0, amount_wan=20000.0)}
    res = nfs.screen(q, as_of="2026-09-16", exclude_codes=set())
    r = res["候选"][0]
    assert r["高开回踩标"] is True
    assert r["尾盘参考限价"] == r["factors"]["现价"]
