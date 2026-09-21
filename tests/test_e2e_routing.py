"""Tier-1 end-to-end routing test (see docs/CI_E2E_TESTING.md).

Proves the full capture → apply → child → client chain: that a per-task
``model`` / ``provider`` set on ``tasks[i]`` actually reaches the OpenAI client
each subagent instantiates — not just the ``_build_child_agent`` arguments
(which ``test_integration_smoke.py`` already covers).

Runs only when a real hermes-agent host is importable; self-skips otherwise, so
the default host-free unit run stays green. To run it against a checkout:

    PYTHONPATH=/path/to/hermes-agent:. \\
      /path/to/hermes-agent/.venv/bin/python -m pytest tests/test_e2e_routing.py

Determinism: the network/catalog boundary (``switch_model``) is mocked to return
two distinct credential bundles keyed by the requested model, and the OpenAI SDK
is replaced by a recording fake — so there is no socket and no real provider.
Vanilla ``delegate_task`` runs the whole batch on one credential bundle; two
*distinct* base_urls at the client boundary is exactly what the plugin adds.
"""

from __future__ import annotations

import inspect
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

run_agent = pytest.importorskip("run_agent", reason="no hermes-agent host on path")
dt = pytest.importorskip("tools.delegate_tool", reason="no hermes-agent host on path")

from hermes_delegate_routing.patches import apply_patches  # noqa: E402

# Per-task routing table: requested model -> resolved (base_url, model, provider).
_ROUTES = {
    "route-alpha": ("https://alpha.test/v1", "resolved-alpha", "prov-alpha"),
    "route-beta": ("https://beta.test/v1", "resolved-beta", "prov-beta"),
}


def _neutral_credentials_kwargs() -> dict:
    """Avoid coupling host-backed tests to the operator's delegation provider config."""
    if "credentials_cfg" in inspect.signature(dt.delegate_task).parameters:
        return {"credentials_cfg": {"provider": None}}
    return {}


def _patch_client_ctor(recorder):
    """Patch every known OpenAI client construction site (old/new hosts).

    Older hosts build subagent clients via ``run_agent.OpenAI``; newer ones
    go through ``agent.process_bootstrap.OpenAI`` (a lazy proxy the host
    resolves at call time precisely so tests can patch it). Patch whichever
    sites exist and return their names; fail loudly when none do, so the
    test cannot silently pass without intercepting anything.
    """
    from contextlib import ExitStack

    stack = ExitStack()
    patched = []
    for target in ("agent.process_bootstrap.OpenAI", "run_agent.OpenAI"):
        try:
            stack.enter_context(patch(target, side_effect=recorder))
        except (AttributeError, ImportError, ModuleNotFoundError):
            continue
        patched.append(target)
    assert patched, "no known OpenAI client construction site to intercept"
    return stack


def _fake_switch_model(*, raw_input, **kwargs):
    """Stand in for the host /model resolver: map the requested model to a bundle."""
    route = _ROUTES.get((raw_input or "").strip())
    if route is None:
        return SimpleNamespace(success=False, error_message=f"unknown model {raw_input!r}")
    base_url, model, provider = route
    return SimpleNamespace(
        success=True,
        new_model=model,
        target_provider=provider,
        base_url=base_url,
        api_key=f"key-for-{provider}",
        api_mode="chat_completions",
        request_overrides={},
        error_message=None,
    )


def _no_tool_response(**_kwargs):
    """A minimal chat-completion with no tool calls → the child exits after one call."""
    msg = MagicMock()
    msg.content = "done"
    msg.tool_calls = None
    msg.refusal = None
    # Hermes 0.21 inspects optional reasoning fields; MagicMock's implicit
    # attributes are truthy/iterable enough to look like bogus provider data.
    msg.reasoning = None
    msg.reasoning_content = None
    msg.reasoning_details = None
    msg.model_extra = {}
    resp = MagicMock()
    resp.choices = [MagicMock(message=msg, finish_reason="stop")]
    resp.usage = MagicMock(
        prompt_tokens=1, completion_tokens=1, total_tokens=2, prompt_tokens_details=None
    )
    return resp


