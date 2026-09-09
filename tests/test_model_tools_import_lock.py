"""Fork-specific regression: never start a ``model_tools`` import on this path.

Upstream fac3877 fixed ``_invalidate_tool_defs_cache()`` (covered upstream-
side in tests/test_schema_cache_invalidation.py::test_invalidation_never_imports_model_tools).
This fork additionally had ``model_tools`` in ``_preimport_host()`` — called
by ``apply_patches()`` before locking — which re-opens the same cross-thread
module import-lock cycle one frame up the stack::

    register()
     -> apply_patches()
        -> _preimport_host()
           -> import model_tools   # <- must never happen here either

These tests pin the fork fix: with ``model_tools`` unloaded, neither
``_preimport_host()`` nor the full ``apply_patches()`` may attempt its
import (MetaPathFinder tripwire), while patching still succeeds.
"""

from __future__ import annotations

import importlib.abc
import inspect
import sys
import types


class _Tripwire(importlib.abc.MetaPathFinder):
    """Records any attempt to import model_tools; never serves it."""

    def __init__(self, attempted):
        self._attempted = attempted

    def find_spec(self, name, path=None, target=None):
        if name == "model_tools" or name.startswith("model_tools."):
            self._attempted.append(name)
        return None


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
            self._generation = 7

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
    return dt


def test_preimport_host_never_imports_model_tools(monkeypatch):
    """_preimport_host() must not list or import model_tools (fork path)."""
    from hermes_delegate_routing import patches as _p

    # Direct source guard: the module name must not be a preimport candidate,
    # so a future re-add of "model_tools" to the candidate tuple fails here.
    src = inspect.getsource(_p._preimport_host)
    assert '"model_tools"' not in src and "'model_tools'" not in src

    monkeypatch.delitem(sys.modules, "model_tools", raising=False)
    attempted = []
    tripwire = _Tripwire(attempted)
    sys.meta_path.insert(0, tripwire)
    try:
        _p._preimport_host()
    finally:
        sys.meta_path.remove(tripwire)

    assert attempted == []


def test_apply_patches_never_imports_model_tools(monkeypatch):
    """Full apply_patches() with model_tools unloaded starts no such import."""
    from hermes_delegate_routing import patches as _p
    from hermes_delegate_routing.patches import apply_patches

    _install_fake_host(monkeypatch)
    _p._RESTORE_STATE.clear()

    attempted = []
    tripwire = _Tripwire(attempted)
    sys.meta_path.insert(0, tripwire)
    try:
        assert apply_patches() is True
    finally:
        sys.meta_path.remove(tripwire)

    assert attempted == []
