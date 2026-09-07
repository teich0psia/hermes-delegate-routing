"""Tests for the vendored model/provider override resolver.

Ported from upstream PR #36790's resolver coverage. Runs against fake
``hermes_cli.*`` modules (see conftest) or a real host — tests patch the module
attributes either way.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_delegate_routing.resolver import resolve_model_provider_override


def _parent():
    return SimpleNamespace(provider="anthropic", model="opus", base_url="", api_key="")


def _switch_result(**kw):
    base = dict(
        success=True,
        new_model=None,
        target_provider=None,
        base_url=None,
        api_key=None,
        api_mode=None,
        error_message=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_resolves_model_with_structured_provider():
    with patch("hermes_cli.config.load_config", return_value={}), patch(
        "hermes_cli.model_switch.switch_model"
    ) as mock_switch, patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={"command": None, "args": []},
    ):
        mock_switch.return_value = _switch_result(
            new_model="stepfun/step-3.5-flash", target_provider="openrouter"
        )
        creds = resolve_model_provider_override(
            model_input="stepfun/step-3.5-flash",
            provider_input="openrouter",
            parent_agent=_parent(),
        )
    assert creds["provider"] == "openrouter"
    assert creds["model"] == "stepfun/step-3.5-flash"
    assert mock_switch.call_args.kwargs["explicit_provider"] == "openrouter"


def test_resolves_inline_provider_flag():
    with patch("hermes_cli.config.load_config", return_value={}), patch(
        "hermes_cli.model_switch.switch_model"
    ) as mock_switch, patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={"command": None, "args": []},
    ):
        mock_switch.return_value = _switch_result(
            new_model="stepfun/step-3.5-flash", target_provider="openrouter"
        )
        resolve_model_provider_override(
            model_input="stepfun/step-3.5-flash --provider openrouter",
            provider_input=None,
            parent_agent=_parent(),
        )
    assert mock_switch.call_args.kwargs["explicit_provider"] == "openrouter"


def test_conflicting_structured_and_inline_provider_fails():
    with pytest.raises(ValueError) as ctx:
        resolve_model_provider_override(
            model_input="sonnet --provider anthropic",
            provider_input="openrouter",
            parent_agent=_parent(),
        )
    assert "Conflicting provider overrides" in str(ctx.value)


def test_empty_input_fails():
    with pytest.raises(ValueError) as ctx:
        resolve_model_provider_override(
            model_input="", provider_input=None, parent_agent=_parent()
        )
    assert "empty" in str(ctx.value)


def test_switch_model_failure_raises():
    with patch("hermes_cli.model_switch.switch_model") as mock_switch:
        mock_switch.return_value = _switch_result(
            success=False, error_message="no such model"
        )
        with pytest.raises(ValueError) as ctx:
            resolve_model_provider_override(
                model_input="nope", provider_input=None, parent_agent=_parent()
            )
    assert "no such model" in str(ctx.value)


def test_tolerates_parse_model_flags_arity_growth():
    """Regression: host parse_model_flags gained a 5th return value (is_session);
    the resolver must read only elements [0]/[1] and ignore the growing tail."""
    with patch(
        "hermes_cli.model_switch.parse_model_flags",
        return_value=("m", "", 0, 0, 0, "future"),
    ), patch("hermes_cli.config.load_config", return_value={}), patch(
        "hermes_cli.model_switch.switch_model"
    ) as mock_switch, patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={"command": None, "args": []},
    ):
        mock_switch.return_value = _switch_result(new_model="m", target_provider="anthropic")
        creds = resolve_model_provider_override(
            model_input="m", provider_input=None, parent_agent=_parent()
        )
    assert creds["model"] == "m"


def test_model_only_same_provider_takes_effect():
    # Isolate from the operator's real delegation.* config: on a live host
    # load_config() returns the operator's baseline (e.g. a custom provider),
    # which correctly takes precedence over the synthetic parent.
    with patch("hermes_cli.config.load_config", return_value={}), patch(
        "hermes_cli.model_switch.switch_model"
    ) as mock_switch, patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={"command": None, "args": []},
    ):
        mock_switch.return_value = _switch_result(
            new_model="glm-5", target_provider="anthropic"
        )
        creds = resolve_model_provider_override(
            model_input="glm-5", provider_input=None, parent_agent=_parent()
        )
    assert creds["model"] == "glm-5"
    assert creds["provider"] == "anthropic"
    # Provider omitted at task level: pin the inherited parent provider rather
    # than letting /model auto-detect a different one from the model name.
    assert mock_switch.call_args.kwargs["explicit_provider"] == "anthropic"


def test_model_only_inherits_delegation_provider_before_parent():
    with patch(
        "hermes_cli.config.load_config",
        return_value={
            "delegation": {
                "model": "delegation-model",
                "provider": "openrouter",
                "base_url": "https://delegation.test/v1",
                "api_key": "delegation-key",
            }
        },
    ), patch("hermes_cli.model_switch.switch_model") as mock_switch, patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={"command": None, "args": []},
    ):
        mock_switch.return_value = _switch_result(
            new_model="task-model", target_provider="openrouter"
        )
        route = resolve_model_provider_override(
            model_input="task-model", provider_input=None, parent_agent=_parent()
        )

    kwargs = mock_switch.call_args.kwargs
    assert kwargs["raw_input"] == "task-model"
    assert kwargs["explicit_provider"] == "openrouter"
    assert kwargs["current_provider"] == "openrouter"
    assert kwargs["current_model"] == "delegation-model"
    assert kwargs["current_base_url"] == "https://delegation.test/v1"
    assert kwargs["current_api_key"] == "delegation-key"
    assert route["model"] == "task-model"
    assert route["provider"] == "openrouter"


def test_provider_only_inherits_delegation_model_before_parent():
    with patch(
        "hermes_cli.config.load_config",
        return_value={
            "delegation": {
                "model": "delegation-model",
                "provider": "openrouter",
            }
        },
    ), patch("hermes_cli.model_switch.switch_model") as mock_switch, patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={"command": None, "args": []},
    ):
        mock_switch.return_value = _switch_result(
            new_model="delegation-model", target_provider="deepseek"
        )
        route = resolve_model_provider_override(
            model_input=None, provider_input="deepseek", parent_agent=_parent()
        )

    kwargs = mock_switch.call_args.kwargs
    assert kwargs["raw_input"] == "delegation-model"
    assert kwargs["explicit_provider"] == "deepseek"
    assert kwargs["current_provider"] == "openrouter"
    assert kwargs["current_model"] == "delegation-model"
    assert route["model"] == "delegation-model"
    assert route["provider"] == "deepseek"


def test_model_only_preserves_direct_delegation_base_url():
    with patch(
        "hermes_cli.config.load_config",
        return_value={
            "delegation": {
                "model": "delegation-model",
                "base_url": "https://delegation.test/v1",
                "api_key": "delegation-key",
            }
        },
    ), patch("hermes_cli.model_switch.switch_model") as mock_switch, patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={"command": None, "args": []},
    ):
        mock_switch.return_value = _switch_result(
            new_model="task-model", target_provider="custom"
        )
        resolve_model_provider_override(
            model_input="task-model", provider_input=None, parent_agent=_parent()
        )

    kwargs = mock_switch.call_args.kwargs
    assert kwargs["explicit_provider"] == "custom"
    assert kwargs["current_provider"] == "custom"
    assert kwargs["current_base_url"] == "https://delegation.test/v1"
    assert kwargs["current_api_key"] == "delegation-key"


def test_preserves_current_host_request_overrides_and_runtime_max_output_tokens():
    with patch("hermes_cli.config.load_config", return_value={}), patch(
        "hermes_cli.model_switch.switch_model"
    ) as mock_switch, patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={
            "command": None,
            "args": [],
            "request_overrides": {"ignored": "switch result already won"},
            "max_output_tokens": 4096,
        },
    ):
        result = _switch_result(new_model="m", target_provider="custom")
        result.request_overrides = {"extra_body": {"chat_template_kwargs": {"x": 1}}}
        mock_switch.return_value = result
        route = resolve_model_provider_override(
            model_input="m", provider_input="custom", parent_agent=_parent()
        )

    assert route["request_overrides"] == {
        "extra_body": {"chat_template_kwargs": {"x": 1}}
    }
    assert route["max_output_tokens"] == 4096


def test_runtime_request_overrides_are_used_when_switch_result_lacks_field():
    with patch("hermes_cli.config.load_config", return_value={}), patch(
        "hermes_cli.model_switch.switch_model"
    ) as mock_switch, patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={
            "command": None,
            "args": [],
            "request_overrides": {"extra_body": {"thinking": {"type": "disabled"}}},
        },
    ):
        mock_switch.return_value = _switch_result(new_model="m", target_provider="custom")
        route = resolve_model_provider_override(
            model_input="m", provider_input="custom", parent_agent=_parent()
        )

    assert route["request_overrides"] == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }
