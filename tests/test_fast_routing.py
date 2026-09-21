"""Per-task Fast control through the existing capture/apply seams."""
from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_delegate_routing.patches import (
    make_build_child_wrapper,
    make_delegate_task_wrapper,
    make_schema_override,
)


@pytest.fixture
def fast_resolver(monkeypatch):
    module = ModuleType("hermes_cli.models")
    module.resolve_fast_mode_overrides = Mock(return_value={"service_tier": "priority"})
    monkeypatch.setitem(sys.modules, "hermes_cli.models", module)
    return module.resolve_fast_mode_overrides


def _parent(**kwargs):
    return SimpleNamespace(
        model="fast-model", provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex", api_mode="codex_responses",
        reasoning_config={"effort": "high", "enabled": True},
        **{"service_tier": None, "request_overrides": {}, **kwargs},
    )


def _host(children):
    def build_child(task_index, goal, context, toolsets, model, max_iterations,
                    task_count, parent_agent, **kwargs):
        # Deliberately share inherited dictionaries: the plugin must copy, not mutate.
        child = SimpleNamespace(
            model=model or parent_agent.model,
            provider=kwargs.get("override_provider") or parent_agent.provider,
            base_url=kwargs.get("override_base_url") or parent_agent.base_url,
            api_mode=parent_agent.api_mode,
            service_tier=parent_agent.service_tier,
            request_overrides=kwargs.get(
                "override_request_overrides", parent_agent.request_overrides
            ),
            reasoning_config=parent_agent.reasoning_config,
            close=Mock(),
        )
        children.append(child)
        return child

    wrapped_build = make_build_child_wrapper(build_child)

    def delegate_task(tasks=None, parent_agent=None, **_kwargs):
        for i, task in enumerate(tasks or []):
            wrapped_build(i, task["goal"], None, None, None, 10, len(tasks), parent_agent)
        return "ok"

    return delegate_task


def _unused_resolver(**_kwargs):
    raise AssertionError("Fast-only tasks must not resolve a new model/provider")


def test_fast_on_is_child_local_without_changing_model_or_reasoning(fast_resolver):
    parent = _parent(request_overrides={"extra_body": {"thinking": {"type": "enabled"}}})
    children = []
    delegate = make_delegate_task_wrapper(_host(children), _unused_resolver)

    assert delegate(tasks=[{"goal": "on", "fast": True}], parent_agent=parent) == "ok"
    child = children[0]
    assert child.request_overrides == {
        "service_tier": "priority", "extra_body": {"thinking": {"type": "enabled"}},
    }
    assert child.service_tier == "priority"
    assert child.model == parent.model
    assert child.reasoning_config == parent.reasoning_config
    assert parent.request_overrides == {"extra_body": {"thinking": {"type": "enabled"}}}
    assert parent.service_tier is None
    fast_resolver.assert_called_once_with(
        parent.model, provider=parent.provider, base_url=parent.base_url,
    )


@pytest.mark.parametrize("mode", [None, "priority", "auto", "cold"])
def test_off_omitted_and_on_are_isolated_in_one_batch(mode, fast_resolver):
    inherited = {
        "service_tier": "priority", "speed": "fast",
        "extra_body": {
            "service_tier": "priority", "speed": "fast", "thinking": {"type": "enabled"},
        },
    }
    parent = _parent(service_tier=mode, request_overrides=inherited)
    children = []
    delegate = make_delegate_task_wrapper(_host(children), _unused_resolver)
    assert delegate(tasks=[
        {"goal": "off", "fast": False}, {"goal": "inherit"}, {"goal": "on", "fast": True},
    ], parent_agent=parent) == "ok"

    off, omitted, on = children
    assert off.service_tier is None
    assert off.request_overrides == {"extra_body": {"thinking": {"type": "enabled"}}}
    assert omitted.request_overrides == inherited
    assert omitted.service_tier == mode
    assert on.request_overrides == {
        "service_tier": "priority", "extra_body": {"thinking": {"type": "enabled"}},
    }
    assert parent.service_tier == mode
    assert parent.request_overrides == inherited
    assert inherited["extra_body"]["service_tier"] == "priority"
    fast_resolver.assert_called_once()


