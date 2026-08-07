"""展示端数据同步(sync):本地端产物 → 展示端 DB。

当前提供 import_to_db(A 期:git 携带文件产物,展示端一次性导入 DB 只读展示)。
后续 B 期在此加 ingest(收录接口)/ sign(钢印签名/验签)/ upload(本地端签名上传)。
职责:只读文件 + 调 tools.store 公开 API 落库;不触网、不调 LLM、不改 store。
"""
