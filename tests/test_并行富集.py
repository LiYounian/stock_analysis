"""候选池富集**有界并发**单测(锁语义,防未来重写误删规则)。

为什么这么写:候选池「消息面回灌」节点逐只串行采集/跑 LLM(~30min)是「当天选股当天出」的
主因之一,改成有界线程池并发提速。并发改动风险高,必须锁死四条不变量:

  ① 并行 == 串行:同一输入,并发数=1 与 并发数=8 产出的候选评分/重排**逐值相等**
     (并发只改「怎么算」、不改「算什么」;按稳定键排序 → 与完成顺序无关)。
  ② 并发有界:实际在飞任务数 ≤ 配置并发数(尊重 LLM 网关 429,不无脑全开)。
  ③ 缓存并发安全:多线程同时写同一 LLM 缓存键 → 文件不损坏、读不到半截(原子写)。
  ④ 开关退回串行:config 并发数=1 一键回退,行为等价旧串行(kill-switch)。
  ⑤ 防未来不破:并发下仍只用注入 record(≤as_of 信息),不额外加载别的票/别的日期。
"""
import json
import threading
import time

import pytest

from tools import parallel
from tools.config.strategy import THRESHOLDS
from tools.pipeline import candidate_message as cm


# ————————————————————————————————————————————————
# 有界并发原语 parallel.pmap:顺序确定 + 有界
# ————————————————————————————————————————————————
def test_pmap_preserves_input_order():
    """结果按输入序回填,与完成顺序无关(慢的排前面也不乱序)。"""
    def _fn(i, x):
        time.sleep(0.01 * (5 - i) if i < 5 else 0)   # 前面的故意更慢
        return x * 10
    assert parallel.pmap(_fn, [0, 1, 2, 3, 4, 5], workers=4) == [0, 10, 20, 30, 40, 50]


def test_pmap_serial_fallback():
    """workers<=1 或单元素 → 串行(零线程),结果与并发一致。"""
    items = list(range(10))
    assert parallel.pmap(lambda i, x: x + 1, items, workers=1) == [x + 1 for x in items]
    assert parallel.pmap(lambda i, x: x + 1, [7], workers=8) == [8]
    assert parallel.pmap(lambda i, x: x, [], workers=8) == []


def test_并发有界_在飞数不超配置():
    """② 实际在飞任务数 ≤ 配置并发数(ThreadPool 上界即限流上界)。"""
    workers = 4
    n = 30
    lock = threading.Lock()
    state = {"cur": 0, "peak": 0}

    def _fn(i, x):
        with lock:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        time.sleep(0.01)                 # 制造重叠窗口,逼出真实峰值
        with lock:
            state["cur"] -= 1
        return x

    parallel.pmap(_fn, list(range(n)), workers=workers)
    assert state["peak"] <= workers      # 峰值在飞 ≤ 配置并发数
    assert state["peak"] > 1             # 确实并发了(不是退化成串行)


