"""股票池配置(数据即代码)。

来源:东方财富「AI」自选分组截图(2026-08-05),去重后 32 只。
详见 docs/股票清单.md。已剔除板块指数「机器人 BK1408」(非个股)。
"""
import json
import random
from dataclasses import dataclass
from pathlib import Path

_DEV_SAMPLE_FILE = Path(__file__).resolve().parents[2] / "config" / "dev_sample.json"


@dataclass(frozen=True)
class Stock:
    code: str          # 6 位代码,如 "002156"
    name: str          # 名称
    industry: str      # 细分行业
    sector: str        # 大类板块


# 32 只自选池。sector 用于组合层聚合(方案2 第一层)。
_POOL: list[Stock] = [
    # —— 半导体(14) ——
    Stock("002156", "通富微电", "半导体封测", "半导体"),
    Stock("688110", "东芯股份", "存储芯片设计", "半导体"),
    Stock("000021", "深科技", "存储封测/电子制造", "半导体"),
    Stock("600667", "太极实业", "半导体封测/工程", "半导体"),
    Stock("002409", "雅克科技", "半导体材料(电子特气/前驱体)", "半导体"),
    Stock("301308", "江波龙", "存储模组", "半导体"),
    Stock("688249", "晶合集成", "晶圆代工", "半导体"),
    Stock("688008", "澜起科技", "内存接口芯片设计", "半导体"),
    Stock("688521", "芯原股份", "芯片设计IP", "半导体"),
    Stock("603893", "瑞芯微", "SoC芯片设计", "半导体"),
    Stock("603290", "斯达半导", "功率半导体(IGBT)", "半导体"),
    Stock("688187", "时代电气", "功率半导体/轨交装备", "半导体"),
    Stock("603501", "豪威集团", "CIS图像传感器设计(原韦尔股份)", "半导体"),
    Stock("600584", "长电科技", "半导体封测", "半导体"),
    # —— 电子元件(2) ——
    Stock("000636", "风华高科", "被动元件(MLCC)", "电子元件"),
    Stock("002463", "沪电股份", "PCB", "电子元件"),
    # —— 机器人/自动化(6) ——
    Stock("002747", "埃斯顿", "工业机器人", "机器人/自动化"),
    Stock("688017", "绿的谐波", "谐波减速器", "机器人/自动化"),
    Stock("300124", "汇川技术", "工业自动化/伺服", "机器人/自动化"),
    Stock("002851", "麦格米特", "电力电子/自动化", "机器人/自动化"),
    Stock("300024", "机器人", "工业机器人(新松)", "机器人/自动化"),
    Stock("603662", "柯力传感", "传感器(称重/机器人触觉概念)", "机器人/自动化"),
    # —— 光通信(3) ——
    Stock("300394", "天孚通信", "光器件", "光通信"),
    Stock("600498", "烽火通信", "通信设备", "光通信"),
    Stock("600487", "亨通光电", "光纤光缆/通信", "光通信"),
    # —— AI算力(2) ——
    Stock("603019", "中科曙光", "服务器/算力", "AI算力"),
    Stock("002837", "英维克", "数据中心温控/液冷", "AI算力"),
    # —— 消费电子(1) ——
    Stock("002241", "歌尔股份", "消费电子(声学/VR)", "消费电子"),
    # —— 新能源材料(2) ——
    Stock("300073", "当升科技", "锂电正极材料", "新能源材料"),
    Stock("300748", "金力永磁", "稀土永磁磁材", "新能源材料"),
    # —— 公用事业(2) ——
    Stock("000539", "粤电力A", "火电/电力", "公用事业"),
    Stock("000601", "韶能股份", "水电/电力", "公用事业"),
]


def get_pool() -> list[Stock]:
    """返回全部 32 只自选票。"""
    return list(_POOL)


def get_codes() -> list[str]:
    """返回全部代码列表。"""
    return [s.code for s in _POOL]


def by_sector() -> dict[str, list[Stock]]:
    """按大类板块分组,供组合层聚合用。"""
    out: dict[str, list[Stock]] = {}
    for s in _POOL:
        out.setdefault(s.sector, []).append(s)
    return out


def get(code: str) -> Stock | None:
    """按代码查单只。"""
    return next((s for s in _POOL if s.code == code), None)


def get_dev_codes(n: int = 10) -> list[str]:
    """开发期固定子集:首次从全池随机抽 n 只并持久化到 config/dev_sample.json,之后复用。

    既是「随机选出」(首次 random.sample),又「固定复用」(落盘后每次读同一批)→ 开发可复现。
    想重选:删掉 config/dev_sample.json 再调用。
    """
    if _DEV_SAMPLE_FILE.exists():
        try:
            saved = json.loads(_DEV_SAMPLE_FILE.read_text(encoding="utf-8"))
            codes = [c for c in saved.get("codes", []) if get(c)]
            if len(codes) >= min(n, len(_POOL)):
                return codes[:n]
        except Exception:
            pass
    picked = sorted(random.sample(get_codes(), min(n, len(_POOL))))
    _DEV_SAMPLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DEV_SAMPLE_FILE.write_text(
        json.dumps({"n": n, "codes": picked}, ensure_ascii=False, indent=2), encoding="utf-8")
    return picked
