"""登记等价性:SSOT 注册表(tools/scheduling/tasks.yaml) ↔ 现有 live launchd plist。

锁语义(呼应架构⑥任务书"dry-run 证明现有任务能等价登记运行" + 约法第6条测试锁语义):
本测试证明 **声明式注册表里每条任务的触发时刻,与它对应的 live launchd plist 完全一致**
——即"把 launchd 任务搬进注册表"是等价搬运、不是偷偷改点。这为将来 P3 真正收编
launchd(人工 gated 切换)留下**可复核的等价证据**:切换前跑本测试,红了说明注册表
与实盘漂移、必须先对齐。

只读比对,不装载/卸载/改任何 live launchd、不起调度、不触发真跑(建-not-切)。

比对口径:把两侧都归一成 **一周内的 (星期, 时, 分) 触发时刻集合**再逐集合相等。
- launchd 侧:直接读 plist 的 StartCalendarInterval(每个 dict 一个触发点)。
- 注册表侧:用与 runtime 同一个 APScheduler CronTrigger 引擎枚举一整周的触发时刻
  (锁的是"实际会被调度成什么",而非 cron 字符串的字面写法)。

⚠️ 只比对**触发时刻(排期)**,不比对命令体/env/slot 实参——那些是 P3 切换时另行核对的
迁移项;本测试专注"什么时候跑"的等价。
"""
from __future__ import annotations

import datetime as dt
import plistlib
import re
from pathlib import Path

import pytest
from apscheduler.triggers.cron import CronTrigger

from tools.scheduling import load_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHD_DIR = REPO_ROOT / "ops" / "launchd"

# 2024-01-01 是周一,取当周做枚举窗口(1 月无 DST,Asia/Shanghai 亦无 DST,枚举稳定)。
_WEEK_START = dt.datetime(2024, 1, 1, 0, 0, 0)
_WEEK_DAYS = 7

# 触发时刻归一表示:Python weekday(周一=0 … 周日=6, hour, minute)
Slot = tuple[int, int, int]

# 现有 plist 的中文说明注释里含 `--until` 等双连字符;`--` 在 XML 注释里非法,
# 严格的 expat(plistlib)会拒解析(plutil 宽容故日常无感)。比对只需 plist 数据体、
# 不需注释,故解析前先剥掉注释块,避免被注释里的合法业务文字绊倒。
_XML_COMMENT = re.compile(rb"<!--.*?-->", re.DOTALL)


def _load_plist(plist_path: Path) -> dict:
    raw = _XML_COMMENT.sub(b"", plist_path.read_bytes())
    return plistlib.loads(raw)


def _launchd_weekday_to_py(wd: int) -> int:
    """launchd Weekday(周日=0 或 7,周一=1 … 周六=6)→ Python weekday(周一=0 … 周日=6)。"""
    return (wd - 1) % 7


def plist_slots(plist_path: Path) -> set[Slot]:
    """读 plist 的 StartCalendarInterval → 一周触发时刻集合。

    launchd 语义:StartCalendarInterval 里省略的字段=通配。现有 stock plist 全部显式给
    Weekday+Hour+Minute(逐工作日铺开),故这里按"缺 Weekday 即全周 0-6"稳妥展开,
    对现状是精确读取、对将来偶发通配写法也不会漏。
    """
    data = _load_plist(plist_path)
    entries = data.get("StartCalendarInterval")
    if entries is None:
        return set()
    if isinstance(entries, dict):
        entries = [entries]
    slots: set[Slot] = set()
    for e in entries:
        hour = e["Hour"]
        minute = e.get("Minute", 0)
        if "Weekday" in e:
            weekdays = [_launchd_weekday_to_py(int(e["Weekday"]))]
        else:
            weekdays = list(range(7))
        for wd in weekdays:
            slots.add((wd, int(hour), int(minute)))
    return slots


