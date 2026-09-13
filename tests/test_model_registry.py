"""模型注册表 + 路由骨架单测(架构①-P1)。

锁四件事(纯逻辑,不联网):
  1. 注册表可载 + 结构自洽(providers/routes 解析、引用完整性、default 兜底)。
  2. 路由表解析正确 + get_client(purpose) 按表分流。
  3. **行为零变化**回归:任意 purpose 返回的 client 的 model/timeout/关思考 与改动前(settings)一致;
     千问已注册但**不默认路由**。
  4. 凭证只引用环境变量名、不入库;真实值运行时读 env。
  5. 换模型 = 改 YAML 一行(config-driven,非硬编);注册表异常回退旧路径(降级不崩)。
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tools.config import model_registry as mr
from tools.config import settings
from tools.llm import client as lc


# ---------- 1. 注册表可载 + 结构自洽 ----------
def test_registry_loads_and_shape():
    reg = mr.load_registry()
    assert {"deepseek_v4pro", "qwen_max"} <= set(reg.providers)
    ds = reg.providers["deepseek_v4pro"]
    assert ds.kind == "openai_compat"
    assert ds.model == "deepseek-v4-pro"           # 与今天 settings.LLM_MODEL 一致
    assert ds.base_url_env == "LLM_BASE_URL" and ds.api_key_env == "LLM_API_KEY"
    assert ds.enabled is True
    # 路由覆盖现有全部触点用途
    assert {"extract", "sentiment", "summary", "relevance", "financial_verdict"} <= set(reg.routes)


def test_registry_default_model_matches_settings():
    """注册表主模型 == settings.LLM_MODEL:P1 不悄悄换掉今天在用的模型。"""
    reg = mr.load_registry()
    assert reg.primary_spec_for("extract").model == settings.LLM_MODEL


# ---------- 2. 路由表解析正确 ----------
def test_route_resolution():
    reg = mr.load_registry()
    for purpose in ("extract", "sentiment", "summary", "relevance", "financial_verdict"):
        assert reg.route_for(purpose).primary == "deepseek_v4pro"   # P1 全指 DeepSeek


def test_unknown_purpose_falls_to_default():
    """未知 purpose → 兜底到 default_purpose(与今天'忽略 purpose 恒走 DeepSeek'等价)。"""
    reg = mr.load_registry()
    assert reg.route_for("不存在的用途xyz").primary == reg.route_for(reg.default_purpose).primary
    assert reg.primary_spec_for("不存在的用途xyz").model == settings.LLM_MODEL


# ---------- 3. get_client(purpose) 按表分流 + 行为零变化回归 ----------
@pytest.fixture
def _dummy_env(monkeypatch):
    """给 DeepSeek provider 的 env 变量名喂占位值,使 client 可构造(不联网)。"""
    monkeypatch.setenv("LLM_BASE_URL", "http://dummy.local/v1")
    monkeypatch.setenv("LLM_API_KEY", "dummy-key")


@pytest.mark.parametrize("purpose",
                         ["extract", "sentiment", "summary", "relevance",
                          "financial_verdict", "不存在的用途xyz"])
def test_get_client_behavior_unchanged(purpose, _dummy_env):
    """核心回归:任意 purpose 返回的 client,model/timeout/关思考 与改动前(settings 口径)完全一致。

    这就是"行为零变化"的可执行断言——今天所有触点(不论传不传 purpose)都拿 DeepSeek,
    P1 之后仍拿到语义等价的同一个 DeepSeek client。
    """
    cli = lc.get_client(purpose)
    assert isinstance(cli, lc.OpenAICompatClient)
    assert cli.model == settings.LLM_MODEL
    assert cli._timeout == settings.LLM_TIMEOUT
    assert cli._disable_thinking == settings.LLM_DISABLE_THINKING


def test_get_client_default_purpose(_dummy_env):
    """不传 purpose(现有绝大多数调用点的形态)→ 仍是 DeepSeek。"""
    assert lc.get_client().model == settings.LLM_MODEL


# ---------- 4. 千问已注册但 P1 不默认路由 ----------
def test_qwen_registered_but_not_default_routed():
    reg = mr.load_registry()
    qwen = reg.providers["qwen_max"]
    assert qwen.enabled is True and qwen.kind == "openai_compat"    # 已注册、可切换
    # P1:没有任何 route 的 primary 指向千问(迁移在 P2)
    assert all(r.primary != "qwen_max" for r in reg.routes.values())


# ---------- 5. 凭证纪律:只引用环境变量名、值不入库 ----------
def test_credentials_are_env_names_only():
    reg = mr.load_registry()
    for spec in reg.providers.values():
        # 存的是变量名(以 _ENV 结尾的约定名),不是 URL/Key 值
        assert spec.base_url_env and not spec.base_url_env.lower().startswith("http")
        assert spec.api_key_env and " " not in spec.api_key_env


def test_yaml_file_contains_no_secret_values():
    """直接扫 YAML 文本:不得出现 URL / Bearer / sk- 形态的真实凭证。"""
    text = mr.REGISTRY_PATH.read_text(encoding="utf-8")
    low = text.lower()
    assert "http://" not in low and "https://" not in low
    assert "bearer " not in low
    assert "sk-" not in low
    # 变量名本身应在(证明确实用引用而非硬编)
    assert "base_url_env" in text and "api_key_env" in text


def test_resolve_reads_env_at_runtime(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://runtime.local/v1")
    monkeypatch.setenv("LLM_API_KEY", "runtime-key")
    ds = mr.load_registry().providers["deepseek_v4pro"]
    assert ds.resolve_base_url() == "http://runtime.local/v1"
    assert ds.resolve_api_key() == "runtime-key"


# ---------- 6. 换模型 = 改 YAML 一行(config-driven) ----------
def test_swap_model_is_one_line(tmp_path: Path):
    """改注册表的 model 一行,resolve 立即反映——证明模型由配置驱动、非代码硬编。"""
    yml = tmp_path / "reg.yaml"
    yml.write_text(textwrap.dedent("""
        version: 1
        providers:
          deepseek_next:
            kind: openai_compat
            model: deepseek-v5-ultra
            base_url_env: LLM_BASE_URL
            api_key_env: LLM_API_KEY
            params: {enable_thinking: false, timeout: 60}
            enabled: true
        routes:
          extract: {primary: deepseek_next, fallback: []}
        default_purpose: extract
    """), encoding="utf-8")
    reg = mr.load_registry_from(yml)
    assert reg.primary_spec_for("extract").model == "deepseek-v5-ultra"


def test_parse_rejects_dangling_provider_ref(tmp_path: Path):
    """路由引用未声明的 provider → 解析即报错(fail loud,不静默)。"""
    yml = tmp_path / "bad.yaml"
    yml.write_text(textwrap.dedent("""
        version: 1
        providers:
          a: {kind: openai_compat, model: m, base_url_env: LLM_BASE_URL, api_key_env: LLM_API_KEY}
        routes:
          extract: {primary: ghost, fallback: []}
        default_purpose: extract
    """), encoding="utf-8")
    with pytest.raises(ValueError):
        mr.load_registry_from(yml)


# ---------- 7. 注册表异常 → 回退旧路径(降级不崩) ----------
def test_get_client_falls_back_on_registry_error(monkeypatch, _dummy_env):
    monkeypatch.setattr(settings, "LLM_BASE_URL", "http://dummy.local/v1")
    monkeypatch.setattr(settings, "LLM_API_KEY", "dummy-key")

    def _boom(purpose):
        raise RuntimeError("模拟注册表损坏")

    monkeypatch.setattr(mr, "primary_spec_for", _boom)
    cli = lc.get_client("extract")                # 不抛,回退旧固定构造
    assert isinstance(cli, lc.OpenAICompatClient)
    assert cli.model == settings.LLM_MODEL


# ---------- 8. openai_compat 之外的 kind:构造显式报错(不静默降级) ----------
def test_unsupported_kind_raises_in_build():
    spec = mr.ProviderSpec(
        id="claude_x", kind="anthropic", model="claude-opus",
        base_url_env="X_URL", api_key_env="X_KEY")
    with pytest.raises(NotImplementedError):
        lc._build_client(spec)
