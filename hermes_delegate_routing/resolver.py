"""Model/provider override resolver.

Vendored from the (rejected) upstream PR
https://github.com/NousResearch/hermes-agent/pull/36790 — the function
``_resolve_model_provider_override``. It reuses the host's ``/model`` switch
pipeline so aliases, provider catalogs, custom providers, and ``--provider``
syntax resolve exactly as they do for the ``/model`` command.

We vendor rather than import because this function only ever existed inside the
rejected PR; it is not in released hermes-agent. Host imports are done lazily
inside the function so (a) import failures degrade to a clear ValueError and
(b) unit tests can stub ``hermes_cli.*`` (see tests/conftest.py).

Returns a route dict shaped like the host's
``_resolve_delegation_credentials`` so it flows straight into
``_build_child_agent``. On newer Hermes releases it also preserves
``request_overrides`` / ``max_output_tokens`` when the host resolver exposes them.
"""

from __future__ import annotations

from typing import Any


def resolve_model_provider_override(
    *,
    model_input: str | None,
    provider_input: str | None,
    parent_agent,
) -> dict:
    """Resolve an explicit delegate_task model/provider override.

    Raises ValueError on empty/unparseable/conflicting input or a failed switch.
    """
    raw_model = str(model_input or "").strip()
    explicit_provider = str(provider_input or "").strip()
    if not raw_model and not explicit_provider:
        raise ValueError("model/provider override is empty")

    try:
        from hermes_cli.model_switch import parse_model_flags, switch_model
    except Exception as exc:  # pragma: no cover - defensive import guard
        raise ValueError(
            f"Cannot import model switch resolver for delegation override: {exc}"
        ) from exc

    # parse_model_flags returns (model, provider, *flags). The flag tail has
    # grown across host versions (4-tuple in PR #36790, 5-tuple today), so read
    # only the first two positionally to stay arity-agnostic.
    parsed = parse_model_flags(raw_model)
    parsed_model = parsed[0] if len(parsed) > 0 else ""
    parsed_provider = parsed[1] if len(parsed) > 1 else ""
    if explicit_provider and parsed_provider and explicit_provider != parsed_provider:
        raise ValueError(
            f"Conflicting provider overrides: provider={explicit_provider!r} "
            f"but model contains --provider {parsed_provider!r}."
        )
    provider_for_switch = explicit_provider or parsed_provider
    if not parsed_model and not provider_for_switch:
        raise ValueError(f"Could not parse model/provider override: {raw_model!r}")

    user_providers = None
    custom_providers = None
    full_cfg: dict[str, Any] = {}
    try:
        from hermes_cli.config import load_config

        loaded = load_config() or {}
        if isinstance(loaded, dict):
            full_cfg = loaded
            user_providers = full_cfg.get("providers")
            custom_providers = full_cfg.get("custom_providers")
    except Exception:  # pragma: no cover - config is best-effort
        pass

    # Resolve omitted task fields from the configured delegation baseline:
    # task > delegation config > parent. The upstream plugin used parent_agent
    # directly here, which meant model-only overrides could bypass
    # delegation.provider (and provider-only overrides could lose
    # delegation.model).
    delegation_cfg: dict[str, Any] = {}
    configured = full_cfg.get("delegation")
    if isinstance(configured, dict):
        delegation_cfg = configured

    parent_model = getattr(parent_agent, "model", "") or ""
    parent_provider = getattr(parent_agent, "provider", "") or ""
    parent_base_url = getattr(parent_agent, "base_url", "") or ""
    parent_api_key = getattr(parent_agent, "api_key", "") or ""

    delegation_model = str(delegation_cfg.get("model") or "").strip()
    delegation_provider = str(delegation_cfg.get("provider") or "").strip()
    delegation_base_url = str(delegation_cfg.get("base_url") or "").strip()
    delegation_api_key = str(delegation_cfg.get("api_key") or "").strip()

    baseline_model = delegation_model or parent_model
    baseline_provider = delegation_provider or parent_provider
    baseline_base_url = delegation_base_url or parent_base_url
    baseline_api_key = delegation_api_key or parent_api_key

    # A direct delegation.base_url without an explicit provider is Hermes'
    # generic custom-endpoint branch. Pin that endpoint instead of falling back
    # to the parent's provider; switch_model still owns URL/api-mode resolution.
    if delegation_base_url and not delegation_provider:
        baseline_provider = "custom"

    # Structured/inline task provider wins. Otherwise pin the inherited
    # delegation/parent provider so a model-only task cannot silently hop to a
    # different provider. Provider-only tasks similarly reuse the inherited
    # delegation/parent model instead of auto-detecting a provider default.
    provider_for_switch = explicit_provider or parsed_provider or baseline_provider
    model_for_switch = parsed_model or baseline_model
    if not model_for_switch:
        raise ValueError(
            "Could not resolve a model for provider override; set tasks[i].model, "
            "delegation.model, or a parent model."
        )

    result = switch_model(
        raw_input=model_for_switch,
        current_provider=baseline_provider,
        current_model=baseline_model,
        current_base_url=baseline_base_url,
        current_api_key=baseline_api_key,
        is_global=False,
        explicit_provider=provider_for_switch,
        user_providers=user_providers,
        custom_providers=custom_providers,
    )
    if not result.success:
        raise ValueError(
            f"Cannot resolve delegation model/provider override "
            f"{raw_model or provider_for_switch!r}: "
            f"{result.error_message or 'unknown error'}"
        )

    request_overrides = getattr(result, "request_overrides", None)
    if request_overrides is not None:
        request_overrides = dict(request_overrides or {})

    creds: dict[str, Any] = {
        "model": result.new_model or None,
        "provider": result.target_provider or None,
        "base_url": result.base_url or None,
        "api_key": result.api_key or None,
        "api_mode": result.api_mode or None,
        "request_overrides": request_overrides,
        "max_output_tokens": None,
        "command": None,
        "args": [],
    }

    # Preserve runtime-provider metadata switch_model does not expose directly,
    # e.g. ACP command/args and max-output settings for provider personalities.
    # Older hosts may not expose these keys; leaving them as None makes the
    # apply seam preserve the host's pre-existing delegation values unchanged.
    if creds["provider"]:
        try:
            from hermes_cli.runtime_provider import resolve_runtime_provider

            runtime = resolve_runtime_provider(
                requested=creds["provider"],
                target_model=creds["model"],
            )
            creds["command"] = runtime.get("command")
            creds["args"] = list(runtime.get("args") or [])
            if creds["request_overrides"] is None and "request_overrides" in runtime:
                creds["request_overrides"] = dict(runtime.get("request_overrides") or {})
            creds["max_output_tokens"] = runtime.get("max_output_tokens")
        except Exception:  # pragma: no cover - runtime metadata is best-effort
            pass

    return creds
