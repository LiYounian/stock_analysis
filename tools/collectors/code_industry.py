"""全A「代码→行业(申万一级)」离线映射表落盘(选股页展示用)。

背景:全A screener 选出的票没有中心记录里的人工 sector/industry(serialize 只给自选池
成员填 industry/sector,全A 票 meta.industry=None),导致选股页各策略行「行业」列空。
本模块产出一份**全A 覆盖**的 code→行业 映射,供展示层做回退(与 code_name.json 同构、同用法)。

数据源:baostock `query_stock_industry`(自有协议,不受东财指纹墙影响,~94% 全A 覆盖,
证监会行业门类)。用 tools.analysis.industry_map.to_sw 归一到**申万一级**(31 类,展示更清爽);
无法归一(极少)则回退去掉前缀码的证监会门类名。空行业(退市/僵尸股)跳过。

口径与前视偏差(诚实标注):
  - 这是**当前快照**(baostock 返回的是现状行业),与 code_name.json 一样属"现状展示标签"。
    选股页展示的是**当日选出的票**,配当前行业标签正确;历史日期回看时用现状行业作标签属轻微
    时点近似(仅标签、不进任何回测/打分),可接受。真正去前视的历史时点行业见 industry_history。
  - 申万一级为门类级近似(证监会 83 门类→申万 31,多对一),粒度损失见 industry_map 注释。

**离线用**:一次性拉取,不进日常闭环。刷新:`python -m tools.collectors.code_industry`。
落盘:config/code_industry.json = {code: 申万一级行业名}。web 只读、不触网、模块级缓存一次。
"""
from __future__ import annotations

import json
import logging
import re

from tools.config import settings

logger = logging.getLogger("collectors.code_industry")

_OUT_PATH = settings.PROJECT_ROOT / "config" / "code_industry.json"
_CODE_NAME_PATH = settings.PROJECT_ROOT / "config" / "code_name.json"
_CSRC_PREFIX = re.compile(r"^[A-Z]\d{2}")


def _fetch_baostock_industry() -> dict[str, str]:
    """baostock 全市场「6位代码→证监会行业名(带前缀码)」;空行业跳过。"""
    import contextlib
    import io

    import baostock as bs

    buf = io.StringIO()
    raw: dict[str, str] = {}
    with contextlib.redirect_stdout(buf):     # baostock 登录/登出打印,吞掉
        lg = bs.login()
        if lg.error_code != "0":
            raise ConnectionError(f"baostock 登录失败: {lg.error_msg}")
        try:
            rs = bs.query_stock_industry()
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()       # [updateDate, code, code_name, industry, cls]
                industry = (row[3] or "").strip()
                if industry:
                    raw[row[1].split(".")[-1]] = industry   # sh.600000 → 600000
        finally:
            bs.logout()
    if not raw:
        raise ConnectionError("baostock query_stock_industry 返回空")
    return raw


def refresh() -> dict[str, str]:
    """拉 baostock 证监会行业 → 归一申万一级 → 与主档 code_name.json 交集去退市 → 落盘。

    返回最终 {code: 申万一级行业名}。无法归一的极少数保留去前缀的证监会门类名(不硬凑、不丢)。
    """
    from tools.analysis.industry_map import to_sw

    raw = _fetch_baostock_industry()

    # 与主档 code_name.json 交集去退市(与 small_cap_universe 同口径);缺失则不过滤
    try:
        code_name = json.loads(_CODE_NAME_PATH.read_text("utf-8"))
        valid = set(code_name.keys())
    except FileNotFoundError:
        logger.warning("code_name.json 缺失,不做退市过滤(全量落盘)")
        valid = None

    out: dict[str, str] = {}
    unmapped = 0
    for code, csrc in raw.items():
        if valid is not None and code not in valid:
            continue
        sw = to_sw(csrc)
        if not sw:                                    # 极少数:回退去前缀的证监会门类名
            sw = _CSRC_PREFIX.sub("", csrc).strip() or None
            unmapped += 1
        if sw:
            out[code] = sw

    out = dict(sorted(out.items()))
    _OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=0), "utf-8")
    cov = f"{len(out)}/{len(valid)}" if valid is not None else str(len(out))
    logger.info("code→行业映射落盘 %s(%s 只覆盖;%d 只未归一到申万一级,已回退证监会门类)",
                _OUT_PATH, cov, unmapped)
    return out


def load() -> dict[str, str]:
    """读 code→行业 映射;缺失抛 FileNotFoundError(不静默返空)。"""
    return json.loads(_OUT_PATH.read_text("utf-8"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    refresh()