def cron_slots(cron: str) -> set[Slot]:
    """用 APScheduler CronTrigger 枚举一整周的触发时刻 → 归一集合(与 runtime 同引擎)。

    锁的是"注册表实际会被调度成什么时刻",绕开 cron 字面/周字段编码坑
    (tasks.yaml 已用 mon-fri 名字规避 APScheduler 数字周 0=周一 的歧义)。
    """
    trig = CronTrigger.from_crontab(cron)
    tz = trig.timezone
    try:
        start = tz.localize(_WEEK_START)               # pytz 风格
    except AttributeError:
        start = _WEEK_START.replace(tzinfo=tz)          # zoneinfo 风格
    end = start + dt.timedelta(days=_WEEK_DAYS)
    slots: set[Slot] = set()
    prev = start - dt.timedelta(minutes=1)
    # 上限护栏:一周每分钟至多 7*24*60,循环远不到即 break;防坏表达式死循环
    for _ in range(7 * 24 * 60 + 10):
        nxt = trig.get_next_fire_time(None, prev + dt.timedelta(minutes=1))
        if nxt is None or nxt >= end:
            break
        slots.add((nxt.weekday(), nxt.hour, nxt.minute))
        prev = nxt
    return slots


def _plist_id(plist_path: Path) -> str:
    """com.stock.<id>.plist → <id>(与 tasks.yaml 的 id 对齐)。"""
    label = _load_plist(plist_path)["Label"]
    assert label.startswith("com.stock."), f"意外的 Label:{label}"
    return label[len("com.stock."):]


def _all_plists() -> dict[str, Path]:
    paths = sorted(LAUNCHD_DIR.glob("com.stock.*.plist"))
    assert paths, f"未找到任何 launchd plist:{LAUNCHD_DIR}"
    return {_plist_id(p): p for p in paths}


# ── 测试 ────────────────────────────────────────────────────────────────
def test_registry_covers_every_launchd_job():
    """每个 live launchd 业务 job 都在注册表里有对应声明(防漏登记 / 防将来新增 launchd 不入表)。"""
    reg = load_registry()
    yaml_ids = set(reg.by_id())
    plist_ids = set(_all_plists())
    missing = plist_ids - yaml_ids
    assert not missing, (
        f"这些 launchd job 未登记进 tasks.yaml(SSOT 漏项):{sorted(missing)}")


@pytest.mark.parametrize("plist_id,plist_path", sorted(_all_plists().items()))
def test_schedule_equivalence(plist_id: str, plist_path: Path):
    """注册表任务的触发时刻集合 == 对应 launchd plist 的触发时刻集合(等价登记的核心断言)。

    含 enabled=false 的收编条目(sepa/autopush):它们虽不参与影子排期,但登记的 cron
    仍须与实盘 plist 等价——否则将来启用/切换会静默改点。
    """
    reg = load_registry()
    spec = reg.by_id().get(plist_id)
    assert spec is not None, f"launchd job {plist_id} 在 tasks.yaml 中无对应任务"

    want = plist_slots(plist_path)                      # 实盘 launchd 触发时刻
    got = cron_slots(spec.cron)                         # 注册表 cron 枚举触发时刻
    assert got == want, (
        f"[{plist_id}] 排期不等价:\n"
        f"  tasks.yaml cron={spec.cron!r} → {sorted(got)}\n"
        f"  launchd plist          → {sorted(want)}\n"
        f"  仅注册表有={sorted(got - want)} 仅launchd有={sorted(want - got)}")


def test_weekday_mapping_sanity():
    """守住周字段映射本身:launchd 1..5 == 周一..周五(Python 0..4),别被编码坑悄悄带偏。"""
    assert [_launchd_weekday_to_py(w) for w in (1, 2, 3, 4, 5)] == [0, 1, 2, 3, 4]
    assert _launchd_weekday_to_py(6) == 5      # 周六
    assert _launchd_weekday_to_py(0) == 6      # 周日(launchd 0)
    assert _launchd_weekday_to_py(7) == 6      # 周日(launchd 7,与 0 同义)
