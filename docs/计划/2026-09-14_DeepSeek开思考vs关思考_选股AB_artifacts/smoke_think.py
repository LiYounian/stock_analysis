"""smoke:开思考臂对 1~2 只跑通,确认 extract 吐出合规 schema JSON。"""
from __future__ import annotations
import json, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from thinking_client import ThinkingClient
from tools.analysis import deep_analysis as da

DR = "/Users/yqg/Documents/projects/stock_analysis/data/analysis"
EXP = "/Users/yqg/Documents/projects/stock_analysis/docs/每日分析/经验沉淀"
DATE = "2026-09-02"
CODES = ["301246", "000157"]

cli = ThinkingClient(os.environ["QWEN_BASE_URL"], os.environ["QWEN_API_KEY"])
t0 = time.time()
res = da.generate(DATE, CODES, client=cli, data_root=Path(DR), experience_base=Path(EXP))
for r in res:
    tag = "ERR" if r.error else "OK"
    print(f"[{tag}] {r.code} stance={r.unit.get('stance')} dir1={r.unit.get('dir_1d')}"
          f" conf1={r.unit.get('dir_1d_conf')} err={r.error} coerc={len(r.coercions)}")
print("STATS:", json.dumps(cli.stats, ensure_ascii=False))
print(f"total {time.time()-t0:.0f}s")
# 落一个样例 unit 便于人工看 schema 合规性
Path(__file__).with_name("smoke_units.json").write_text(
    json.dumps(da.units_of(res), ensure_ascii=False, indent=2), encoding="utf-8")