def test_per_task_model_provider_reaches_the_client():
    neutral_credentials = _neutral_credentials_kwargs()
    assert apply_patches() is True, "plugin should activate on a supported host"

    # Every OpenAI(...) instantiation records its base_url; correlate to model
    # via the create(model=...) call each child makes exactly once.
    seen: list[dict] = []
    seen_lock = threading.Lock()

    def _recording_openai(*_args, **kwargs):
        client = MagicMock()
        record = {"base_url": str(kwargs.get("base_url") or ""), "model": None}
        with seen_lock:
            seen.append(record)

        def _create(**call_kwargs):
            record["model"] = call_kwargs.get("model")
            return _no_tool_response(**call_kwargs)

        client.chat.completions.create = _create
        client.close = MagicMock()
        return client

    with patch("hermes_cli.model_switch.switch_model", side_effect=_fake_switch_model), patch(
        "hermes_cli.model_switch.parse_model_flags",
        side_effect=lambda raw: ((raw or "").strip(), "", False, False, False),
    ), _patch_client_ctor(_recording_openai), patch.object(
        run_agent.AIAgent, "_build_system_prompt", return_value="You are a test agent"
    ):
        parent = run_agent.AIAgent(
            base_url="https://parent.test/v1",
            api_key="parent-key",
            model="parent-model",
            provider="prov-parent",
            api_mode="chat_completions",
            max_iterations=1,
            enabled_toolsets=["terminal"],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform="cli",
        )

        raw = dt.delegate_task(
            tasks=[
                {"goal": "Return the word alpha only.", "model": "route-alpha"},
                {"goal": "Return the word beta only.", "model": "route-beta"},
            ],
            background=False,
            parent_agent=parent,
            **neutral_credentials,
        )

    result = json.loads(raw)
    assert "error" not in result, f"delegate_task failed: {result}"

    # The child clients (base_url != the parent's) must show BOTH routed
    # endpoints — vanilla delegate would show one shared bundle for the batch.
    child_pairs = {
        (r["base_url"], r["model"])
        for r in seen
        if r["base_url"] != "https://parent.test/v1"
    }
    assert child_pairs == {
        ("https://alpha.test/v1", "resolved-alpha"),
        ("https://beta.test/v1", "resolved-beta"),
    }, f"per-task routing did not reach the client boundary: {seen}"


def test_per_task_reasoning_effort_reaches_the_request_boundary():
    neutral_credentials = _neutral_credentials_kwargs()
    assert apply_patches() is True, "plugin should activate on a supported host"
    seen: list[dict] = []
    seen_lock = threading.Lock()

    def _recording_openai(*_args, **kwargs):
        client = MagicMock()
        record = {"base_url": str(kwargs.get("base_url") or ""), "calls": []}
        with seen_lock:
            seen.append(record)

        def _create(**call_kwargs):
            record["calls"].append(
                {
                    "model": call_kwargs.get("model"),
                    "extra_body": call_kwargs.get("extra_body") or {},
                }
            )
            return _no_tool_response(**call_kwargs)

        client.chat.completions.create = _create
        client.close = MagicMock()
        return client

    # resolve_runtime_provider is faked ONLY for this test's fake
    # providers; real providers pass through to the host. Newer hosts
    # consult it during batch credential resolution, so a blanket mock
    # breaks the batch leg with a missing-key error.
    try:
        from hermes_cli import runtime_provider as _runtime_provider_mod

        _real_resolve = _runtime_provider_mod.resolve_runtime_provider
    except (ImportError, AttributeError):
        _real_resolve = None

    def _selective_resolve(requested=None, target_model=None, **kwargs):
        if requested in ("prov-alpha", "prov-beta") or _real_resolve is None:
            return {"command": None, "args": []}
        return _real_resolve(
            requested=requested, target_model=target_model, **kwargs
        )

    with patch("hermes_cli.model_switch.switch_model", side_effect=_fake_switch_model), patch(
        "hermes_cli.model_switch.parse_model_flags",
        side_effect=lambda raw: ((raw or "").strip(), "", False, False, False),
    ), patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        side_effect=_selective_resolve,
    ), _patch_client_ctor(_recording_openai), patch.object(
        run_agent.AIAgent, "_build_system_prompt", return_value="You are a test agent"
    ), patch.object(
        run_agent.AIAgent, "_supports_reasoning_extra_body", return_value=True
    ):
        parent = run_agent.AIAgent(
            base_url="https://parent.test/v1",
            api_key="parent-key",
            model="parent-model",
            provider="prov-parent",
            api_mode="chat_completions",
            reasoning_config={"enabled": True, "effort": "medium"},
            max_iterations=1,
            enabled_toolsets=["terminal"],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform="cli",
        )

        raw = dt.delegate_task(
            tasks=[
                {
                    "goal": "Return the word alpha only.",
                    "model": "route-alpha",
                    "reasoning_effort": "low",
                },
                {
                    "goal": "Return the word beta only.",
                    "model": "route-beta",
                    "reasoning_effort": "high",
                },
            ],
            background=False,
            parent_agent=parent,
            **neutral_credentials,
        )

    result = json.loads(raw)
    assert "error" not in result, f"delegate_task failed: {result}"

    child_calls = [
        call
        for record in seen
        if record["base_url"] != "https://parent.test/v1"
        for call in record["calls"]
    ]
    by_model = {call["model"]: call for call in child_calls}
    assert by_model["resolved-alpha"]["extra_body"]["reasoning"]["effort"] == "low"
    assert by_model["resolved-beta"]["extra_body"]["reasoning"]["effort"] == "high"


