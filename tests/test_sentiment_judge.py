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