def test_fast_schema_is_optional_boolean_without_a_default():
    task = {"type": "object", "properties": {"goal": {"type": "string"}},
            "required": ["goal"]}
    schema = {"parameters": {"properties": {"tasks": {"items": task}}}}
    result = make_schema_override(lambda: schema)()
    item = result["parameters"]["properties"]["tasks"]["items"]
    assert item["properties"]["fast"]["type"] == "boolean"
    assert "default" not in item["properties"]["fast"]
    assert item["required"] == ["goal"]
    assert "fast" not in result["parameters"]["properties"]
    assert "fast" not in task["properties"]


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}])
def test_fast_rejects_non_boolean_values_before_delegating(value):
    host = Mock(return_value="ok")
    delegate = make_delegate_task_wrapper(host, _unused_resolver)
    raw = delegate(tasks=[{"goal": "invalid", "fast": value}])
    host.assert_not_called()
    result = json.loads(raw)
    assert "fast" in result["error"]
    assert "boolean" in result["error"]


def test_unsupported_fast_fails_without_mutation_and_closes_child(fast_resolver):
    fast_resolver.return_value = None
    parent = _parent(request_overrides={"service_tier": "priority"})
    children = []
    delegate = make_delegate_task_wrapper(_host(children), _unused_resolver)
    with pytest.raises(ValueError, match="fast=True.*unsupported"):
        delegate(tasks=[{"goal": "unsupported", "fast": True}], parent_agent=parent)
    assert parent.request_overrides == {"service_tier": "priority"}
    children[0].close.assert_called_once()


def test_fast_capability_failure_fallback_keeps_other_explicit_overrides(fast_resolver, caplog):
    fast_resolver.return_value = None
    children = []
    resolver = Mock(return_value={"model": "other-model", "provider": "other-provider"})
    delegate = make_delegate_task_wrapper(_host(children), resolver, on_error="fallback")
    assert delegate(tasks=[{
        "goal": "unsupported", "model": "other-model", "provider": "other-provider",
        "reasoning_effort": "low", "fast": True,
    }], parent_agent=_parent()) == "ok"
    child = children[0]
    assert (child.model, child.provider) == ("other-model", "other-provider")
    assert child.reasoning_config == {"effort": "low", "enabled": True}
    assert child.request_overrides == {}
    assert child.service_tier is None
    child.close.assert_not_called()
    assert "fast" in caplog.text and "unsupported" in caplog.text


def test_missing_fast_helper_fails_clearly_and_closes_child(fast_resolver, monkeypatch):
    monkeypatch.delattr(sys.modules["hermes_cli.models"], "resolve_fast_mode_overrides")
    children = []
    delegate = make_delegate_task_wrapper(_host(children), _unused_resolver)
    with pytest.raises(ValueError, match="Fast.*unavailable"):
        delegate(tasks=[{"goal": "old host", "fast": True}], parent_agent=_parent())
    children[0].close.assert_called_once()


@pytest.mark.parametrize("missing", ["request_overrides", "service_tier"])
def test_explicit_fast_fails_closed_on_host_attribute_drift(missing, fast_resolver):
    from hermes_delegate_routing.patches import _apply_fast_override

    child = _parent()
    delattr(child, missing)
    with pytest.raises(ValueError, match="Fast.*attribute"):
        _apply_fast_override(child, False)


def test_anthropic_fast_uses_its_native_endpoint_and_speed_parameter(fast_resolver):
    from hermes_delegate_routing.patches import _apply_fast_override

    child = _parent()
    child.model = "claude-opus-4-8"
    child.provider = "anthropic"
    child.api_mode = "anthropic_messages"
    child._anthropic_base_url = "https://api.anthropic.com"
    child.base_url = "https://unused-openai-client.test/v1"
    fast_resolver.return_value = {"speed": "fast"}
    _apply_fast_override(child, True)
    assert child.request_overrides == {"speed": "fast"}
    fast_resolver.assert_called_once_with(
        child.model, provider="anthropic", base_url="https://api.anthropic.com",
    )


def test_explicit_on_beats_conflicting_extra_body_tier(fast_resolver):
    from hermes_delegate_routing.patches import _apply_fast_override

    child = _parent(request_overrides={"extra_body": {"service_tier": "default"}})
    _apply_fast_override(child, True)
    assert child.request_overrides["service_tier"] == "priority"
    assert "service_tier" not in child.request_overrides["extra_body"]


def test_rejected_fast_batch_also_closes_previously_constructed_children(fast_resolver):
    fast_resolver.return_value = None
    children = []
    delegate = make_delegate_task_wrapper(_host(children), _unused_resolver)
    with pytest.raises(ValueError, match="fast=True.*unsupported"):
        delegate(tasks=[
            {"goal": "constructed first"}, {"goal": "rejected", "fast": True},
        ], parent_agent=_parent())
    assert len(children) == 2
    for child in children:
        child.close.assert_called_once()
