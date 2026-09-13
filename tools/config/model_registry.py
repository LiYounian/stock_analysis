"""模型注册表加载器(SSOT · 单一真源)。

注册表本体是 `model_registry.yaml`(同目录),描述:
  - providers:每个模型端点的 kind / model / 凭证**环境变量名** / 默认参数 / enabled
  - routes:purpose(用途)→ primary(主 provider)[+ fallback 链]

本模块只负责**加载 + 校验 + 解析路由**;不构造 client(那是 tools/llm/client.py 的活)。

凭证纪律:注册表只存**环境变量名**(base_url_env / api_key_env),URL/Key 的真实值
由 ProviderSpec.resolve_base_url()/resolve_api_key() 运行时读 os.getenv,**绝不入库**。

设计见 docs/计划/2026-09-13_选股系统架构设计_程序化与模型迁移.md §1。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

# 注册表 YAML 路径(与本模块同目录);可传入自定义路径(测试/多环境)绕过默认。
REGISTRY_PATH = Path(__file__).with_name("model_registry.yaml")

# 本波次(P1)已实现的 client kind;其余 kind 声明可入表,但路由到时构造会显式报错(不静默降级)。
SUPPORTED_KINDS = ("openai_compat",)


@dataclass(frozen=True)
class ProviderSpec:
    """单个模型端点的声明。凭证只存变量名,值 resolve_* 时读环境。"""

    id: str
    kind: str
    model: str
    base_url_env: str
    api_key_env: str
    params: dict = field(default_factory=dict)
    enabled: bool = True
    notes: str = ""

    def resolve_base_url(self) -> str:
        """从环境变量读 base_url 真实值(未配置 → 空串,由 client 构造时报清晰错误)。"""
        return os.getenv(self.base_url_env, "")

    def resolve_api_key(self) -> str:
        """从环境变量读 api_key 真实值(未配置 → 空串)。"""
        return os.getenv(self.api_key_env, "")


@dataclass(frozen=True)
class Route:
    """一个用途的路由:主 provider + fallback 链(P1 fallback 仅为设计意向数据,不执行)。"""

    purpose: str
    primary: str
    fallback: tuple[str, ...] = ()


@dataclass(frozen=True)
class Registry:
    """整张注册表(providers + routes + 兜底用途)。"""

    providers: dict[str, ProviderSpec]
    routes: dict[str, Route]
    default_purpose: str

    def route_for(self, purpose: str) -> Route:
        """purpose → Route:命中即返回;未命中回退到 default_purpose 的路由。

        与今天"get_client 忽略 purpose 恒走 DeepSeek"等价:任意未知 purpose 都落到
        default_purpose(=extract → deepseek_v4pro),不抛错、不静默换模型。
        """
        route = self.routes.get(purpose)
        if route is not None:
            return route
        route = self.routes.get(self.default_purpose)
        if route is None:                      # 注册表自洽性已在 load 时校验,理论到不了
            raise KeyError(f"路由表缺 default_purpose={self.default_purpose!r} 的条目")
        return route

    def spec_for(self, provider_id: str) -> ProviderSpec:
        """provider id → ProviderSpec(缺失即报错,不静默)。"""
        spec = self.providers.get(provider_id)
        if spec is None:
            raise KeyError(f"注册表无 provider={provider_id!r}")
        return spec

    def primary_spec_for(self, purpose: str) -> ProviderSpec:
        """purpose → 主 provider 的 ProviderSpec(get_client 分流用的一步到位入口)。"""
        return self.spec_for(self.route_for(purpose).primary)


def _parse(raw: dict, source: str) -> Registry:
    """把 YAML 原始 dict 解析成 Registry,并做自洽性校验(fail loud,不静默)。"""
    providers_raw = raw.get("providers") or {}
    routes_raw = raw.get("routes") or {}
    default_purpose = raw.get("default_purpose", "extract")

    providers: dict[str, ProviderSpec] = {}
    for pid, p in providers_raw.items():
        try:
            providers[pid] = ProviderSpec(
                id=pid,
                kind=p["kind"],
                model=p["model"],
                base_url_env=p["base_url_env"],
                api_key_env=p["api_key_env"],
                params=dict(p.get("params") or {}),
                enabled=bool(p.get("enabled", True)),
                notes=str(p.get("notes", "")),
            )
        except KeyError as e:
            raise ValueError(f"{source}: provider {pid!r} 缺必填字段 {e}") from e

    routes: dict[str, Route] = {}
    for purpose, r in routes_raw.items():
        primary = r.get("primary")
        if not primary:
            raise ValueError(f"{source}: route {purpose!r} 缺 primary")
        fallback = tuple(r.get("fallback") or ())
        # 引用完整性:primary/fallback 指向的 provider 必须已声明
        for ref in (primary, *fallback):
            if ref not in providers:
                raise ValueError(
                    f"{source}: route {purpose!r} 引用未声明的 provider {ref!r}")
        routes[purpose] = Route(purpose=purpose, primary=primary, fallback=fallback)

    if default_purpose not in routes:
        raise ValueError(
            f"{source}: default_purpose={default_purpose!r} 不在 routes 中(兜底路由缺失)")

    return Registry(providers=providers, routes=routes, default_purpose=default_purpose)


def load_registry_from(path: str | Path) -> Registry:
    """从指定路径加载并解析注册表(不缓存;测试/多环境用)。"""
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return _parse(raw, source=str(path))


@lru_cache(maxsize=1)
def load_registry() -> Registry:
    """加载默认注册表(进程内缓存一次)。要换注册表内容改 YAML 后重启进程即可。"""
    return load_registry_from(REGISTRY_PATH)


def primary_spec_for(purpose: str) -> ProviderSpec:
    """便捷入口:purpose → 主 provider 的 ProviderSpec(走默认缓存注册表)。"""
    return load_registry().primary_spec_for(purpose)
