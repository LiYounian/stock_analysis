"""#23 深采分层门控 · 边缘候选切片(tools.pipeline.edge.edge_slice)单测。

锁语义(守则6,防未来重写误删):
  · 取「入选之外」前 k 只(selected 集合内的一律排除);
  · 每策略上界 k 封顶(成本硬约束的第一道闸,全局 MAX 是第二道);
  · 保序去重、脏元素安全;k<=0 关闭(退回旧二值门控);
  · 对入选非严格前缀的排序(否决沉底/软降级)稳健——按排名扫、凡不在 selected 即边缘。
"""
from tools.config import settings
from tools.pipeline.edge import edge_slice


def test_takes_first_k_outside_selected():
    ranked = ["A", "B", "C", "D", "E", "F"]
    # A/B 入选 → 边缘从 C 起取前 3
    assert edge_slice(ranked, {"A", "B"}, k=3) == ["C", "D", "E"]


def test_selected_excluded_even_if_not_prefix():
    """入选非严格前缀(否决沉底致入选散落排名中):selected 内的一律跳过。"""
    ranked = ["A", "B", "C", "D", "E"]
    assert edge_slice(ranked, {"B", "D"}, k=2) == ["A", "C"]


def test_k_zero_or_negative_disables():
    assert edge_slice(["A", "B"], set(), k=0) == []
    assert edge_slice(["A", "B"], set(), k=-1) == []


def test_dedup_and_dirty_elements():
    ranked = ["A", "A", "", None, "B", 123, "B", "C"]
    assert edge_slice(ranked, set(), k=10) == ["A", "B", "C"]


def test_k_larger_than_pool_returns_all_available():
    assert edge_slice(["A", "B"], {"A"}, k=99) == ["B"]


def test_default_k_reads_setting(monkeypatch):
    """k=None → 读 settings.SCREENALL_EDGE_TOPK(单一真源)。"""
    monkeypatch.setattr(settings, "SCREENALL_EDGE_TOPK", 2)
    assert edge_slice(["A", "B", "C", "D"], set()) == ["A", "B"]
    monkeypatch.setattr(settings, "SCREENALL_EDGE_TOPK", 0)
    assert edge_slice(["A", "B", "C", "D"], set()) == []
