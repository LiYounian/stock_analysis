"""财报详情页头部 None 兜底 · 回归测试。

锁死语义:个股财报详情页头部的「行业/概念」字段(d.sector / d.industry)为 None 时,
页面头部**不得**渲染出字面量 "None"(此前 `{{ d.sector }}` 裸渲染,sector 缺失即漏 "None";
真实数据里不少票 sector 就是 None)。只兜底模板,不动数据层。

走真实 FastAPI 路由 + 真实模板渲染;无 data/analysis 缓存时自动跳过(不误报)。
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from web import data_access as da
from web.app import app

client = TestClient(app)

# 找一只有财报详情的票(财报榜首行),没有则跳过。
_ROWS = (da.financial_page("latest") or {}).get("rows") or []
_CODE = _ROWS[0]["code"] if _ROWS else None
skip_no_fin = pytest.mark.skipif(_CODE is None, reason="无财报数据缓存,先 run.py")


def _head_small(html: str) -> str:
    """抓头部 <small>…</small>(行业/概念 那行)。"""
    m = re.search(r"<h1>.*?<small>(.*?)</small>", html, re.S)
    return m.group(1) if m else ""


def _detail_html(monkeypatch, sector, industry) -> str:
    base = da.financial_detail(_CODE, "latest")
    assert base is not None
    d = dict(base, sector=sector, industry=industry)
    monkeypatch.setattr(da, "financial_detail", lambda code, date="latest": d)
    r = client.get(f"/financial/{_CODE}")
    assert r.status_code == 200
    return r.text


@skip_no_fin
def test_sector_none_头部不出现字面None(monkeypatch):
    head = _head_small(_detail_html(monkeypatch, None, None))
    assert "None" not in head, f"sector/industry 均 None 时头部漏了字面 None:{head!r}"
    assert "财报详细分析" in head


@skip_no_fin
def test_sector_none_industry_有值只显示industry(monkeypatch):
    head = _head_small(_detail_html(monkeypatch, None, "社会服务"))
    assert "None" not in head
    assert "社会服务" in head


@skip_no_fin
def test_两者齐全显示两段(monkeypatch):
    head = _head_small(_detail_html(monkeypatch, "金融", "银行"))
    assert "None" not in head
    assert "金融" in head and "银行" in head
