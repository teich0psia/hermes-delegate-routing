"""Tests for the vendored model/provider override resolver.

Ported from upstream PR #36790's resolver coverage. Runs against fake
``hermes_cli.*`` modules (see conftest) or a real host — tests patch the module
attributes either way.
"""

from __future__ import annotations

import contextlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_delegate_routing.resolver import resolve_model_provider_override


@contextlib.contextmanager
def _stub_model_switch(*, switch_model=None, parsed=None, **parsers):
    """Install a controlled ``hermes_cli.model_switch`` for one resolution call.

    Lets a test describe the host's parser surface exactly — structured only,
    legacy only, both, or neither — independently of whichever host happens to be
    importable in this run. ``parsed`` is sugar for a structured parser returning
    that ``model_input`` / ``explicit_provider`` pair.
    """
    import hermes_cli

    stub = types.ModuleType("hermes_cli.model_switch")
    fields = dict(parsers)
    if switch_model is not None:
        fields["switch_model"] = switch_model
    if parsed is not None:
        fields["parse_model_flags_detailed"] = lambda raw: SimpleNamespace(
            model_input=parsed.get("model_input", ""),
            explicit_provider=parsed.get("explicit_provider", ""),
        )
    vars(stub).update(fields)
    with patch.object(hermes_cli, "model_switch", stub), patch.dict(
        sys.modules, {"hermes_cli.model_switch": stub}
    ):
        yield stub


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
    """Regression: legacy host parse_model_flags gained a 5th return value
    (is_session); the legacy fallback must read only elements [0]/[1] and ignore
    the growing tail."""
    with _stub_model_switch(
        switch_model=lambda **kw: _switch_result(new_model="m", target_provider="anthropic"),
        parse_model_flags=lambda raw: ("m", "", 0, 0, 0, "future"),
    ), patch("hermes_cli.config.load_config", return_value={}), patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={"command": None, "args": []},
    ):
        creds = resolve_model_provider_override(
            model_input="m", provider_input=None, parent_agent=_parent()
        )
    assert creds["model"] == "m"


def test_prefers_structured_parser_when_host_exposes_both():
    """Modern hosts expose parse_model_flags_detailed; the legacy tuple wrapper is
    ignored even when present."""
    legacy = MagicMock(side_effect=AssertionError("legacy parser must not be used"))
    detailed = MagicMock(
        return_value=SimpleNamespace(model_input="m", explicit_provider="openrouter")
    )
    switch = MagicMock(
        return_value=_switch_result(new_model="m", target_provider="openrouter")
    )
    with _stub_model_switch(
        switch_model=switch,
        parse_model_flags_detailed=detailed,
        parse_model_flags=legacy,
    ), patch("hermes_cli.config.load_config", return_value={}), patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={"command": None, "args": []},
    ):
        creds = resolve_model_provider_override(
            model_input="m --provider openrouter",
            provider_input=None,
            parent_agent=_parent(),
        )

    detailed.assert_called_once_with("m --provider openrouter")
    legacy.assert_not_called()
    assert creds["model"] == "m"
    assert creds["provider"] == "openrouter"
    assert switch.call_args.kwargs["explicit_provider"] == "openrouter"


def test_structured_parser_only_host_resolves_inline_provider():
    """Modern-only host (the installed shape): no parse_model_flags at all."""
    with _stub_model_switch(
        switch_model=lambda **kw: _switch_result(
            new_model="stepfun/step-3.5-flash", target_provider="openrouter"
        ),
        parsed={
            "model_input": "stepfun/step-3.5-flash",
            "explicit_provider": "openrouter",
        },
    ), patch("hermes_cli.config.load_config", return_value={}), patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={"command": None, "args": []},
    ):
        creds = resolve_model_provider_override(
            model_input="stepfun/step-3.5-flash --provider openrouter",
            provider_input=None,
            parent_agent=_parent(),
        )
    assert creds["model"] == "stepfun/step-3.5-flash"
    assert creds["provider"] == "openrouter"


def test_legacy_tuple_only_host_still_resolves():
    """Older host: only the tuple parser exists (structured result absent)."""
    seen = {}

    def legacy(raw):
        seen["raw"] = raw
        return ("glm-5", "openrouter", False, False, False)

    switch = MagicMock(
        return_value=_switch_result(new_model="glm-5", target_provider="openrouter")
    )
    with _stub_model_switch(
        switch_model=switch, parse_model_flags=legacy
    ), patch("hermes_cli.config.load_config", return_value={}), patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={"command": None, "args": []},
    ):
        creds = resolve_model_provider_override(
            model_input="glm-5 --provider openrouter",
            provider_input=None,
            parent_agent=_parent(),
        )

    assert seen["raw"] == "glm-5 --provider openrouter"
    assert creds["model"] == "glm-5"
    assert switch.call_args.kwargs["raw_input"] == "glm-5"
    assert switch.call_args.kwargs["explicit_provider"] == "openrouter"


def test_host_without_any_parser_fails_clearly():
    """Neither parser present: fail with a message naming both, not a raw
    AttributeError from an unrelated lookup."""
    with _stub_model_switch(
        switch_model=lambda **kw: _switch_result()
    ), pytest.raises(ValueError) as ctx:
        resolve_model_provider_override(
            model_input="m", provider_input=None, parent_agent=_parent()
        )
    message = str(ctx.value)
    assert "parse_model_flags_detailed" in message
    assert "parse_model_flags" in message


def test_selected_parser_failure_propagates_without_fallback():
    """A present structured parser that raises must not silently fall back to the
    legacy parser: a host bug would otherwise become a plausible wrong route."""
    def boom(raw):
        raise RuntimeError("structured parser exploded")

    legacy = MagicMock(side_effect=AssertionError("legacy parser must not be used"))
    with _stub_model_switch(
        switch_model=lambda **kw: _switch_result(),
        parse_model_flags_detailed=boom,
        parse_model_flags=legacy,
    ), pytest.raises(RuntimeError, match="structured parser exploded"):
        resolve_model_provider_override(
            model_input="m", provider_input=None, parent_agent=_parent()
        )
    legacy.assert_not_called()


def test_malformed_structured_result_fails_loudly_without_fallback():
    """A structured result missing a required field is a malformed host parser.

    It must raise (direct attribute access, like the removed upstream wrapper) —
    never degrade into an inherited route, and never silently fall back to the
    legacy parser. Mirrors "model field absent + structured provider supplied".
    """
    class _MissingModelField:
        explicit_provider = "openrouter"

    legacy = MagicMock(side_effect=AssertionError("legacy parser must not be used"))
    switch = MagicMock(side_effect=AssertionError("switch_model must not be called"))
    with _stub_model_switch(
        switch_model=switch,
        parse_model_flags_detailed=lambda raw: _MissingModelField(),
        parse_model_flags=legacy,
    ), pytest.raises(AttributeError):
        resolve_model_provider_override(
            model_input="m", provider_input=None, parent_agent=_parent()
        )
    switch.assert_not_called()
    legacy.assert_not_called()


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
