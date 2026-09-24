"""Offline regression at the real host's preflight -> build-children seam."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

dt = pytest.importorskip("tools.delegate_tool", reason="no hermes-agent host on path")


@pytest.fixture
def host_batch(monkeypatch):
    import run_agent
    from hermes_cli import config, model_switch, runtime_provider
    from tools import delegation_live_log
    from hermes_delegate_routing import patches

    cfg = {
        "provider": "broken-baseline", "model": "baseline-model",
        "reasoning_effort": "high",
        "fallback_providers": [{"provider": "backup", "model": "backup-model"}],
    }
    parent = SimpleNamespace(
        model="parent-model", provider="parent-provider", base_url="https://parent.test/v1",
        api_key="parent-key", reasoning_config={"enabled": True, "effort": "low"},
        request_overrides={"parent_only": True},
    )
    built, runtime_calls = [], []

    def switch_model(*, raw_input, explicit_provider, **kwargs):
        if raw_input == "unresolvable":
            return SimpleNamespace(success=False, error_message="unknown route")
        return SimpleNamespace(
            success=True, new_model=raw_input, target_provider=explicit_provider,
            base_url=f"https://{explicit_provider}.test/v1", api_key=f"key-{explicit_provider}",
            api_mode="chat_completions" if explicit_provider == "alpha" else None,
            request_overrides={"alpha_only": True} if explicit_provider == "alpha" else None,
        )

    def resolve_runtime_provider(requested=None, target_model=None, **kwargs):
        runtime_calls.append(requested)
        if requested == "broken-baseline":
            raise ValueError("baseline authentication unavailable")
        return {"command": None, "args": []}

    def child(**kwargs):
        result = SimpleNamespace(**kwargs, service_tier=None)
        built.append(result)
        return result

    monkeypatch.setattr(config, "load_config", lambda: {"delegation": cfg})
    monkeypatch.setattr(dt, "_load_config", lambda: cfg)
    monkeypatch.setattr(model_switch, "switch_model", switch_model)
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", resolve_runtime_provider)
    monkeypatch.setattr(dt, "is_spawn_paused", lambda: False)
    monkeypatch.setattr(dt, "_get_max_spawn_depth", lambda: 1)
    monkeypatch.setattr(dt, "_get_max_concurrent_children", lambda: 8)
    monkeypatch.setattr(dt, "_oneshot_spawn_budget", lambda *args: None)
    monkeypatch.setattr(dt, "_resolve_child_toolsets", lambda *args: ([], []))
    monkeypatch.setattr(dt, "_build_child_system_prompt", lambda *args, **kw: "test child")
    monkeypatch.setattr(dt, "_resolve_child_credential_pool", lambda *args, **kw: None)
    monkeypatch.setattr(dt, "_apply_child_compression_cap", lambda *args: None)
    monkeypatch.setattr(delegation_live_log, "create_live_transcripts", lambda *a, **kw: (None, [], []))
    monkeypatch.setattr(run_agent, "AIAgent", child)
    # Stop at dispatch: real credential preflight, task validation, build loop,
    # child runtime assembly and plugin wrappers execute; no inference runs.
    monkeypatch.setattr(dt, "_run_batch", lambda batch, background: json.dumps({"built": len(built)}))
    assert patches.apply_patches()
    try:
        yield SimpleNamespace(parent=parent, cfg=cfg, built=built, runtime_calls=runtime_calls)
    finally:
        patches.restore_patches()


def test_all_explicit_routes_do_not_authenticate_unused_baseline(host_batch):
    result = json.loads(dt.delegate_task(tasks=[
        {"goal": "Build the alpha artifact", "model": "alpha-model", "provider": "alpha",
         "reasoning_effort": "low", "fast": False},
        {"goal": "Build the beta artifact", "model": "beta-model --provider beta"},
    ], parent_agent=host_batch.parent))

    assert result == {"built": 2}, result
    assert "broken-baseline" not in host_batch.runtime_calls
    alpha, beta = host_batch.built
    assert (alpha.model, alpha.provider, alpha.api_key) == ("alpha-model", "alpha", "key-alpha")
    assert (beta.model, beta.provider, beta.api_key) == ("beta-model", "beta", "key-beta")
    assert alpha.base_url == "https://alpha.test/v1"
    assert beta.base_url == "https://beta.test/v1"
    assert beta.api_mode is None
    assert beta.acp_command is None and beta.acp_args == []
    assert alpha.request_overrides == {"alpha_only": True}
    assert beta.request_overrides == {}  # neither sibling nor parent personality
    assert alpha.reasoning_config == {"enabled": True, "effort": "low"}
    assert beta.reasoning_config == {"enabled": True, "effort": "high"}
    assert alpha.service_tier is None
    assert beta.fallback_model == host_batch.cfg["fallback_providers"]
    assert host_batch.cfg["provider"] == "broken-baseline"

    # A subsequent inherited call must not retain the previous bypass decision.
    result = json.loads(dt.delegate_task(goal="Build an inherited artifact", parent_agent=host_batch.parent))
    assert "baseline authentication unavailable" in result["error"]
    assert len(host_batch.built) == 2


@pytest.mark.parametrize("override,policy", [
    ({}, "fail"),
    ({"model": "model-only"}, "fail"),
    ({"provider": "beta"}, "fail"),
    ({"model": "unresolvable", "provider": "beta"}, "fallback"),
], ids=["inherited", "model-only", "provider-only", "failed-route"])
def test_any_baseline_dependent_task_keeps_real_preflight(host_batch, monkeypatch, override, policy):
    from hermes_delegate_routing import patches

    patches.restore_patches()
    monkeypatch.setattr(patches, "_read_on_error", lambda: policy)
    assert patches.apply_patches()
    result = json.loads(dt.delegate_task(tasks=[
        {"goal": "Build the explicit artifact", "model": "alpha-model", "provider": "alpha"},
        {"goal": "Build the dependent artifact", **override},
    ], parent_agent=host_batch.parent))
    assert "baseline authentication unavailable" in result["error"]
    assert host_batch.runtime_calls[-1] == "broken-baseline"
    assert host_batch.built == []
