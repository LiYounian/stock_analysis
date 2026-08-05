"""全局参数配置。

集中管理路径、采集参数、开关。分析层/采集层统一从这里取,不散落硬编码。
"""
from pathlib import Path

# —— 路径 ——
PROJECT_ROOT = Path(__file__).resolve().parents[2]   # 股票分析/
DATA_RAW = PROJECT_ROOT / "data" / "raw"              # 采集缓存根目录
REPORT_DIR = PROJECT_ROOT / "docs" / "报告"           # 报告产出目录

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

# —— 采集限频(防封)——
FETCH_SLEEP_SEC = 0.5       # 单次请求间隔


def ensure_dirs() -> None:
    """确保缓存/报告目录存在。"""
    for d in (DATA_RAW, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
