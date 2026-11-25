"""Crew-wide LLM profiles (sre_common.llm): switching, per-agent overrides, legacy fallback, native routing."""

import pytest

from sre_common.llm import build_llm, defined_profiles, llm_kwargs, resolve_profile

ENV = {
    "SRE_LLM_LOCAL_MODEL": "openai/qwen-local-test",
    "SRE_LLM_LOCAL_BASE_URL": "https://llm.example/v1/",
    "SRE_LLM_LOCAL_API_KEY": "test-key-not-real",
    "SRE_LLM_LOCAL_CONTEXT_WINDOW": "262144",
    "SRE_LLM_LOCAL_TIMEOUT": "300",
    "SRE_LLM_OPENAI_MODEL": "openai/gpt-4o-mini",
}


def test_switch_between_profiles():
    assert defined_profiles(ENV) == ["local", "openai"]
    local = resolve_profile("argocd_observability", env={**ENV, "SRE_LLM_PROFILE": "local"})
    assert (local.name, local.model, local.context_window, local.timeout) == ("local", "openai/qwen-local-test", 262144, 300.0)
    openai = resolve_profile("argocd_observability", env={**ENV, "SRE_LLM_PROFILE": "openai"})
    assert (openai.model, openai.base_url, openai.api_key) == ("openai/gpt-4o-mini", None, None)


def test_per_agent_override_and_explicit_profile():
    env = {**ENV, "SRE_LLM_PROFILE": "local", "SRE_LLM_PROFILE_K8S_CAPACITY_LEAD": "openai"}
    assert resolve_profile("k8s_capacity_lead", env=env).name == "openai"
    assert resolve_profile("k8s_capacity_analyst", env=env).name == "local"
    assert resolve_profile("k8s_capacity_lead", "LOCAL", env=env).name == "local"  # CLI flag wins


def test_undefined_profile_is_a_clear_error():
    with pytest.raises(ValueError, match="'gemini' is not defined.*SRE_LLM_GEMINI_MODEL.*local, openai"):
        resolve_profile("x", env={**ENV, "SRE_LLM_PROFILE": "gemini"})


def test_legacy_variables_without_profile():
    env = {"CREWAI_LLM": "openai/gpt-4o-mini", "CREWAI_LLM_LEAD": "openai/gpt-4o", "OPENAI_API_BASE": "http://proxy:4000"}
    lead = resolve_profile("k8s_capacity_lead", legacy_model_vars=("CREWAI_LLM_LEAD", "CREWAI_LLM"), env=env)
    assert (lead.model, lead.base_url) == ("openai/gpt-4o", "http://proxy:4000")
    assert resolve_profile("argocd_observability", env={"MODEL": "anthropic/claude-sonnet-5-5"}).model == \
        "anthropic/claude-sonnet-5-5"
    assert resolve_profile("argocd_observability", env={}) is None
    # k8s_capacity never read MODEL: a MODEL meant for another agent must not change its model
    env2 = {"MODEL": "openai/qwen-local-test", "CREWAI_LLM": "openai/gpt-4o-mini"}
    assert resolve_profile("k8s_capacity_analyst", legacy_model_vars=("CREWAI_LLM_ANALYST", "CREWAI_LLM"),
                           env=env2).model == "openai/gpt-4o-mini"


def test_api_key_never_in_repr():
    p = resolve_profile("x", env={**ENV, "SRE_LLM_PROFILE": "local"})
    assert "test-key-not-real" not in repr(p) and "test-key-not-real" not in p.describe()
    assert llm_kwargs(p)["base_url"] == "https://llm.example/v1"


def test_local_profile_builds_native_openai_client():
    """openai/<custom-model> + base_url must use CrewAI's native OpenAI client (litellm isn't installed)."""
    pytest.importorskip("crewai")
    llm = build_llm("argocd_observability", env={**ENV, "SRE_LLM_PROFILE": "local"})
    assert type(llm).__name__ == "OpenAICompletion"
    assert llm.model == "qwen-local-test"
    assert str(llm.base_url).rstrip("/") == "https://llm.example/v1"
    assert llm.supports_function_calling()
    assert llm.get_context_window_size() == int(262144 * 0.85)


def test_token_estimate_warning_logged_once_per_model(caplog):
    import logging

    from sre_common.llm import quiet_token_estimate_warning

    quiet_token_estimate_warning()
    quiet_token_estimate_warning()  # idempotent: one filter only
    log = logging.getLogger("crewai.llms.base_llm")
    msg = "Using an approximate input token count for model %r: assuming one token per four characters."
    with caplog.at_level(logging.WARNING, logger="crewai.llms.base_llm"):
        for _ in range(3):
            log.warning(msg, "qwen-local")
        log.warning(msg, "other-model")
        log.warning("unrelated warning")
    texts = [r.getMessage() for r in caplog.records]
    assert sum("qwen-local" in t for t in texts) == 1
    assert sum("other-model" in t for t in texts) == 1
    assert "unrelated warning" in texts
