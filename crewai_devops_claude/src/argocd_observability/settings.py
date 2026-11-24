"""Settings (pydantic-settings). Values come from the environment or ``.env``; nothing is hard-coded.

Standard names are used where they exist (``ARGOCD_SERVER``, ``ARGOCD_AUTH_TOKEN``, ``ARGOCD_INSECURE``,
``PROMETHEUS_URL``, ``MODEL``); everything specific to this agent is prefixed ``ARGOCD_OBS_``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AuthMode = Literal["auto", "kube", "argocd"]


class ObservabilitySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    # --- auth / backends -------------------------------------------------------------
    auth_mode: AuthMode = Field("auto", validation_alias=AliasChoices("ARGOCD_OBS_AUTH_MODE", "auth_mode"))
    argocd_server: str | None = Field(None, validation_alias=AliasChoices("ARGOCD_SERVER", "argocd_server"))
    argocd_auth_token: SecretStr | None = Field(
        None, validation_alias=AliasChoices("ARGOCD_AUTH_TOKEN", "argocd_auth_token"))
    argocd_insecure: bool = Field(False, validation_alias=AliasChoices("ARGOCD_INSECURE", "argocd_insecure"))
    argocd_plaintext: bool = Field(False, validation_alias=AliasChoices("ARGOCD_PLAINTEXT", "argocd_plaintext"))
    argocd_namespace: str = Field("argocd", validation_alias=AliasChoices("ARGOCD_NAMESPACE", "argocd_namespace"))

    # None -> the client's default resolution ($KUBECONFIG or ~/.kube/config, *current* context)
    kubeconfig: str | None = Field(None, validation_alias=AliasChoices("ARGOCD_OBS_KUBECONFIG", "kubeconfig"))
    kube_context: str | None = Field(None, validation_alias=AliasChoices("ARGOCD_OBS_KUBE_CONTEXT", "kube_context"))

    # --- metrics ----------------------------------------------------------------------
    prometheus_url: str | None = Field(None, validation_alias=AliasChoices("PROMETHEUS_URL", "prometheus_url"))
    prometheus_insecure: bool = Field(False, validation_alias=AliasChoices("PROMETHEUS_INSECURE", "prometheus_insecure"))
    scrape_argocd_metrics: bool = Field(
        True, validation_alias=AliasChoices("ARGOCD_OBS_SCRAPE_METRICS", "scrape_argocd_metrics"))

    # --- limits -----------------------------------------------------------------------
    log_tail_lines: int = Field(200, ge=1, validation_alias=AliasChoices("ARGOCD_OBS_LOG_TAIL_LINES", "log_tail_lines"))
    log_tail_max: int = Field(1000, ge=1, validation_alias=AliasChoices("ARGOCD_OBS_LOG_TAIL_MAX", "log_tail_max"))
    http_timeout: float = Field(15.0, gt=0, validation_alias=AliasChoices("ARGOCD_OBS_HTTP_TIMEOUT", "http_timeout"))
    metrics_timeout: float = Field(5.0, gt=0, validation_alias=AliasChoices("ARGOCD_OBS_METRICS_TIMEOUT", "metrics_timeout"))
    max_tool_output_chars: int = Field(
        12000, ge=500, validation_alias=AliasChoices("ARGOCD_OBS_MAX_TOOL_OUTPUT_CHARS", "max_tool_output_chars"))
    max_response_bytes: int = Field(
        20_000_000, ge=10_000, validation_alias=AliasChoices("ARGOCD_OBS_MAX_RESPONSE_BYTES", "max_response_bytes"))
    progressing_threshold_minutes: int = Field(
        10, ge=1, validation_alias=AliasChoices("ARGOCD_OBS_PROGRESSING_THRESHOLD_MINUTES", "progressing_threshold_minutes"))

    # --- LLM / agent ------------------------------------------------------------------
    # LiteLLM model string, e.g. "anthropic/claude-sonnet-5-5" or "openai/gpt-4o-mini".
    # None -> CrewAI default model. Provider credentials stay in their usual env vars.
    model: str | None = Field(None, validation_alias=AliasChoices("MODEL", "CREWAI_LLM", "model"))
    temperature: float = Field(0.1, validation_alias=AliasChoices("ARGOCD_OBS_TEMPERATURE", "temperature"))
    max_iter: int = Field(25, ge=3, validation_alias=AliasChoices("ARGOCD_OBS_MAX_ITER", "max_iter"))
    max_execution_time: int | None = Field(
        900, validation_alias=AliasChoices("ARGOCD_OBS_MAX_EXECUTION_TIME", "max_execution_time"))

    @field_validator("argocd_server")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip().rstrip("/") or None if v else None

    @property
    def argocd_base_url(self) -> str | None:
        if not self.argocd_server:
            return None
        if "://" in self.argocd_server:
            return self.argocd_server
        return ("http://" if self.argocd_plaintext else "https://") + self.argocd_server

    @property
    def has_argocd_api(self) -> bool:
        return bool(self.argocd_server and self.argocd_auth_token and self.argocd_auth_token.get_secret_value())

    def resolved_mode(self) -> Literal["kube", "argocd"]:
        if self.auth_mode == "auto":
            return "argocd" if self.has_argocd_api else "kube"
        return self.auth_mode
