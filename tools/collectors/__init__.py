"""采集层包。

统一静音 akshare 触发的 pandas PerformanceWarning:
akshare 内部(如 stock_finance_sina)用大量 frame.insert() 逐列拼表,新版 pandas(3.0+)
对"DataFrame 碎片化"告警更严,每列刷一条。这是纯性能提示、非错误,不影响采集数据,
定时任务全池采集时尤其刷屏。只压这一类警告,其它警告/报错保持原样。
"""
import warnings

try:
    from pandas.errors import PerformanceWarning

    warnings.filterwarnings("ignore", category=PerformanceWarning)
except Exception:       # pandas 缺失/版本无此类时,不因静音逻辑影响采集
    pass
