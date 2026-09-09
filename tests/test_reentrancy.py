"""Regression test: apply_patches() must not deadlock on loader re-entry.

On some hosts, importing a host module re-enters the plugin loader, which
calls register() → apply_patches() on the same thread. With a
non-re-entrant lock held across the seam installs, the nested pass
deadlocked. apply_patches() now pre-imports host modules before locking
(so a nested pass completes first and sets the sentinel) and uses an RLock
as a backstop.

The test installs a meta-path finder serving a fake host module whose
import side effect runs a nested apply_patches() — simulating the loader
re-entry — then runs the outer apply_patches() in a worker thread with a
join timeout so a regression fails instead of hanging the suite.

NOTE: the re-entry trigger is deliberately NOT ``model_tools``. Starting a
model_tools import from the registration path can deadlock on the module
import lock across threads (upstream fac3877) — an RLock cannot break that
cycle, so _preimport_host() and _invalidate_tool_defs_cache() must never
import model_tools at all (see test_model_tools_import_lock.py). The
same-thread RLock path is exercised here via another host module import.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import threading


def _fake_schema():
    return {
        "description": "Spawn subagents.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"goal": {"type": "string"}},
                    },
                },
            },
        },
    }


def _install_fake_host(monkeypatch):
    import types

    def delegate_task(goal=None, context=None, tasks=None, max_iterations=None,
                      role=None, background=None, parent_agent=None):
        return "orig"

    def _build_child_agent(task_index, goal, context, toolsets, model,
                           max_iterations, task_count, parent_agent,
                           override_provider=None, override_base_url=None,
                           override_api_key=None, override_api_mode=None,
                           override_acp_command=None, override_acp_args=None,
                           role="leaf"):
        return ("child", task_index, model, override_provider)

    def _build_dynamic_schema_overrides():
        return _fake_schema()

    def tool_error(msg):
        import json
        return json.dumps({"error": msg})

    dt = types.ModuleType("tools.delegate_tool")
    dt.delegate_task = delegate_task
    dt._build_child_agent = _build_child_agent
    dt._build_dynamic_schema_overrides = _build_dynamic_schema_overrides
    dt.tool_error = tool_error

    class _Entry:
        def __init__(self, builder):
            self.dynamic_schema_overrides = builder

    class _Registry:
        def __init__(self, entry):
            self._entry = entry

        def get_entry(self, name):
            return self._entry if name == "delegate_task" else None

    reg_mod = types.ModuleType("tools.registry")
    reg_mod.registry = _Registry(_Entry(_build_dynamic_schema_overrides))

    process_mod = types.ModuleType("tools.process_registry")

    def _format_async_delegation(evt):
        return f"Role: leaf   Model: {evt.get('model', '?')}"

    vars(process_mod)["_format_async_delegation"] = _format_async_delegation

    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []

    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.delegate_tool", dt)
    monkeypatch.setitem(sys.modules, "tools.registry", reg_mod)
    monkeypatch.setitem(sys.modules, "tools.process_registry", process_mod)
    monkeypatch.delitem(sys.modules, "model_tools", raising=False)
    # Served by the re-entrant finder below; must be absent until imported.
    monkeypatch.delitem(sys.modules, "tools.process_registry_notifications", raising=False)
    return dt


# Host module whose import simulates the loader re-entry. Deliberately NOT
# model_tools (see module docstring): model_tools must never be imported
# from the registration path at all.
_REENTRY_MODULE = "tools.process_registry_notifications"


class _ReentrantLoader(importlib.abc.Loader):
    """Fake host module whose import simulates loader re-entry."""

    def __init__(self, on_exec):
        self._on_exec = on_exec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        # Provide the seam-D formatter BEFORE the nested pass, so the nested
        # apply_patches() sees the same state the outer pass would on a
        # fully-loaded host (and wraps it exactly once).
        def _format_async_delegation(evt):
            return f"Role: leaf   Model: {evt.get('model', '?')}"

        module._format_async_delegation = _format_async_delegation
        self._on_exec()


class _ReentrantFinder(importlib.abc.MetaPathFinder):
    def __init__(self, loader):
        self._loader = loader

    def find_spec(self, name, path=None, target=None):
        if name == _REENTRY_MODULE:
            return importlib.machinery.ModuleSpec(name, self._loader)
        return None


def test_apply_patches_survives_loader_reentry(monkeypatch):
    from unittest.mock import patch as mock_patch

    from hermes_delegate_routing.patches import apply_patches

    dt = _install_fake_host(monkeypatch)
    orig_delegate = dt.delegate_task
    nested_results = []
    finder = _ReentrantFinder(
        _ReentrantLoader(lambda: nested_results.append(apply_patches()))
    )
    sys.meta_path.insert(0, finder)
    try:
        from types import SimpleNamespace

        switch_calls = []
        real_switch = SimpleNamespace(
            success=True, new_model="ROUTED", target_provider="ROUTEDP",
            base_url=None, api_key=None, api_mode=None, error_message=None,
        )

        def counting_switch(**kwargs):
            switch_calls.append(kwargs)
            return real_switch

        with mock_patch(
            "hermes_cli.model_switch.switch_model", side_effect=counting_switch
        ):
            outcomes = []
            worker = threading.Thread(
                target=lambda: outcomes.append(apply_patches()),
                daemon=True,
            )
            worker.start()
            worker.join(timeout=20)

            assert not worker.is_alive(), (
                "apply_patches() deadlocked on loader re-entry"
            )
            assert outcomes == [True]
            assert nested_results == [True]

            # Single patching: one routing resolution per routed task.
            dt.delegate_task(
                tasks=[{"goal": "a", "model": "m1", "provider": "p1"}],
                parent_agent=None,
            )
            assert len(switch_calls) == 1
    finally:
        sys.meta_path.remove(finder)

    assert dt.delegate_task is not orig_delegate
    assert dt._HDR_PATCHED is True
