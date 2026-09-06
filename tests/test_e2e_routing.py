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
    ), patch("run_agent.OpenAI", side_effect=_recording_openai), patch.object(
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

    with patch("hermes_cli.model_switch.switch_model", side_effect=_fake_switch_model), patch(
        "hermes_cli.model_switch.parse_model_flags",
        side_effect=lambda raw: ((raw or "").strip(), "", False, False, False),
    ), patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={"command": None, "args": []},
    ), patch(
        "run_agent.OpenAI", side_effect=_recording_openai
    ), patch.object(
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
