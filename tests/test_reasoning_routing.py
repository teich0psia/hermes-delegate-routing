from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_delegate_routing.patches import (
    make_build_child_wrapper,
    make_delegate_task_wrapper,
    make_schema_override,
)


def _schema():
    return {
        "parameters": {
            "properties": {
                "tasks": {
                    "items": {
                        "properties": {
                            "goal": {"type": "string"},
                            "output_schema": {"type": "object"},
                        }
                    }
                }
            }
        }
    }


def _unused_resolver(**_kwargs):
    raise AssertionError("model/provider resolver should not run for reasoning-only tasks")


def _host_with_native_reasoning(children, *, delegation_reasoning=None):
    def build_child(
        task_index,
        goal,
        context,
        toolsets,
        model,
        max_iterations,
        task_count,
        parent_agent,
        override_provider=None,
        override_base_url=None,
        override_api_key=None,
        override_api_mode=None,
        override_acp_command=None,
        override_acp_args=None,
        role="leaf",
        **_extra,
    ):
        reasoning = delegation_reasoning
        if reasoning is None:
            reasoning = getattr(parent_agent, "reasoning_config", None)
        child = SimpleNamespace(reasoning_config=reasoning)
        children.append(child)
        return child

    wrapped_build = make_build_child_wrapper(build_child)

    def delegate_task(
        goal=None,
        context=None,
        tasks=None,
        max_iterations=None,
        role=None,
        background=None,
        parent_agent=None,
        **_extra,
    ):
        for i, task in enumerate(tasks or []):
            wrapped_build(
                i,
                task["goal"],
                task.get("context"),
                None,
                "batch-model",
                10,
                len(tasks),
                parent_agent,
                override_provider="batch-provider",
            )
        return "ok"

    return delegate_task


def test_schema_advertises_reasoning_effort_without_losing_host_fields():
    wrapped = make_schema_override(_schema)
    out = wrapped()
    props = out["parameters"]["properties"]["tasks"]["items"]["properties"]
    assert props["reasoning_effort"]["type"] == "string"
    assert "model" in props and "provider" in props
    assert "output_schema" in props


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        ("none", {"enabled": False}),
        (False, {"enabled": False}),
        ("minimal", {"enabled": True, "effort": "minimal"}),
        ("low", {"enabled": True, "effort": "low"}),
        ("medium", {"enabled": True, "effort": "medium"}),
        ("high", {"enabled": True, "effort": "high"}),
        ("xhigh", {"enabled": True, "effort": "xhigh"}),
        ("max", {"enabled": True, "effort": "max"}),
        ("ultra", {"enabled": True, "effort": "ultra"}),
    ],
)
def test_task_reasoning_uses_host_parser_and_beats_native_resolution(effort, expected):
    children = []
    parent = SimpleNamespace(reasoning_config={"enabled": True, "effort": "low"})
    host = _host_with_native_reasoning(
        children,
        delegation_reasoning={"enabled": True, "effort": "medium"},
    )
    wrapped = make_delegate_task_wrapper(host, _unused_resolver)

    assert wrapped(
        tasks=[{"goal": "a", "reasoning_effort": effort}],
        parent_agent=parent,
    ) == "ok"
    assert children[0].reasoning_config == expected


def test_omitted_reasoning_preserves_delegation_then_parent_inheritance():
    parent = SimpleNamespace(reasoning_config={"enabled": True, "effort": "low"})

    delegated_children = []
    delegated_host = _host_with_native_reasoning(
        delegated_children,
        delegation_reasoning={"enabled": True, "effort": "medium"},
    )
    make_delegate_task_wrapper(delegated_host, _unused_resolver)(
        tasks=[{"goal": "a"}], parent_agent=parent
    )
    assert delegated_children[0].reasoning_config == {"enabled": True, "effort": "medium"}

    inherited_children = []
    inherited_host = _host_with_native_reasoning(inherited_children)
    make_delegate_task_wrapper(inherited_host, _unused_resolver)(
        tasks=[{"goal": "a"}], parent_agent=parent
    )
    assert inherited_children[0].reasoning_config == parent.reasoning_config


def test_invalid_reasoning_fail_hard_skips_host():
    called = False

    def host(**_kwargs):
        nonlocal called
        called = True
        return "ok"

    wrapped = make_delegate_task_wrapper(host, _unused_resolver, on_error="fail")
    raw = wrapped(tasks=[{"goal": "a", "reasoning_effort": "warp-11"}])
    assert called is False
    assert "reasoning_effort" in json.loads(raw)["error"]


def test_invalid_reasoning_fallback_preserves_native_reasoning():
    children = []
    host = _host_with_native_reasoning(
        children,
        delegation_reasoning={"enabled": True, "effort": "medium"},
    )
    wrapped = make_delegate_task_wrapper(host, _unused_resolver, on_error="fallback")
    assert wrapped(tasks=[{"goal": "a", "reasoning_effort": "warp-11"}]) == "ok"
    assert children[0].reasoning_config == {"enabled": True, "effort": "medium"}


def test_build_child_forwards_current_host_provider_personality_fields():
    seen = {}

    def build_child(
        task_index,
        goal,
        context,
        toolsets,
        model,
        max_iterations,
        task_count,
        parent_agent,
        override_provider=None,
        override_base_url=None,
        override_api_key=None,
        override_api_mode=None,
        override_request_overrides=None,
        override_max_tokens=None,
        override_acp_command=None,
        override_acp_args=None,
        role="leaf",
    ):
        seen["request_overrides"] = override_request_overrides
        seen["max_tokens"] = override_max_tokens
        return SimpleNamespace(reasoning_config=None)

    from hermes_delegate_routing._state import ROUTING

    token = ROUTING.set(
        {
            0: {
                "model": "routed",
                "provider": "custom-provider",
                "request_overrides": {"extra_body": {"thinking": {"type": "disabled"}}},
                "max_output_tokens": 321,
            }
        }
    )
    try:
        make_build_child_wrapper(build_child)(
            0,
            "goal",
            None,
            None,
            "batch",
            10,
            1,
            SimpleNamespace(),
            override_request_overrides={"stale": True},
            override_max_tokens=999,
        )
    finally:
        ROUTING.reset(token)

    assert seen["request_overrides"] == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }
    assert seen["max_tokens"] == 321
