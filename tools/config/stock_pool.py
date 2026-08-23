"""股票池配置(可持久化)。

真源 = `config/stock_pool.json`;文件缺失时以下方 `_SEED` 初始化并落盘。
运行期在内存维护一份副本 `_pool`,读函数走内存;增删函数改内存并回写 JSON。
读接口 get_pool/get_codes/by_sector/get 签名保持不变,故上层(run/analysis/
collectors/report/web)无需改动。界面「票池管理」经 tools.pool_service 调用增删。

历史:来源东方财富「AI」自选分组截图(2026-08-05)去重 32 只;
2026-08-06 增补光模块板块(中际旭创/光迅科技,天孚通信由光通信改归光模块)→ 34 只。
已剔除板块指数「机器人 BK1408」(非个股)。详见 docs/股票清单.md。
"""
import json
import random
import re
import threading
from dataclasses import asdict, dataclass

from tools.config import settings

_STORE = settings.PROJECT_ROOT / "config" / "stock_pool.json"
_DEV_SAMPLE_FILE = settings.PROJECT_ROOT / "config" / "dev_sample.json"
_LOCK = threading.RLock()
_CODE_RE_A = re.compile(r"^\d{6}$")
_CODE_RE_HK = re.compile(r"^\d{5}$")


@dataclass(frozen=True)
class Stock:
    code: str          # A股6位 / 港股5位,如 "002156" / "00700"
    name: str          # 名称
    industry: str      # 细分行业
    sector: str        # 大类板块
    market: str = "A"  # 市场:"A"(沪深京) / "HK"(港股)


# 种子池(仅在 JSON 不存在时用于初始化;之后真源是 config/stock_pool.json)。
# sector 用于组合层聚合(方案2 第一层),同板块须写成完全一致的字符串。
_SEED: list[Stock] = [
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
    # —— 光模块(3) ——
    Stock("300308", "中际旭创", "光模块", "光模块"),
    Stock("300394", "天孚通信", "光模块", "光模块"),
    Stock("002281", "光迅科技", "光模块", "光模块"),
    # —— 光通信(2) ——
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

_pool: list[Stock] = []   # 运行期内存副本(真源 = _STORE JSON)


# ————————————————————————————————————————————————
# 持久化(内部)
# ————————————————————————————————————————————————
def _persist() -> None:
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    _STORE.write_text(
        json.dumps([asdict(s) for s in _pool], ensure_ascii=False, indent=2),
        encoding="utf-8")


def _load() -> None:
    """从 JSON 载入内存副本;文件缺失则以 _SEED 初始化并落盘。"""
    global _pool
    with _LOCK:
        if _STORE.exists():
            raw = json.loads(_STORE.read_text(encoding="utf-8"))
            _pool = [Stock(**d) for d in raw]
        else:
            _pool = list(_SEED)
            _persist()


def reload() -> None:
    """重新从磁盘载入(供外部改文件后同步内存)。"""
    _load()


# ————————————————————————————————————————————————
# 读接口(签名不变;上层依赖这几个)
# ————————————————————————————————————————————————
def get_pool() -> list[Stock]:
    """返回全部自选票(内存副本的拷贝)。"""
    with _LOCK:
        return list(_pool)


def get_codes() -> list[str]:
    """返回全部代码列表。"""
    return [s.code for s in get_pool()]


def get_codes_by_market(market: str = "A") -> list[str]:
    """按市场筛选代码列表。"""
    return [s.code for s in get_pool() if s.market == market]


def is_hk(code: str) -> bool:
    """判断代码是否为港股(在池中且 market=HK)。"""
    s = get(code)
    return s is not None and getattr(s, "market", "A") == "HK"


def by_sector() -> dict[str, list[Stock]]:
    """按大类板块分组,供组合层聚合用。"""
    out: dict[str, list[Stock]] = {}
    for s in get_pool():
        out.setdefault(s.sector, []).append(s)
    return out


def get(code: str) -> Stock | None:
    """按代码查单只。"""
    return next((s for s in get_pool() if s.code == code), None)


def get_dev_codes(n: int = 10) -> list[str]:
    """开发期固定子集:首次从全池随机抽 n 只并持久化到 config/dev_sample.json,之后复用。

    既是「随机选出」(首次 random.sample),又「固定复用」(落盘后每次读同一批)→ 开发可复现。
    想重选:删掉 config/dev_sample.json 再调用。
    """
    pool_codes = get_codes()
    if _DEV_SAMPLE_FILE.exists():
        try:
            saved = json.loads(_DEV_SAMPLE_FILE.read_text(encoding="utf-8"))
            codes = [c for c in saved.get("codes", []) if get(c)]
            if len(codes) >= min(n, len(pool_codes)):
                return codes[:n]
        except Exception:
            pass
    picked = sorted(random.sample(pool_codes, min(n, len(pool_codes))))
    _DEV_SAMPLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DEV_SAMPLE_FILE.write_text(
        json.dumps({"n": n, "codes": picked}, ensure_ascii=False, indent=2), encoding="utf-8")
    return picked


# ————————————————————————————————————————————————
# 增删接口(改内存 + 回写 JSON;供票池管理编排调用)
# ————————————————————————————————————————————————
def add_stock(code: str, name: str, industry: str, sector: str,
              market: str = "A") -> Stock:
    """新增一只票并持久化。校验:代码格式(A股6位/港股5位)、名称/板块非空、代码不重复。

    返回新增的 Stock。校验失败或代码已存在抛 ValueError。
    """
    code = (code or "").strip()
    name = (name or "").strip()
    industry = (industry or "").strip()
    sector = (sector or "").strip()
    market = (market or "A").strip().upper()
    if market not in ("A", "HK"):
        raise ValueError(f"market 须为 'A' 或 'HK':{market!r}")
    if market == "HK":
        if not _CODE_RE_HK.match(code):
            raise ValueError(f"港股代码须为 5 位数字:{code!r}")
    else:
        if not _CODE_RE_A.match(code):
            raise ValueError(f"A股代码须为 6 位数字:{code!r}")
    if not name:
        raise ValueError("名称不能为空")
    if not sector:
        raise ValueError("大类板块(sector)不能为空")
    with _LOCK:
        if any(s.code == code and s.market == market for s in _pool):
            raise ValueError(f"代码已在票池中:{code}({market})")
        s = Stock(code, name, industry, sector, market)
        _pool.append(s)
        _persist()
    return s


def remove_stock(code: str, market: str | None = None) -> Stock:
    """从票池移除一只并持久化。返回被移除的 Stock;不存在抛 ValueError。

    market 不指定时按 code 匹配第一个(向后兼容);指定时精确匹配。
    """
    code = (code or "").strip()
    with _LOCK:
        if market:
            idx = next((i for i, s in enumerate(_pool)
                        if s.code == code and s.market == market.upper()), None)
        else:
            idx = next((i for i, s in enumerate(_pool) if s.code == code), None)
        if idx is None:
            raise ValueError(f"票池中无此代码:{code}")
        s = _pool.pop(idx)
        _persist()
    return s


_load()   # 导入即载入(缺文件则从种子初始化并落盘)
