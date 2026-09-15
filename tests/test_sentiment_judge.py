"""P-B 情绪判官语义锁(mock LLM,不真调网关)。锁防偷看/只A-B不打分/无文本弃权。"""
from __future__ import annotations

import json
import os

from tools.analysis.industry_temp import sentiment_judge as SJ


class _MockClient:
    def __init__(self, ret):
        self.ret = ret
        self.calls = 0

    def extract(self, text, schema, *, instruction, temperature=0.0):
        self.calls += 1
        self._last_text = text
        return self.ret


def test_to_ab_mapping():
    assert SJ.to_ab("乐观") == "A" and SJ.to_ab("正面") == "A"
    assert SJ.to_ab("悲观") == "B" and SJ.to_ab("停滞") == "B"
    assert SJ.to_ab("中性") is None and SJ.to_ab(None) is None


def test_aggregate_majority_and_tie():
    assert SJ._aggregate({"a": "A", "b": "A", "c": "B", "d": None}) == "A"
    assert SJ._aggregate({"a": "A", "b": "B"}) is None          # 平局
    assert SJ._aggregate({"a": None, "b": None}) is None        # 全弃权


def test_no_text_abstains_without_calling_llm():
    """无文本→弃权,且**绝不调用 LLM**(不臆造)。"""
    client = _MockClient({"投资者情绪": "乐观"})
    r = SJ.judge("银行", "2026-09-11", client=client, text=[])
    assert r["abstain"] is True and r["综合"] is None
    assert all(v is None for v in r["维度"].values())
    assert client.calls == 0


def test_judge_maps_labels_to_ab():
    client = _MockClient({"投资者情绪": "乐观", "经济展望": "正面",
                          "流行风格": "审慎", "景气": "中性", "依据": "测试"})
    r = SJ.judge("银行", "2026-09-11", client=client, text=["【2026-09-10】银行让利。"])
    assert client.calls == 1
    assert r["维度"] == {"投资者情绪": "A", "经济展望": "A", "流行风格": "B", "景气": None}
    assert r["综合"] == "A"          # 2A 1B → A
    assert r["abstain"] is False


def test_prompt_only_ab_no_scores():
    """只输出 A/B、不打分(防止悄悄回到打分):schema 无强度/分值字段,instruction 明示不打分。"""
    vals = " ".join(str(v) for v in SJ.SENTIMENT_SCHEMA.values())
    assert "强度" not in vals and "分" not in vals.replace("一句话", "")
    assert "不要打分" in SJ.SENTIMENT_INSTRUCTION or "不打分" in SJ.SENTIMENT_INSTRUCTION
    assert "评估日之后" in SJ.SENTIMENT_INSTRUCTION      # 防偷看明示


def test_gather_pit_only_le_date(tmp_path):
    """采集只取 ≤date 的落盘日 + 条目日期 ≤date;未来文本绝不进。"""
    root = tmp_path / "data"
    for day, items in [
        ("2026-09-10", [{"date": "2026-09-10", "title": "银行利好", "summary": "s1", "industries": ["银行"]}]),
        ("2026-09-11", [{"date": "2026-09-11", "title": "银行政策", "summary": "s2", "industries": ["银行"]}]),
        ("2026-09-12", [{"date": "2026-09-12", "title": "未来银行", "summary": "s3", "industries": ["银行"]}]),
    ]:
        d = root / "raw" / day / "policy"
        d.mkdir(parents=True)
        json.dump(items, open(d / f"policy_{day}.json", "w", encoding="utf-8"), ensure_ascii=False)
    texts = SJ.gather_pit_policy_text("银行", "2026-09-11", window_days=30, data_root=str(root))
    joined = " ".join(texts)
    assert "银行利好" in joined and "银行政策" in joined
    assert "未来银行" not in joined      # 09-12 > 评估日 09-11,防未来


def test_net_a():
    assert SJ._net_a({"a": "A", "b": "A", "c": "B", "d": None}) == (2 - 1) / 3
    assert SJ._net_a({"a": "A", "b": "B"}) == 0.0
    assert SJ._net_a({"a": None}) is None


def test_debias_relative_corrects_bull_skew():
    """全体偏A(利好skew)时,去偏后按相对全市场排序:高于中位→A、低于→B。"""
    results = [
        {"industry": "电子", "净A度": 1.0},    # 全A(最热)
        {"industry": "医药", "净A度": 0.5},    # 中位
        {"industry": "银行", "净A度": 0.0},    # 相对最冷(虽仍非负)
    ]
    baseline = SJ._debias(results)
    assert baseline == 0.5
    rel = {r["industry"]: r["情绪_相对"] for r in results}
    assert rel["电子"] == "A" and rel["银行"] == "B" and rel["医药"] is None


def test_news_rollup_pit_only_le_date(tmp_path):
    """增强A:成分个股 news rollup 只取 ≤date;未来 news 不进。"""
    root = tmp_path / "data"
    mem = {"600000": "银行", "601398": "银行"}
    for day, code, item in [
        ("2026-09-10", "600000", {"title": "银行A股利好", "time": "2026-09-10 09:00:00"}),
        ("2026-09-12", "601398", {"title": "未来银行新闻", "time": "2026-09-12 09:00:00"}),
    ]:
        d = root / "raw" / day / "news"
        d.mkdir(parents=True)
        json.dump([item], open(d / f"{code}.json", "w", encoding="utf-8"), ensure_ascii=False)
    texts = SJ.gather_pit_news_text("银行", "2026-09-11", window_days=30,
                                    data_root=str(root), membership=mem)
    joined = " ".join(texts)
    assert "银行A股利好" in joined and "未来银行新闻" not in joined


def test_shadow_roundtrip_records_nonvalidated(tmp_path):
    """shadow 落盘=纯记录、标非validated;read_sentiment_shadow 回读综合。"""
    client = _MockClient({"投资者情绪": "乐观", "经济展望": "正面",
                          "流行风格": "激进", "景气": "生机", "依据": "x"})
    out = tmp_path / "shadow"
    payload = SJ.run_shadow("2026-09-11", out_dir=str(out), industries=["银行"],
                            client=client, data_root=str(tmp_path / "nodata"))
    assert payload["非validated"] is True and payload["forward_shadow"] is True
    # 无 policy 文本(nodata)→ 该行业弃权,综合 None
    back = SJ.read_sentiment_shadow("2026-09-11", str(out))
    assert "银行" in back


def test_sentiment_series_reads_netA(tmp_path):
    """pattern 消费接口:get_industry_sentiment_series 从 shadow 取某行业净A度时序。"""
    out = tmp_path / "shadow"
    out.mkdir()
    for d, na in [("2026-09-10", 1.0), ("2026-09-11", -0.5)]:
        payload = {"date": d, "results": [{"industry": "银行", "净A度": na, "情绪_相对": "A"}]}
        json.dump(payload, open(out / f"{d}.json", "w", encoding="utf-8"), ensure_ascii=False)
    ser = SJ.get_industry_sentiment_series("银行", ["2026-09-10", "2026-09-11", "2026-09-12"], str(out))
    assert ser == {"2026-09-10": 1.0, "2026-09-11": -0.5}    # 09-12 无文件→缺省