@pytest.mark.parametrize("inherited_fast", [False, True])
def test_mixed_fast_values_reach_real_host_sdk_boundary(inherited_fast):
    """Actual AIAgent + delegate loop; only the network/SDK is replaced."""
    from copy import deepcopy

    from hermes_cli import models

    resolve_fast_mode_overrides = getattr(models, "resolve_fast_mode_overrides", None)
    if not callable(resolve_fast_mode_overrides):
        pytest.skip("host has no route-aware Fast resolver")
    if not resolve_fast_mode_overrides(
        "gpt-5.4", provider="openai", base_url="https://api.openai.com/v1",
    ):
        pytest.skip("host does not support Fast on the integration test route")
    neutral_credentials = _neutral_credentials_kwargs()
    assert apply_patches() is True
    seen = []

    def recording_client(*_args, **kwargs):
        client = MagicMock()
        client.base_url = kwargs.get("base_url")
        client.api_key = kwargs.get("api_key")
        calls = []
        seen.append(calls)

        def create(**call_kwargs):
            calls.append(deepcopy(call_kwargs))
            return _no_tool_response(**call_kwargs)

        client.chat.completions.create = create
        return client

    inherited = {"service_tier": "priority"} if inherited_fast else {}
    with _patch_client_ctor(recording_client), patch.object(
        run_agent.AIAgent, "_build_system_prompt", return_value="You are a test agent"
    ), patch("socket.socket.connect", side_effect=AssertionError("network forbidden in test")):
        parent = run_agent.AIAgent(
            model="gpt-5.4", provider="openai", base_url="https://api.openai.com/v1",
            api_key="test-only-key", api_mode="chat_completions",
            request_overrides=deepcopy(inherited), max_iterations=1,
            enabled_toolsets=["terminal"], quiet_mode=True,
            skip_context_files=True, skip_memory=True, platform="cli",
        )
        try:
            raw = dt.delegate_task(
                tasks=[
                    {"goal": "Return one word.", "fast": True},
                    {"goal": "Return one word.", "fast": False},
                    {"goal": "Return one word."},
                ],
                background=False, max_iterations=1, parent_agent=parent,
                **neutral_credentials,
            )
            assert parent.request_overrides == inherited
        finally:
            parent.close()

    result = json.loads(raw)
    assert "error" not in result, result
    child_calls = [calls for calls in seen if calls]
    assert len(child_calls) == 3, result
    assert [calls[0].get("service_tier") for calls in child_calls] == [
        "priority", None, "priority" if inherited_fast else None,
    ]
    assert all(call["model"] == "gpt-5.4" for calls in child_calls for call in calls)


@pytest.mark.parametrize("fast", [True, False])
def test_fast_survives_native_codex_request_assembly(fast):
    """Host Fast resolver and Codex transport, with no credentials or network."""
    from hermes_delegate_routing.patches import _apply_fast_override

    models = pytest.importorskip("hermes_cli.models")
    transport = pytest.importorskip("agent.transports.codex")
    windows = pytest.importorskip("agent.fast_mode")
    if not callable(getattr(models, "resolve_fast_mode_overrides", None)):
        pytest.skip("host has no route-aware Fast resolver")
    child = SimpleNamespace(
        model="gpt-5.4", provider="openai-codex", api_mode="codex_responses",
        base_url="https://chatgpt.com/backend-api/codex", service_tier="auto",
        request_overrides={"service_tier": "priority"}, _fast_until=float("inf"),
    )
    _apply_fast_override(child, fast)
    payload = transport.ResponsesApiTransport().build_kwargs(
        child.model, [{"role": "user", "content": "Test request; never sent."}],
        provider=child.provider, base_url=child.base_url, is_codex_backend=True,
        request_overrides=windows.effective_request_overrides(child),
        reasoning_config={"enabled": True, "effort": "high"},
    )
    assert payload.get("service_tier") == ("priority" if fast else None)
    assert payload["model"] == child.model
    assert payload["reasoning"]["effort"] == "high"


def test_fast_rejects_proxy_using_the_real_host_gate():
    from hermes_delegate_routing.patches import _apply_fast_override

    models = pytest.importorskip("hermes_cli.models")
    if not callable(getattr(models, "resolve_fast_mode_overrides", None)):
        pytest.skip("host has no route-aware Fast resolver")
    child = SimpleNamespace(
        model="gpt-5.4", provider="openrouter", base_url="https://openrouter.ai/api/v1",
        service_tier=None, request_overrides={},
    )
    with pytest.raises(ValueError, match="unsupported"):
        _apply_fast_override(child, True)
    assert child.request_overrides == {}
