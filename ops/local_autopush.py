"""本地采集机闭环:盘后跑全池采集+分析 → 把当日 analysis 产物签名上传到远端 ingest。

闭合"本地算 → 远端看"这条环:数据传输从「git 携带」改为「B 期签名上传」(代码仍走 git)。
  ① 跑全池流水线(复用 `python -m tools.run all --all`,产物落 data/analysis/<今天>/)
  ② 调 tools.sync.upload 把当日产物打包+钢印签名+分片上传到远端 ingest(失败退避重试/断点续传)
只出站、不开端口;远端只接收存储、不跑采集/LLM。

设计成可注入(runner / upload_fn),流水线与上传都不真跑即可单测。真跑由 launchd 盘后触发
(见 ops/launchd/ + docs/参考/远端自动更新与自愈.md)。不重写 tools.sync.*,只编排调用。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from tools.config import settings
from tools.sync import upload
from ops.remote_update import subprocess_runner

logger = logging.getLogger("ops.local_autopush")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _receipt_dir() -> Path:
    return settings.PROJECT_ROOT / "data" / "sync_receipts"


def build_pipeline_cmd(python: str, all_pool: bool = True) -> list[str]:
    """全池流水线命令(复用现有 CLI 入口,不重写)。all_pool=True → 加 --all 跑全池 32 只。"""
    cmd = [python, "-m", "tools.run", "all"]
    if all_pool:
        cmd.append("--all")
    return cmd


def run_local_push(date: str, *, python: str, url: str, token: str, source: str,
                   key_id: str, key: str, analysis_dir: Path | None = None,
                   receipt_path: Path | None = None, run_pipeline: bool = True,
                   all_pool: bool = True, runner=subprocess_runner, upload_fn=upload.upload_date,
                   retries: int = 5) -> dict:
    """跑流水线(可跳过)→ 上传当日产物。返回 {ok, step, receipt?}。流水线失败则不上传。"""
    if run_pipeline:
        cmd = build_pipeline_cmd(python, all_pool)
        logger.info("跑全池流水线:%s", " ".join(cmd))
        rc, out = runner(cmd)
        if rc != 0:
            logger.error("流水线失败(rc=%s),中止上传:%s", rc, out[-500:])
            return {"ok": False, "step": "pipeline", "rc": rc}

    logger.info("上传当日产物到远端 ingest:date=%s", date)
    receipt = upload_fn(date, url=url, token=token, source=source, key_id=key_id, key=key,
                        analysis_dir=analysis_dir, receipt_path=receipt_path, retries=retries)
    failed = receipt.get("summary", {}).get("failed", 1)
    ok = failed == 0
    logger.info("上传完成:%s", receipt.get("summary"))
    return {"ok": ok, "step": "done" if ok else "upload", "receipt": receipt}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地采集机盘后闭环:跑全池流水线 → 签名上传到远端 ingest")
    ap.add_argument("--date", default=_today(), help="上传日期 YYYY-MM-DD;缺省今天")
    ap.add_argument("--no-pipeline", action="store_true", help="跳过流水线,只上传已有产物")
    ap.add_argument("--dev-pool", action="store_true", help="流水线只跑 10 只开发子集(默认全池 --all)")
    ap.add_argument("--retries", type=int, default=5, help="每分片最大重试次数(指数退避)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    missing = [n for n in ("SYNC_INGEST_URL", "SYNC_INGEST_TOKEN", "SYNC_SIGNING_KEY")
               if not getattr(settings, n)]
    if missing:
        print(f"缺少必需环境变量:{', '.join(missing)}(见 settings.py 同步配置块 / launchd env 文件)")
        return 2

    # 流水线用"当前正在运行本脚本的解释器"(conda 或 venv 都自动对),避免硬编 .venv
    python = sys.executable
    res = run_local_push(
        args.date, python=python, url=settings.SYNC_INGEST_URL, token=settings.SYNC_INGEST_TOKEN,
        source=settings.SYNC_SOURCE_ID, key_id=settings.SYNC_KEY_ID, key=settings.SYNC_SIGNING_KEY,
        receipt_path=_receipt_dir() / f"{args.date}.json",
        run_pipeline=not args.no_pipeline, all_pool=not args.dev_pool, retries=args.retries)

    if res["ok"]:
        print(f"闭环完成 {args.date}:{res['receipt']['summary']}")
        return 0
    print(f"闭环未完成(step={res['step']}):{res.get('receipt', {}).get('summary', res)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
