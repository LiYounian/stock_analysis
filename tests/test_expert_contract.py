"""F1 单测:ExpertVerdict 契约 + validate_verdict 公共守门。

锁语义:字段枚举/区间校验、方向与强度符号一致性、缺数据显式表达。
"""
import pytest

from tools.contracts import expert as ec
from tools.contracts.expert import ExpertVerdict, validate_verdict, is_valid_verdict


def _ok(**over) -> dict:
    base = dict(专家="技术趋势", 能力类型="方向", 方向="看多", 强度=0.5,
                置信度=0.8, 默认权重=1.0, 依据=["MACD金叉"], 数据充分度="充分", 原始={})
    base.update(over)
    return base


def test_valid_envelope_passes():
    assert validate_verdict(_ok()) == []
    assert is_valid_verdict(_ok())


def test_dataclass_roundtrip_and_validate():
    v = ExpertVerdict(专家="情绪三层", 能力类型="方向", 方向="看空", 强度=-0.3,
                      置信度=0.6, 默认权重=1.0, 依据=["政策利空"], 数据充分度="部分降级")
    v.validate()                          # 不抛
    assert validate_verdict(v.to_dict()) == []


def test_non_dict_rejected():
    assert validate_verdict(123)
    assert validate_verdict(None)


@pytest.mark.parametrize("field,bad", [
    ("能力类型", "预测"), ("方向", "偏多"),          # 偏多是旧术语,应被拒(D5 统一看多/看空)
    ("数据充分度", "满"), ("专家", ""),
])
def test_illegal_enums_rejected(field, bad):
    assert validate_verdict(_ok(**{field: bad}))


@pytest.mark.parametrize("val", [-1.01, 1.01, "0.5", True])
def test_strength_out_of_range_rejected(val):
    assert validate_verdict(_ok(方向="中性" if val in ("0.5", True) else "看多", 强度=val))


@pytest.mark.parametrize("val", [-0.01, 1.01, "1", True])
def test_confidence_out_of_range_rejected(val):
    assert validate_verdict(_ok(置信度=val))


@pytest.mark.parametrize("val", [-1.0, "1", True])
def test_weight_must_be_nonneg_number(val):
    assert validate_verdict(_ok(默认权重=val))


def test_boundary_values_ok():
    assert validate_verdict(_ok(方向="看多", 强度=1.0, 置信度=1.0)) == []
    assert validate_verdict(_ok(方向="看空", 强度=-1.0, 置信度=0.0)) == []
    assert validate_verdict(_ok(方向="中性", 强度=0.0)) == []


def test_direction_strength_sign_consistency():
    assert validate_verdict(_ok(方向="看多", 强度=-0.2))     # 看多却负 → 拒
    assert validate_verdict(_ok(方向="看空", 强度=0.2))      # 看空却正 → 拒
    assert validate_verdict(_ok(方向="中性", 强度=0.2))      # 中性非0 → 拒
    assert validate_verdict(_ok(方向="不适用", 强度=0.1))    # 不适用非0 → 拒


def test_missing_data_expressed_explicitly():
    """缺数据的合法表达:方向中性 + 强度0 + 置信度0 + 数据充分度=缺失。"""
    v = _ok(方向="中性", 强度=0.0, 置信度=0.0, 数据充分度="缺失", 依据=["数据缺失"])
    assert validate_verdict(v) == []


def test_depends_only_on_stdlib():
    """契约层不 import 分析器/web(依赖方向守卫)。"""
    import inspect
    src = inspect.getsource(ec)
    for bad in ("import web", "tools.analysis", "tools.strategy", "tools.store"):
        assert bad not in src
