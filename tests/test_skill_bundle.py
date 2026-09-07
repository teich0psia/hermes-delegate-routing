"""Tests for 0.3.0: self-contained schema, error skill pointer, bundled skill."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_delegate_routing.patches import (
    _MINIMAL_EXAMPLE,
    _SKILL_FALLBACK_HINT,
    _SKILL_REF,
    _redact,
    _skill_pointer,
    make_delegate_task_wrapper,
    make_schema_override,
    restore_patches,
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


def _props(builder_output=None):
    out = make_schema_override(builder_output or _host_like_builder_output)()
    return out["parameters"]["properties"]["tasks"]["items"]["properties"]


def test_schema_model_desc_is_self_contained():
    desc = _props()["model"]["description"]
    for needle in ("tasks[i]", "top-level", "cross providers", "on_error"):
        assert needle in desc, needle


def test_schema_provider_desc_is_self_contained():
    desc = _props()["provider"]["description"]
    for needle in ("tasks[i]", "top-level", "on_error"):
        assert needle in desc, needle


def test_schema_reasoning_desc_mentions_scope_and_policy():
    desc = _props()["reasoning_effort"]["description"]
    assert "tasks[i]" in desc
    assert "on_error" in desc


def test_schema_model_desc_documents_inline_provider_exception():
    assert "--provider" in _props()["model"]["description"]


def test_schema_preexisting_field_wins_for_type_and_enum():
    def _builder():
        out = _host_like_builder_output()
        props = out["parameters"]["properties"]["tasks"]["items"]["properties"]
        props["model"] = {"type": "string", "enum": ["native-a"], "description": "native"}
        props["provider"] = {"type": "string"}  # no description → ours fills in
        return out

    props = _props(_builder)
    assert props["model"] == {"type": "string", "enum": ["native-a"], "description": "native"}
    assert props["provider"]["description"]  # filled, type preserved
    assert props["provider"]["type"] == "string"


def test_skill_ref_shape():
    assert _SKILL_REF == "delegate_routing:delegate-routing"


def _raising_resolver(**_kwargs):
    raise ValueError("boom-no-such-model")


def test_fail_closed_error_points_at_skill_with_example():
    def host(**_kw):
        raise AssertionError("host must not run on fail-closed error")

    import hermes_delegate_routing.patches as _p

    _p.SKILL_LIVE = True
    try:
        wrapped = make_delegate_task_wrapper(host, _raising_resolver, on_error="fail")
        raw = wrapped(tasks=[{"goal": "a", "model": "nope"}], parent_agent=None)
    finally:
        _p.SKILL_LIVE = False
    err = json.loads(raw)["error"]
    assert _SKILL_REF in err
    assert "skill_view" in err
    assert "tasks" in err
    assert _MINIMAL_EXAMPLE in err


def test_fail_closed_error_without_live_skill_omits_dead_pointer():
    def host(**_kw):
        raise AssertionError("host must not run on fail-closed error")

    import hermes_delegate_routing.patches as _p

    assert _p.SKILL_LIVE is False  # default: register() sets it live
    wrapped = make_delegate_task_wrapper(host, _raising_resolver, on_error="fail")
    err = json.loads(wrapped(tasks=[{"goal": "a", "model": "nope"}], parent_agent=None))["error"]
    assert _SKILL_REF not in err
    assert "tasks[i]" in err  # schema guidance still present
    assert _MINIMAL_EXAMPLE in err


def test_skill_pointer_follows_live_flag():
    import hermes_delegate_routing.patches as _p

    _p.SKILL_LIVE = True
    assert _SKILL_REF in _skill_pointer()
    _p.SKILL_LIVE = False
    assert _SKILL_REF not in _skill_pointer()
    assert "tasks[i]" in _skill_pointer()
    assert _SKILL_FALLBACK_HINT == _skill_pointer()


def test_redact_removes_secret_shaped_fragments():
    leaked = "auth failed: api_key=sk-live-12345 and https://x.test/?token=abc&m=1"
    out = _redact(ValueError(leaked))
    assert "sk-live-12345" not in out
    assert "token=abc" not in out
    assert "[REDACTED]" in out
    assert "auth failed" in out


def test_redact_never_raises_and_caps_length():
    class _Bad:
        def __str__(self):
            raise RuntimeError("nope")

    assert _redact(_Bad()) == "[unprintable]"
    assert len(_redact("x" * 5000)) == 2000


def test_invalid_reasoning_error_points_at_skill():
    def host(**_kw):
        raise AssertionError("host must not run on fail-closed error")

    def _unused_resolver(**_kwargs):
        raise AssertionError("resolver must not run for reasoning-only tasks")

    import hermes_delegate_routing.patches as _p

    _p.SKILL_LIVE = True
    try:
        wrapped = make_delegate_task_wrapper(host, _unused_resolver, on_error="fail")
        raw = wrapped(tasks=[{"goal": "a", "reasoning_effort": "warp-11"}])
    finally:
        _p.SKILL_LIVE = False
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


def test_register_with_fake_ctx_registers_skill_with_description():
    import hermes_delegate_routing.patches as _p
    from hermes_delegate_routing import _register_bundled_skill

    seen = {}

    class _Ctx:
        def register_skill(self, name, path, *args, **kwargs):
            seen["name"] = name
            seen["path"] = Path(path)
            seen["args"] = args
            return object()

    assert _register_bundled_skill(_Ctx()) is True
    assert seen.get("name") == "delegate-routing"
    assert seen.get("path") is not None and seen["path"].name == "SKILL.md"
    assert seen.get("args") and seen["args"][0]  # description non-empty
    assert _p.SKILL_LIVE is False  # helper alone does not flip the live flag


def test_register_old_host_two_arg_ctx_is_supported():
    from hermes_delegate_routing import _register_bundled_skill

    seen = {}

    class _OldCtx:
        def register_skill(self, name, path):
            seen["name"] = name
            return object()

    assert _register_bundled_skill(_OldCtx()) is True
    assert seen.get("name") == "delegate-routing"


def test_register_without_ctx_is_safe():
    from hermes_delegate_routing import register

    register(None)  # must not raise


def test_register_without_register_skill_attr_is_safe():
    from hermes_delegate_routing import _register_bundled_skill

    assert _register_bundled_skill(object()) is False  # no register_skill attr


def test_register_sets_live_flag_and_hooks_unload():
    import hermes_delegate_routing.patches as _p
    from hermes_delegate_routing import register

    seen = {}

    class _Ctx:
        def register_skill(self, name, path, *args, **kwargs):
            seen["skill"] = name
            return object()

        def on_unload(self, cb):
            seen["unload"] = cb
            return object()

    old = _p.SKILL_LIVE
    try:
        register(_Ctx())
        assert seen.get("skill") == "delegate-routing"
        assert seen.get("unload") is restore_patches
        assert _p.SKILL_LIVE is True
    finally:
        _p.SKILL_LIVE = old


def test_restore_without_snapshot_is_noop():
    import hermes_delegate_routing.patches as _p

    _p._RESTORE_STATE.clear()
    restore_patches()  # must not raise


def test_version_is_0_3_0():
    import hermes_delegate_routing

    assert hermes_delegate_routing.__version__ == "0.3.0"
