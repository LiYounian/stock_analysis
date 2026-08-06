"""数据存取层 store:薄仓储层,收敛数据读写(见 docs/信息流转与层职责.md §2.2)。

对外接口(文件后端;未来可换 DB,上层不动):
  get_raw / put_raw      —— raw 原始数据(kline/fundflow 走 parquet,余走 json)
  get_record / put_record —— 中心记录 data/analysis/{code}.json
  iter_records            —— 遍历个股中心记录(排除 panel/screen 等视图)
  get_view / put_view     —— 视图 data/analysis/{name}.json(panel/screen 等)
"""
from tools.store.repo import (
    get_raw,
    get_record,
    get_view,
    iter_records,
    put_raw,
    put_record,
    put_view,
)

__all__ = [
    "get_raw", "put_raw",
    "get_record", "put_record", "iter_records",
    "get_view", "put_view",
]
