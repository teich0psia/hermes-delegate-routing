"""Tests for 0.3.0: self-contained schema, error skill pointer, bundled skill."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_delegate_routing.patches import (
    _MINIMAL_EXAMPLE,
    _SKILL_REF,
    make_delegate_task_wrapper,
    make_schema_override,
)


def _host_like_builder_output():
    return {
        "description": "Spawn subagents.",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string", "description": "Task goal"},
                        },
                    },
                },
            },
        },
    }


def _props():
    out = make_schema_override(_host_like_builder_output)()
    return out["parameters"]["properties"]["tasks"]["items"]["properties"]


def test_schema_model_desc_is_self_contained():
    desc = _props()["model"]["description"]
    for needle in ("tasks[i]", "top-level", "cross providers", "fail"):
        assert needle in desc, needle


def test_schema_provider_desc_is_self_contained():
    desc = _props()["provider"]["description"]
    for needle in ("tasks[i]", "top-level", "fail"):
        assert needle in desc, needle


def test_schema_reasoning_desc_mentions_scope():
    assert "tasks[i]" in _props()["reasoning_effort"]["description"]


def test_skill_ref_shape():
    assert _SKILL_REF == "delegate_routing:delegate-routing"


def _raising_resolver(**_kwargs):
    raise ValueError("boom-no-such-model")


def test_fail_closed_error_points_at_skill_with_example():
    def host(**_kw):
        raise AssertionError("host must not run on fail-closed error")

    wrapped = make_delegate_task_wrapper(host, _raising_resolver, on_error="fail")
    raw = wrapped(tasks=[{"goal": "a", "model": "nope"}], parent_agent=None)
    err = json.loads(raw)["error"]
    assert _SKILL_REF in err
    assert "skill_view" in err
    assert "tasks" in err
    assert _MINIMAL_EXAMPLE in err


def test_invalid_reasoning_error_points_at_skill():
    def host(**_kw):
        raise AssertionError("host must not run on fail-closed error")

    def _unused_resolver(**_kwargs):
        raise AssertionError("resolver must not run for reasoning-only tasks")

    wrapped = make_delegate_task_wrapper(host, _unused_resolver, on_error="fail")
    raw = wrapped(tasks=[{"goal": "a", "reasoning_effort": "warp-11"}])
    assert _SKILL_REF in json.loads(raw)["error"]


def test_bundled_skill_files_exist():
    pkg = Path(__file__).resolve().parent.parent / "hermes_delegate_routing"
    skill = pkg / "skills" / "delegate-routing" / "SKILL.md"
    ref = pkg / "skills" / "delegate-routing" / "references" / "routing-details.md"
    assert skill.is_file()
    assert ref.is_file()
    text = skill.read_text(encoding="utf-8")
    assert "name: delegate-routing" in text
    assert "tasks" in text


def test_register_with_fake_ctx_registers_skill():
    from hermes_delegate_routing import register

    seen = {}

    class _Ctx:
        def register_skill(self, name, path, *args, **kwargs):
            seen["name"] = name
            seen["path"] = Path(path)
            return object()

    register(_Ctx())
    assert seen.get("name") == "delegate-routing"
    assert seen.get("path") is not None and seen["path"].name == "SKILL.md"


def test_register_without_ctx_is_safe():
    from hermes_delegate_routing import register

    register(None)  # must not raise


def test_register_without_register_skill_attr_is_safe():
    from hermes_delegate_routing import _register_bundled_skill

    _register_bundled_skill(object())  # no register_skill attr → no-op, no raise


def test_version_is_0_3_0():
    import hermes_delegate_routing

    assert hermes_delegate_routing.__version__ == "0.3.0"
