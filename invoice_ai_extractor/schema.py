from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


def _bool_env(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = str(env.get(name, str(default))).strip().lower()
    return value in {"1", "true", "yes", "y", "on", "si"}


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(str(env.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(str(env.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class AiExtractorConfig:
    enabled: bool
    provider: str
    api_key: str
    model: str
    fallback_model: str
    allow_free_models: bool
    timeout_seconds: int
    max_retries: int
    debug: bool
    store_raw_response: bool
    log_raw_response: bool
    min_confidence: float
    require_critical_fields: bool
    total_tolerance: float
    fallback_legacy: bool
    config_error: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AiExtractorConfig":
        source = env or os.environ
        enabled = _bool_env(source, "INVOICE_AI_ENABLED", False)
        provider = str(source.get("INVOICE_AI_PROVIDER", "openrouter")).strip().lower() or "openrouter"
        model = str(source.get("OPENROUTER_MODEL", "")).strip()
        fallback_model = str(source.get("OPENROUTER_FALLBACK_MODEL", "")).strip()
        allow_free = _bool_env(source, "INVOICE_AI_ALLOW_FREE_MODELS", False)
        api_key = str(source.get("OPENROUTER_API_KEY", "")).strip()

        config_error = None
        if enabled and provider != "openrouter":
            config_error = "unsupported_provider"
        elif enabled and not api_key:
            config_error = "missing_openrouter_api_key"
        elif enabled and not model:
            config_error = "missing_openrouter_model"
        elif enabled and _is_free_model(model) and not allow_free:
            config_error = "free_model_not_allowed"

        return cls(
            enabled=enabled,
            provider=provider,
            api_key=api_key,
            model=model,
            fallback_model=fallback_model,
            allow_free_models=allow_free,
            timeout_seconds=_int_env(source, "INVOICE_AI_TIMEOUT_SECONDS", 60),
            max_retries=max(_int_env(source, "INVOICE_AI_MAX_RETRIES", 0), 0),
            debug=_bool_env(source, "INVOICE_AI_DEBUG", False),
            store_raw_response=_bool_env(source, "INVOICE_AI_STORE_RAW_RESPONSE", True),
            log_raw_response=_bool_env(source, "INVOICE_AI_LOG_RAW_RESPONSE", False),
            min_confidence=_float_env(source, "INVOICE_AI_MIN_CONFIDENCE", 0.70),
            require_critical_fields=_bool_env(source, "INVOICE_AI_REQUIRE_CRITICAL_FIELDS", True),
            total_tolerance=_float_env(source, "INVOICE_AI_TOTAL_TOLERANCE", 2.0),
            fallback_legacy=_bool_env(source, "INVOICE_AI_FALLBACK_LEGACY", True),
            config_error=config_error,
        )


def _is_free_model(model: str) -> bool:
    normalized = (model or "").strip().lower()
    return normalized == "openrouter/free" or ":free" in normalized
