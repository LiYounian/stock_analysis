"""数据契约:专家结论信封 ExpertVerdict —— 多策略合议的统一 API(F1)。

设计权威:docs/计划/多策略合议_专家投票架构_与新策略roadmap.md §二 + §八(D5 术语已锁"看多/看空/中性")。
定位:所有"专家"(策略的对外身份)对**单票**产出同一个信封;合议层(council)按此加权合成。
本模块是**公共守门**:任何专家产出都要过 validate_verdict。轻量校验、不引第三方(仿 record.py 风格)。

依赖方向:契约层(基座),不 import 任何分析器/web/report。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

# ————————————————————————————————————————————————
# 枚举词表(合议专用,单一真源;方向术语 D5 锁定"看多/看空/中性")
# ————————————————————————————————————————————————
能力类型_枚举 = ("方向", "评级", "信号", "入选")
方向_枚举 = ("看多", "看空", "中性", "不适用")
数据充分度_枚举 = ("充分", "部分降级", "缺失")

# 方向 → 符号(合成时 强度 已带符号,此表供适配器把方向落成符号用)
方向符号 = {"看多": 1.0, "看空": -1.0, "中性": 0.0, "不适用": 0.0}


@dataclass
class ExpertVerdict:
    """一个专家对单票的结论信封。

    字段:
      专家:       registry 里的策略名/专家名(唯一键)
      能力类型:   方向|评级|信号|入选(底层策略类别,见设计稿 §2.3)
      方向:       看多|看空|中性|不适用(统一三态 + 不适用)
      强度:       [-1,1],**已按方向带符号**(看多为正、看空为负);裸分需归一到此区间
      置信度:     [0,1],数据充分度 × 共振/样本数(D3);缺数据时显著降低
      默认权重:   ≥0,话语权基准(真源在 config['合议']['默认权重'])
      依据:       list[str],可读可追溯(沿用现有"依据"惯例)
      数据充分度: 充分|部分降级|缺失
      原始:       各专家原生输出,原样保留供深查(不参与合成)
    """
    专家: str
    能力类型: str
    方向: str
    强度: float
    置信度: float
    默认权重: float = 1.0
    依据: list = field(default_factory=list)
    数据充分度: str = "充分"
    原始: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> None:
        """校验自身,不合规抛 ValueError(供生产端强约束)。"""
        errs = validate_verdict(self.to_dict())
        if errs:
            raise ValueError("ExpertVerdict 非法: " + "; ".join(errs))


def _num_in(val, lo: float, hi: float) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool) and lo <= float(val) <= hi


def validate_verdict(v) -> list[str]:
    """校验一个专家信封(dict 或 ExpertVerdict),返回问题列表(空=合规)。

    公共守门:council / experts / 各专家产出前都应过此。严格校验(与 record.py 对 null 宽容不同——
    专家信封是主动产出物,字段必须齐全合法,缺数据也要显式给"中性 + 置信度0 + 数据充分度=缺失")。
    """
    if isinstance(v, ExpertVerdict):
        v = v.to_dict()
    if not isinstance(v, dict):
        return ["信封非 dict"]

    errs: list[str] = []
    if not isinstance(v.get("专家"), str) or not v.get("专家"):
        errs.append(f"专家 缺失或非字符串: {v.get('专家')!r}")
    if v.get("能力类型") not in 能力类型_枚举:
        errs.append(f"能力类型 非法: {v.get('能力类型')!r}(须 ∈ {能力类型_枚举})")
    if v.get("方向") not in 方向_枚举:
        errs.append(f"方向 非法: {v.get('方向')!r}(须 ∈ {方向_枚举})")
    if not _num_in(v.get("强度"), -1.0, 1.0):
        errs.append(f"强度 越界(须 ∈ [-1,1]): {v.get('强度')!r}")
    if not _num_in(v.get("置信度"), 0.0, 1.0):
        errs.append(f"置信度 越界(须 ∈ [0,1]): {v.get('置信度')!r}")
    w = v.get("默认权重")
    if not (isinstance(w, (int, float)) and not isinstance(w, bool) and float(w) >= 0):
        errs.append(f"默认权重 须为 ≥0 数值: {w!r}")
    if not isinstance(v.get("依据"), list):
        errs.append(f"依据 须为 list: {type(v.get('依据')).__name__}")
    if v.get("数据充分度") not in 数据充分度_枚举:
        errs.append(f"数据充分度 非法: {v.get('数据充分度')!r}(须 ∈ {数据充分度_枚举})")
    if not isinstance(v.get("原始"), dict):
        errs.append(f"原始 须为 dict: {type(v.get('原始')).__name__}")

    # 方向与强度符号一致性(看多→非负、看空→非正、中性/不适用→0)
    d, s = v.get("方向"), v.get("强度")
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        if d == "看多" and s < 0:
            errs.append(f"方向=看多 但 强度<0: {s}")
        elif d == "看空" and s > 0:
            errs.append(f"方向=看空 但 强度>0: {s}")
        elif d in ("中性", "不适用") and float(s) != 0.0:
            errs.append(f"方向={d} 但 强度≠0: {s}")
    return errs


def is_valid_verdict(v) -> bool:
    return not validate_verdict(v)