# ————————————————————————————————————————————————
# ③ LLM 缓存并发安全:多线程压同键不损坏
# ————————————————————————————————————————————————
def test_缓存并发安全_原子写(tmp_path):
    """多线程同时写同一缓存文件 → 最终文件永远是**完整合法 JSON**,读不到半截。"""
    from tools.analysis.event import _atomic_write_json

    p = tmp_path / "cachekey.json"
    payload = {"方向": "看多", "强度": 3, "摘要": "确定值" * 500}   # 稍大,放大半截写风险
    errors: list = []

    def _worker():
        for _ in range(20):
            try:
                _atomic_write_json(p, payload)
                back = json.loads(p.read_text(encoding="utf-8"))   # 并发读:原子 replace 保证读到整份
                if back != payload:
                    errors.append(("mismatch", back))
            except Exception as e:  # noqa: BLE001  半截写会在此炸(JSONDecodeError)
                errors.append(("exc", repr(e)))

    threads = [threading.Thread(target=_worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors                                   # 无损坏、无半截、无不一致
    assert json.loads(p.read_text(encoding="utf-8")) == payload
    # tmp 中间文件不残留(os.replace 消费掉)
    assert not list(tmp_path.glob("*.tmp"))


# ————————————————————————————————————————————————
# ①④⑤ rescore_pool:并行 == 串行(逐值)+ 开关退串行 + 防未来不破
# ————————————————————————————————————————————————
def _rec(code, net):
    """构造带消息面数据的 record(净情绪 net 驱动看多/看空)。"""
    return {"meta": {"code": code, "name": f"名{code}", "as_of": "2026-09-08"},
            "sentiment": {"净情绪分": net, "样本数": 20},
            "fundflow": {"今日主力净流入": 1e8 if net > 0 else -1e8,
                         "主力连续净流入天数": 3 if net > 0 else 0}}


def _pool_and_records(n=16):
    """一批候选:净情绪在 [-0.8,0.8] 间铺开(制造可区分的完整分,排序才有信息量)。"""
    pool = [f"S{i:03d}" for i in range(n)]
    recs = {c: _rec(c, round(-0.8 + 1.6 * i / (n - 1), 3)) for i, c in enumerate(pool)}
    return pool, recs


def _cfg_with(workers: int) -> dict:
    """在生产口径基础上只改并发数,便于对比 1 vs 8 的逐值一致性。"""
    return {**THRESHOLDS["消息面回灌"], "并发数": workers}


def test_并行结果与串行一致(hermetic_experts):
    """① 同输入,并发数=1 vs 并发数=8 → 候选评分/重排**逐值相等**(与完成顺序无关)。"""
    pool, recs = _pool_and_records()
    prov = {c: ["策略0合议"] for c in pool}

    serial = cm.rescore_pool(pool, prov, load_record=lambda c: recs[c], cfg=_cfg_with(1))
    parallel8 = cm.rescore_pool(pool, prov, load_record=lambda c: recs[c], cfg=_cfg_with(8))

    assert serial == parallel8                          # 整个重排列表逐值相等(含排名/分/方向/理由)
    # 排序确实有信息量(不是全同分的平凡通过):完整分严格非增
    scores = [x["完整分"] for x in parallel8]
    assert scores == sorted(scores, reverse=True) and scores[0] > scores[-1]


def test_开关退回串行等价(hermetic_experts, monkeypatch):
    """④ config 并发数=1(kill-switch)行为等价并发路径的串行分支;_concurrency 缺省/误配兜底。"""
    assert cm._concurrency({"并发数": 1}) == 1
    assert cm._concurrency({"并发数": 0}) == 1           # 误配 0 → 钳到 1(不致零线程)
    assert cm._concurrency({"并发数": -3}) == 1          # 误配负 → 钳到 1
    assert cm._concurrency({}) == 6                      # 缺省 → 6
    # 并发数=1 与显式串行参考实现逐值一致
    pool, recs = _pool_and_records(8)
    a = cm.rescore_pool(pool, {}, load_record=lambda c: recs[c], cfg=_cfg_with(1))
    b = cm.rescore_pool(pool, {}, load_record=lambda c: recs[c], cfg=_cfg_with(8))
    assert a == b


def test_防未来不破_并发只用注入record(hermetic_experts):
    """⑤ 并发下只加载候选池内的票(≤as_of 由 load_record 锚定),不额外拉别的票/别的日期。"""
    pool, recs = _pool_and_records(12)
    loaded: list[str] = []
    load_lock = threading.Lock()

    def _load(code):
        with load_lock:
            loaded.append(code)
        return recs[code]

    out = cm.rescore_pool(pool, {}, load_record=_load, cfg=_cfg_with(8))
    assert sorted(loaded) == sorted(pool)               # 恰好各票各一次,无越界加载
    assert {x["code"] for x in out} == set(pool)
    # ③(节点级)不写 record['council']:并发打分从不回写注入 record
    assert all("council" not in recs[c] for c in recs)


def test_节点级并行结果与串行一致(hermetic_experts, analysis_tmpdir, monkeypatch):
    """①(节点级)run_candidate_message_enrich 在 并发数=1 vs 8 下产出的重排 view **逐值相等**。

    桩掉 enrich/serialize(不触网/不跑真 LLM),只让阶段3(回灌打分重排,并发核心)真跑。
    """
    pool, recs = _pool_and_records(14)
    cm.store.set_active_date("2026-09-08")
    cm.store.put_view("策略0合议",
                      {"top": [{"code": c} for c in pool]}, date="2026-09-08")
    monkeypatch.setattr(cm.stock_pool, "get_codes", lambda: [])

    def _run(workers):
        monkeypatch.setitem(THRESHOLDS["消息面回灌"], "并发数", workers)
        v = cm.run_candidate_message_enrich(
            "2026-09-08", top_k=10,
            enrich_fn=lambda p, a, no_llm=False: {"stub": True},
            serialize_fn=lambda p, a: None,
            load_record=lambda c: recs[c])
        return v["重排"]

    r1 = _run(1)
    r8 = _run(8)
    assert r1 == r8                                     # 节点重排结果逐值相等(并发不改选股结论)
    assert len(r1) == 10                                # top_k=10 clamp:14 只候选取各策略前 10
    assert len({x["code"] for x in r1}) == 10
