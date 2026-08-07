"""全局参数配置。

集中管理路径、采集参数、开关。分析层/采集层统一从这里取,不散落硬编码。
"""
import os
from pathlib import Path

# —— 路径 ——
PROJECT_ROOT = Path(__file__).resolve().parents[2]   # 股票分析/
DATA_RAW = PROJECT_ROOT / "data" / "raw"              # 采集缓存根目录
REPORT_DIR = PROJECT_ROOT / "data" / "reports"        # 报告/产物产出目录(不入库)

# —— 行情采集参数 ——
KLINE_PERIOD = "daily"      # daily / weekly
KLINE_ADJUST = "qfq"        # 前复权
KLINE_DAYS = 250            # 默认拉取天数(约一年交易日)

# —— 舆情/新闻采集参数 ——
NEWS_LOOKBACK_DAYS = 7      # 新闻/公告回看窗口
UGC_LIMIT = 50             # 每票 UGC 抓取条数上限

# —— 情感打分(qwen 外包)——
USE_QWEN_SENTIMENT = True   # 情感打分是否走 qwen 外包
QWEN_BATCH_SIZE = 20        # 每批送 qwen 的文本条数

# —— LLM API(全部走环境变量,禁止硬编 url/key,禁止入库)——
# 只认通用 LLM_* 变量;实际 url/key 值只在本机 shell 环境里(~/.zshrc),代码只引用变量名。
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-pro")   # 可用 LLM_MODEL 覆盖
LLM_TIMEOUT = 60
LLM_MAX_RETRY = 3
# 触点 → provider 路由:重批量走 qwen,精确抽取/摘要走用户 API
LLM_ROUTE = {"extract": "openai_compat", "sentiment": "qwen", "summary": "openai_compat"}
LLM_CACHE = DATA_RAW / "llm_cache"             # 抽取结果缓存,改下游免重复调用

# —— 采集限频(防封)——
FETCH_SLEEP_SEC = 0.5       # 单次请求间隔

# —— 数据新鲜度(store 层用)——
RAW_STALE_DAYS = 3          # raw 缓存超过此天数(或无采集元数据)视为陈旧,促使重采

# —— 存储后端(store 层分析侧读写走哪个后端)——
# file(默认):现文件后端,产物落 data/analysis/<日期>/。
# db:SQLAlchemy 后端,记录/视图入库(SQLite 起步,可换 Postgres/MySQL,由 DB_URL 决定)。
# raw 采集缓存(kline/fundamental…)恒走文件,不入库。
STORE_BACKEND = os.getenv("STORE_BACKEND", "file")
# 数据库连接串(SQLAlchemy URL)。默认单文件 SQLite,零运维;
# 换库只改此环境变量,如 mysql+pymysql://user:pwd@host:3306/db、postgresql+psycopg://...
DB_URL = os.getenv("DB_URL", f"sqlite:///{PROJECT_ROOT / 'data' / 'app.db'}")

# —— 定时调度(进程内 APScheduler,tools.scheduler 用)——
# 开关 + 各任务刷新间隔(分钟)。间隔 <=0 表示禁用该任务。全部可用环境变量覆盖。
SCHED_ENABLED = os.getenv("SCHED_ENABLED", "false").lower() in ("1", "true", "yes")
SCHED_FULL_INTERVAL_MIN = int(os.getenv("SCHED_FULL_INTERVAL_MIN", "1440"))   # T1 盘后全量:默认每日
SCHED_BACKFILL_INTERVAL_MIN = int(os.getenv("SCHED_BACKFILL_INTERVAL_MIN", "0"))  # T5 兜底补数:默认关
SCHED_FULL_ALL = os.getenv("SCHED_FULL_ALL", "true").lower() in ("1", "true", "yes")  # 全池 vs 开发子集
SCHED_MISFIRE_GRACE_SEC = int(os.getenv("SCHED_MISFIRE_GRACE_SEC", "3600"))   # 错过触发的宽限(补跑)


def ensure_dirs() -> None:
    """确保缓存/报告目录存在。"""
    for d in (DATA_RAW, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
