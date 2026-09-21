"""Per-task Fast control through the existing capture/apply seams."""
from __future__ import annotations

import json
import logging
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


def _attaching_host(parent, children, on_error="fail"):
    """A host-shaped harness that attaches each child like the real host does."""

    def build_child(task_index, goal, context, toolsets, model, max_iterations,
                    task_count, parent_agent, **kwargs):
        child = SimpleNamespace(
            model="fast-model", provider="openai-codex", base_url="https://example.test/v1",
            api_mode="chat_completions", service_tier=None, request_overrides={}, close=Mock(),
        )
        children.append(child)
        parent_agent._active_children.append(child)  # host attaches during construction
        return child

    wrapped_build = make_build_child_wrapper(build_child)

    def delegate_task(tasks=None, parent_agent=None, **_kwargs):
        for i, task in enumerate(tasks or []):
            wrapped_build(i, task["goal"], None, None, None, 10, len(tasks), parent_agent)
        return "ok"

    return make_delegate_task_wrapper(delegate_task, _unused_resolver, on_error=on_error)


def _attached_parent():
    parent = _parent()
    parent._active_children = []
    return parent


def test_rejected_fast_batch_closes_and_detaches_every_constructed_child(fast_resolver):
    """A rejected batch must leave no closed child referenced by the parent."""
    fast_resolver.return_value = None
    parent = _attached_parent()
    children = []
    delegate = _attaching_host(parent, children)
    with pytest.raises(ValueError, match="fast=True.*unsupported"):
        delegate(tasks=[{"goal": "first"}, {"goal": "rejected", "fast": True}],
                 parent_agent=parent)
    assert len(children) == 2
    for child in children:
        child.close.assert_called_once()
    assert parent._active_children == []


def test_non_valueerror_apply_failure_is_normalized_and_still_cleans_up(fast_resolver):
    """An unexpected error class must not escape with the batch left attached."""
    fast_resolver.side_effect = TypeError("boom from the host resolver")
    parent = _attached_parent()
    children = []
    delegate = _attaching_host(parent, children)
    with pytest.raises(ValueError, match="fast override failed"):
        delegate(tasks=[{"goal": "first"}, {"goal": "boom", "fast": True}], parent_agent=parent)
    assert parent._active_children == []
    for child in children:
        child.close.assert_called_once()


def test_fallback_keeps_the_live_child_attached_to_the_parent(fast_resolver, caplog):
    """Fallback keeps the child running, so it must stay owned by the parent."""
    fast_resolver.return_value = None
    parent = _attached_parent()
    children = []
    delegate = _attaching_host(parent, children, on_error="fallback")
    assert delegate(tasks=[{"goal": "kept", "fast": True}], parent_agent=parent) == "ok"
    assert parent._active_children == children
    children[0].close.assert_not_called()
    assert "fast" in caplog.text and "unsupported" in caplog.text


def test_apply_failure_message_names_the_child_route(fast_resolver):
    fast_resolver.return_value = None
    parent = _parent()
    children = []
    delegate = make_delegate_task_wrapper(_host(children), _unused_resolver)
    with pytest.raises(ValueError) as excinfo:
        delegate(tasks=[{"goal": "unsupported", "fast": True}], parent_agent=parent)
    message = str(excinfo.value)
    assert "model='fast-model'" in message
    assert "provider='openai-codex'" in message


@pytest.mark.parametrize("missing", ["model", "provider"])
def test_fast_on_fails_closed_when_the_route_attributes_are_missing(missing, fast_resolver):
    from hermes_delegate_routing.patches import _apply_fast_override

    child = _parent()
    delattr(child, missing)
    with pytest.raises(ValueError, match="Fast.*attribute"):
        _apply_fast_override(child, True)


def test_non_mapping_request_overrides_fails_closed(fast_resolver):
    from hermes_delegate_routing.patches import _apply_fast_override

    child = _parent(request_overrides=5)
    with pytest.raises(ValueError, match="not a mapping"):
        _apply_fast_override(child, False)


def test_fast_applies_to_a_provider_only_task(fast_resolver):
    children = []
    resolver = Mock(return_value={"provider": "other-provider"})
    delegate = make_delegate_task_wrapper(_host(children), resolver)
    assert delegate(tasks=[
        {"goal": "provider only", "provider": "other-provider", "fast": True},
    ], parent_agent=_parent()) == "ok"
    child = children[0]
    assert (child.model, child.provider) == ("fast-model", "other-provider")
    assert child.request_overrides == {"service_tier": "priority"}
    assert child.service_tier == "priority"


def test_off_preserves_non_fast_tiers(fast_resolver):
    from hermes_delegate_routing.patches import _apply_fast_override

    inherited = {
        "service_tier": "flex", "speed": "standard",
        "extra_body": {"service_tier": "flex", "speed": "standard"},
    }
    child = _parent(request_overrides=inherited)
    _apply_fast_override(child, False)
    assert child.request_overrides == inherited
    assert child.service_tier is None


def test_invalid_fast_value_rejects_the_whole_mixed_batch():
    host = Mock(return_value="ok")
    delegate = make_delegate_task_wrapper(host, _unused_resolver)
    raw = delegate(tasks=[{"goal": "valid"}, {"goal": "invalid", "fast": "yes"}])
    host.assert_not_called()
    error = json.loads(raw)["error"]
    assert "invalid fast value" in error
    assert "for task 1" in error


@pytest.mark.parametrize("value,shown", [(1, "got 1"), ("true", "got 'true'"), (None, "got None")])
def test_invalid_fast_error_shows_the_value_without_a_route_hint(value, shown):
    host = Mock(return_value="ok")
    delegate = make_delegate_task_wrapper(host, _unused_resolver)
    error = json.loads(delegate(tasks=[{"goal": "bad", "fast": value}]))["error"]
    assert shown in error
    assert "Model:" not in error


def test_invalid_fast_type_falls_back_to_batch_routing(fast_resolver, caplog):
    children = []
    delegate = make_delegate_task_wrapper(_host(children), _unused_resolver, on_error="fallback")
    assert delegate(tasks=[
        {"goal": "invalid fast", "model": "other-model", "reasoning_effort": "low", "fast": "yes"},
    ], parent_agent=_parent()) == "ok"
    child = children[0]
    assert child.model == "fast-model"  # the whole task override was skipped
    assert child.reasoning_config == {"effort": "high", "enabled": True}
    assert child.request_overrides == {}
    assert child.service_tier is None
    assert "failed to resolve" in caplog.text


@pytest.mark.parametrize("fast,expected", [(True, "task 0 fast=True applied"),
                                           (False, "task 0 fast=False applied")])
def test_success_log_line_is_the_documented_evidence(fast, expected, fast_resolver, caplog):
    """The bundled skill teaches this exact line as Fast application evidence."""
    caplog.set_level(logging.INFO)
    children = []
    delegate = make_delegate_task_wrapper(_host(children), _unused_resolver)
    assert delegate(tasks=[{"goal": "on", "fast": fast}], parent_agent=_parent()) == "ok"
    assert f"delegate-routing: {expected}" in caplog.text
