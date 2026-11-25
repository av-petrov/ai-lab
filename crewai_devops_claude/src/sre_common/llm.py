"""LLM profiles shared by every agent of the SRE crew.

One switch selects the model for the whole crew; profiles are defined once in the environment / .env:

    SRE_LLM_PROFILE=local                         # active profile (local | openai | any name you define)

    SRE_LLM_LOCAL_MODEL=openai/qwen3.8-27b-local  # "<provider>/<model>"; openai/ + BASE_URL = any OpenAI-compatible API
    SRE_LLM_LOCAL_BASE_URL=https://llm.local/v1
    SRE_LLM_LOCAL_API_KEY=...
    SRE_LLM_LOCAL_CONTEXT_WINDOW=262144           # model's max_input_tokens (CrewAI assumes 8192 for unknown models)
    SRE_LLM_LOCAL_TIMEOUT=300                     # seconds per call
    SRE_LLM_LOCAL_TEMPERATURE=0.1

    SRE_LLM_OPENAI_MODEL=openai/gpt-4o-mini       # api key from OPENAI_API_KEY when no _API_KEY is set

Per agent (optional): SRE_LLM_PROFILE_<AGENT>=openai, e.g. SRE_LLM_PROFILE_ARGOCD_OBSERVABILITY or
SRE_LLM_PROFILE_K8S_CAPACITY_LEAD. An explicit profile argument (e.g. a CLI flag) wins over both.

Legacy (no profile configured): the caller's own legacy model variables in its order (default MODEL, then
CREWAI_LLM; k8s_capacity: CREWAI_LLM_<ROLE>, then CREWAI_LLM), with the endpoint from OPENAI_BASE_URL /
OPENAI_API_BASE. Nothing set -> None (CrewAI default).

Why base_url is passed explicitly: CrewAI 1.15 sends an unknown "openai/<model>" to its native OpenAI client
only when base_url is a constructor argument; an env var alone makes it fall back to LiteLLM.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

PREFIX = "SRE_LLM_"
_FIELDS = ("MODEL", "BASE_URL", "API_KEY", "CONTEXT_WINDOW", "TIMEOUT", "TEMPERATURE")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


@dataclass(frozen=True)
class LLMProfile:
    name: str
    model: str
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)   # never printed
    context_window: int | None = None
    timeout: float | None = None
    temperature: float | None = None

    def describe(self) -> str:
        parts = [f"profile={self.name}", f"model={self.model}"]
        if self.base_url:
            parts.append(f"base_url={self.base_url}")
        if self.context_window:
            parts.append(f"context_window={self.context_window}")
        return " ".join(parts)


def load_env(env: Mapping[str, str] | None = None, dotenv_path: str | Path = ".env") -> dict[str, str]:
    """Process env over .env (without modifying os.environ). Tests pass ``env`` explicitly."""
    if env is not None:
        return dict(env)
    merged: dict[str, str] = {}
    try:
        from dotenv import dotenv_values

        if Path(dotenv_path).exists():
            merged.update({k: v for k, v in dotenv_values(dotenv_path).items() if v is not None})
    except ImportError:
        pass
    merged.update(os.environ)
    return merged


def agent_key(agent: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", agent.upper()).strip("_")


def defined_profiles(env: Mapping[str, str]) -> list[str]:
    names = set()
    for k, v in env.items():
        if k.startswith(PREFIX) and k.endswith("_MODEL") and v:
            names.add(k[len(PREFIX):-len("_MODEL")].lower())
    return sorted(n for n in names if _NAME_RE.match(n))


def selected_profile_name(agent: str | None, profile: str | None, env: Mapping[str, str]) -> str | None:
    if profile:
        return profile.lower()
    if agent:
        v = env.get(f"{PREFIX}PROFILE_{agent_key(agent)}")
        if v:
            return v.lower()
    v = env.get(f"{PREFIX}PROFILE")
    return v.lower() if v else None


def get_profile(name: str, env: Mapping[str, str]) -> LLMProfile:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid LLM profile name '{name}' (use lowercase letters, digits, _)")
    p = f"{PREFIX}{name.upper()}_"
    model = env.get(p + "MODEL")
    if not model:
        known = ", ".join(defined_profiles(env)) or "none"
        raise ValueError(f"LLM profile '{name}' is not defined: set {p}MODEL (defined profiles: {known})")

    def num(key: str, cast):
        raw = env.get(p + key)
        if raw in (None, ""):
            return None
        try:
            return cast(raw)
        except ValueError as exc:
            raise ValueError(f"{p}{key}={raw!r} is not a valid number") from exc

    return LLMProfile(name=name, model=model, base_url=(env.get(p + "BASE_URL") or None),
                      api_key=(env.get(p + "API_KEY") or None), context_window=num("CONTEXT_WINDOW", int),
                      timeout=num("TIMEOUT", float), temperature=num("TEMPERATURE", float))


LEGACY_MODEL_VARS: tuple[str, ...] = ("MODEL", "CREWAI_LLM")


def resolve_profile(agent: str | None = None, profile: str | None = None, *,
                    legacy_model_vars: Sequence[str] = LEGACY_MODEL_VARS,
                    env: Mapping[str, str] | None = None) -> LLMProfile | None:
    """The profile an agent should use, or None for CrewAI's default model."""
    e = load_env(env)
    name = selected_profile_name(agent, profile, e)
    if name:
        return get_profile(name, e)
    for var in legacy_model_vars:
        if e.get(var):
            return LLMProfile(name=f"legacy:{var}", model=e[var],
                              base_url=e.get("OPENAI_BASE_URL") or e.get("OPENAI_API_BASE") or None)
    return None


def llm_kwargs(p: LLMProfile, default_temperature: float = 0.1) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"model": p.model,
                              "temperature": p.temperature if p.temperature is not None else default_temperature}
    if p.base_url:
        kwargs["base_url"] = p.base_url.rstrip("/")
    if p.api_key:
        kwargs["api_key"] = p.api_key
    if p.context_window:
        kwargs["context_window_size"] = p.context_window
    if p.timeout:
        kwargs["timeout"] = p.timeout
    return kwargs


def build_llm(agent: str | None = None, profile: str | None = None, *,
              legacy_model_vars: Sequence[str] = LEGACY_MODEL_VARS,
              default_temperature: float = 0.1, env: Mapping[str, str] | None = None):
    """crewai.LLM for ``agent`` (or None -> CrewAI default). Raises ValueError for an undefined profile."""
    p = resolve_profile(agent, profile, legacy_model_vars=legacy_model_vars, env=env)
    if p is None:
        return None
    from crewai import LLM

    quiet_token_estimate_warning()
    return LLM(**llm_kwargs(p, default_temperature))


class _OncePerModel(logging.Filter):
    """Let CrewAI's "approximate input token count" warning through once per model.

    CrewAI cannot count tokens natively on an OpenAI-compatible server (local models), so it estimates
    4 chars/token and warns on *every* tool result (it checks each one against 25% of the context window).
    The estimate is fine for that check; repeating the warning is noise.
    """

    def __init__(self) -> None:
        super().__init__()
        self._seen: set[Any] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        if not str(record.msg).startswith("Using an approximate input token count"):
            return True
        key = record.args[0] if isinstance(record.args, tuple) and record.args else record.args
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


_TOKEN_ESTIMATE_LOGGER = "crewai.llms.base_llm"


def quiet_token_estimate_warning() -> None:
    log = logging.getLogger(_TOKEN_ESTIMATE_LOGGER)
    if not any(isinstance(f, _OncePerModel) for f in log.filters):
        log.addFilter(_OncePerModel())
